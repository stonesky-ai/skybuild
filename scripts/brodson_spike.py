#!/usr/bin/env python3
"""Replay synthetic Brodson qualification cases; live transport is disabled."""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time


BASE = "257216c5498b95d5a6e56bca60c31abcd6736226"
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "docs/research/brodson-spike"
LIMITS = {"cases": 3, "requests": 6, "attempts_per_case": 2,
          "prompt_bytes": 8192, "input_tokens": 2048, "output_tokens": 512,
          "response_bytes": 16384, "latency_ms": 60000, "returned_tokens": 15360,
          "concurrency": 1}
CLIP_SOURCE = '''def clip_utf8(text, byte_limit):
    if not isinstance(text, str) or type(byte_limit) is not int:
        raise ValueError("Invalid clipping arguments")
    if not 0 <= byte_limit <= 512:
        raise ValueError("Invalid clipping arguments")
    return text[:byte_limit]
'''
INTERVAL_SOURCE = '''def overlaps(a, b):
    """Nonempty integer half-open intervals, with endpoints in -32..32."""
    return max(a[0], b[0]) <= min(a[1], b[1])
'''
RLE_SOURCE = '''def rle(text):
    runs = []
    for char in text:
        if runs and runs[-1][0] == char:
            runs[-1][1] += 1
        else:
            runs.append([char, 1])
    return runs
'''
SOURCES = {"utf8-clip": CLIP_SOURCE, "interval-review": INTERVAL_SOURCE,
           "rle-test-design": RLE_SOURCE}


class SpikeError(ValueError):
    """A fixture or run boundary requires stopping."""


class CandidateError(ValueError):
    """A bounded proposal failed a deterministic assertion."""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def integer(value, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def read_json(path: Path) -> dict:
    with path.open("rb") as stream:
        data = stream.read(131073)
    if len(data) > 131072:
        raise SpikeError("fixture_file_too_large")
    return json.loads(data)


def validate_manifest(manifest: dict, recordings: dict) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema") != "brodson-spike-cases-v1" or manifest.get("base_sha") != BASE:
        raise SpikeError("invalid_case_manifest")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or len(cases) != 3 or any(not isinstance(case, dict) for case in cases):
        raise SpikeError("expected_three_cases")
    if [case.get("id") for case in cases] != list(SOURCES):
        raise SpikeError("case_identity_or_order_changed")
    for case in cases:
        if case.get("source") != SOURCES[case["id"]] or not isinstance(case.get("prompt"), str):
            raise SpikeError("case_source_or_prompt_invalid")
        if len(case["prompt"].encode("utf-8")) > LIMITS["prompt_bytes"]:
            raise SpikeError("prompt_budget_exceeded")
    checks = cases[0].get("checks")
    if not isinstance(checks, list) or not 8 <= len(checks) <= 64:
        raise SpikeError("invalid_clip_checks")
    for row in checks:
        if (not isinstance(row, dict) or set(row) != {"text", "byte_limit"}
                or not isinstance(row["text"], str) or len(row["text"].encode("utf-8")) > 512
                or not integer(row["byte_limit"], 0, 512)):
            raise SpikeError("invalid_clip_check")
    if (not isinstance(recordings, dict) or recordings.get("schema") != "brodson-spike-responses-v1"
            or recordings.get("synthetic") is not True or recordings.get("model") != "synthetic-fixture"
            or not isinstance(recordings.get("cases"), dict) or set(recordings["cases"]) != set(SOURCES)):
        raise SpikeError("synthetic_recordings_required")
    for attempts in recordings["cases"].values():
        if not isinstance(attempts, list) or not 1 <= len(attempts) <= LIMITS["attempts_per_case"]:
            raise SpikeError("attempt_budget_invalid")


def clip_expression(expression: str, text: str, byte_limit: int) -> str:
    """Interpret a small pure expression grammar without eval, exec or imports."""
    if not isinstance(text, str) or not integer(byte_limit, 0, 512):
        raise ValueError("Invalid clipping arguments")
    if not isinstance(expression, str) or len(expression.encode("utf-8")) > 1024:
        raise CandidateError("replacement_too_large")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 100:
        raise CandidateError("replacement_too_complex")

    def visit(node):
        if isinstance(node, ast.Name) and node.id in {"text", "byte_limit"}:
            return text if node.id == "text" else byte_limit
        if isinstance(node, ast.Constant) and (isinstance(node.value, str) or integer(node.value, 0, 512)):
            return node.value
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            value = visit(node.value)
            bounds = [visit(part) if part is not None else None
                      for part in (node.slice.lower, node.slice.upper, node.slice.step)]
            if not isinstance(value, (str, bytes)) or any(part is not None and not integer(part, 0, 512) for part in bounds):
                raise CandidateError("invalid_slice")
            if bounds[2] == 0:
                raise CandidateError("invalid_slice")
            return value[slice(*bounds)]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            value = visit(node.func.value)
            args = [visit(arg) for arg in node.args]
            kwargs = {keyword.arg: visit(keyword.value) for keyword in node.keywords}
            if len(kwargs) != len(node.keywords) or None in kwargs or set(kwargs) - {"encoding", "errors"} or len(args) > 2:
                raise CandidateError("invalid_codec_arguments")
            if (args and "encoding" in kwargs) or (len(args) == 2 and "errors" in kwargs):
                raise CandidateError("duplicate_codec_argument")
            encoding = args[0] if args else kwargs.get("encoding", "utf-8")
            errors = args[1] if len(args) == 2 else kwargs.get("errors", "strict")
            if encoding != "utf-8" or errors not in {"strict", "ignore"}:
                raise CandidateError("unsupported_codec")
            if node.func.attr == "encode" and isinstance(value, str) and errors == "strict":
                return value.encode("utf-8")
            if node.func.attr == "decode" and isinstance(value, bytes):
                return value.decode("utf-8", errors=errors)
        raise CandidateError("forbidden_expression")

    value = visit(tree.body)
    if not isinstance(value, str):
        raise CandidateError("replacement_must_return_text")
    return value


def check_clip(case: dict, candidate: dict) -> None:
    if set(candidate) != {"replacement"}:
        raise CandidateError("expected_one_replacement")
    for row in case["checks"]:
        text, limit = row["text"], row["byte_limit"]
        actual = clip_expression(candidate["replacement"], text, limit)
        used, prefix = 0, ""
        for char in text:
            size = len(char.encode("utf-8"))
            if used + size > limit:
                break
            prefix += char
            used += size
        if actual != prefix:
            raise CandidateError("prefix_or_byte_budget_assertion_failed")


def interval(value) -> bool:
    return (isinstance(value, list) and len(value) == 2
            and all(integer(item, -32, 32) for item in value) and value[0] < value[1])


def check_review(candidate: dict) -> None:
    findings = candidate.get("findings")
    if set(candidate) != {"findings"} or not isinstance(findings, list) or len(findings) != 1:
        raise CandidateError("expected_one_demonstrated_finding")
    finding = findings[0]
    if not isinstance(finding, dict) or set(finding) != {"line", "a", "b", "actual", "expected"}:
        raise CandidateError("invalid_finding")
    if type(finding["line"]) is not int or finding["line"] != 3 or not interval(finding["a"]) or not interval(finding["b"]):
        raise CandidateError("invalid_finding_location_or_intervals")
    a, b = finding["a"], finding["b"]
    actual = max(a[0], b[0]) <= min(a[1], b[1])
    expected = bool(set(range(*a)) & set(range(*b)))
    if actual == expected or finding["actual"] is not actual or finding["expected"] is not expected:
        raise CandidateError("counterexample_does_not_demonstrate_bug")


def rle(text: str) -> list:
    runs = []
    for char in text:
        if runs and runs[-1][0] == char:
            runs[-1][1] += 1
        else:
            runs.append([char, 1])
    return runs


def check_test_design(candidate: dict) -> None:
    tests = candidate.get("tests")
    if set(candidate) != {"tests"} or not isinstance(tests, list) or not 4 <= len(tests) <= 8:
        raise CandidateError("expected_four_to_eight_tests")
    inputs, coverage, killed = set(), set(), set()
    for row in tests:
        if not isinstance(row, dict) or set(row) != {"input", "expected"}:
            raise CandidateError("invalid_test_row")
        text, expected = row["input"], row["expected"]
        if not isinstance(text, str) or len(text) > 128 or text in inputs or not isinstance(expected, list) or len(expected) > 128:
            raise CandidateError("invalid_test_input")
        inputs.add(text)
        if any(not isinstance(run, list) or len(run) != 2 or not isinstance(run[0], str)
               or len(run[0]) != 1 or not integer(run[1], 1, 128) for run in expected):
            raise CandidateError("invalid_expected_runs")
        if (sum(run[1] for run in expected) != len(text)
                or "".join(char * count for char, count in expected) != text
                or any(left[0] == right[0] for left, right in zip(expected, expected[1:]))):
            raise CandidateError("expected_runs_violate_independent_invariants")
        if not text:
            coverage.add("empty")
        if any(count > 1 for _, count in expected):
            coverage.add("adjacent_repeat")
        if len({char for char, _ in expected}) < len(expected):
            coverage.add("separated_repeat")
        if any(ord(char) > 127 for char in text):
            coverage.add("unicode")
        global_counts = [[char, text.count(char)] for char in dict.fromkeys(text)]
        if expected != global_counts:
            killed.add("merge_nonadjacent")
        if expected != rle(text)[:-1]:
            killed.add("drop_final_run")
    if coverage != {"empty", "adjacent_repeat", "separated_repeat", "unicode"} or len(killed) != 2:
        raise CandidateError("coverage_or_mutation_assertion_failed")


def evaluate(case: dict, candidate) -> dict:
    try:
        if not isinstance(candidate, dict):
            raise CandidateError("candidate_must_be_an_object")
        if case["id"] == "utf8-clip":
            check_clip(case, candidate)
        elif case["id"] == "interval-review":
            check_review(candidate)
        else:
            check_test_design(candidate)
    except CandidateError as error:
        return {"accepted": False, "reason": str(error)}
    except (SyntaxError, TypeError, ValueError, KeyError, UnicodeError, RecursionError):
        return {"accepted": False, "reason": "invalid_candidate"}
    return {"accepted": True, "reason": "independent_assertions_passed"}


def response_content(response: dict) -> tuple[dict, dict]:
    if not isinstance(response, dict) or response.get("error") or response.get("ambiguous_completion"):
        raise SpikeError("endpoint_error_or_ambiguous_completion")
    if response.get("finish_reason") != "stop":
        raise SpikeError("incomplete_response")
    latency = response.get("synthetic_latency_ms")
    if not integer(latency, 0, LIMITS["latency_ms"]):
        raise SpikeError("latency_unknown_or_timeout")
    usage = response.get("synthetic_usage")
    if (not isinstance(usage, dict) or set(usage) != {"prompt_tokens", "completion_tokens"}
            or not integer(usage["prompt_tokens"], 0, LIMITS["input_tokens"])
            or not integer(usage["completion_tokens"], 0, LIMITS["output_tokens"])):
        raise SpikeError("usage_unknown_or_budget_exceeded")
    content = response.get("content")
    if not isinstance(content, str) or len(content.encode("utf-8")) > LIMITS["response_bytes"]:
        raise SpikeError("response_missing_or_too_large")
    try:
        candidate = json.loads(content)
    except ValueError:
        raise SpikeError("malformed_response") from None
    return candidate, usage


def run_replay(manifest: dict, recordings: dict) -> dict:
    validate_manifest(manifest, recordings)
    result = {"schema": "brodson-spike-preparation-v1", "synthetic": True,
              "mode": "offline-replay", "live_calls": 0, "source_base": BASE,
              "cases_sha256": digest(json.dumps(manifest, sort_keys=True, ensure_ascii=False)),
              "recordings_sha256": digest(json.dumps(recordings, sort_keys=True, ensure_ascii=False)),
              "model": "synthetic-fixture", "limits": LIMITS.copy(), "status": "prepared",
              "simulated_requests": 0, "synthetic_returned_tokens": 0, "usage_complete": True,
              "first_pass_accepted": 0, "corrected_accepted": 0, "cases": [],
              "live_prerequisites": ["REST worker qualification", "explicit parent clearance",
                                     "qualified tokenizer and accounting", "separate live transport review"]}
    stopped = False
    for case in manifest["cases"]:
        row = {"case_id": case["id"], "prompt_sha256": digest(case["prompt"]),
               "status": "not_run", "attempts": []}
        result["cases"].append(row)
        if stopped:
            continue
        for index, response in enumerate(recordings["cases"][case["id"]]):
            result["simulated_requests"] += 1
            try:
                if result["simulated_requests"] > LIMITS["requests"]:
                    raise SpikeError("request_budget_exceeded")
                candidate, usage = response_content(response)
                result["synthetic_returned_tokens"] += sum(usage.values())
                if result["synthetic_returned_tokens"] > LIMITS["returned_tokens"]:
                    raise SpikeError("total_token_budget_exceeded")
            except SpikeError as error:
                row["attempts"].append({"number": index + 1, "stop_reason": str(error)})
                row["status"], result["status"] = "stopped", "stopped"
                result["stop_reason"], result["usage_complete"] = str(error), False
                stopped = True
                break
            started = time.perf_counter()
            verdict = evaluate(case, candidate)
            row["attempts"].append(verdict | {"number": index + 1,
                "content_sha256": digest(response["content"]), "synthetic_usage": usage,
                "synthetic_latency_ms": response["synthetic_latency_ms"],
                "validation_cpu_ms": round((time.perf_counter() - started) * 1000, 3)})
            if verdict["accepted"]:
                row["status"] = "accepted"
                result["first_pass_accepted" if index == 0 else "corrected_accepted"] += 1
                break
        if row["status"] == "not_run":
            row["status"], result["status"] = "rejected", "rejected"
            result["stop_reason"] = "recordings_exhausted_or_second_defective_proposal"
            stopped = True
    return result


def write_result(path: Path, payload: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=FIXTURES / "cases.json")
    parser.add_argument("--responses", type=Path, default=FIXTURES / "recorded-responses.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--live", action="store_true", help="Disabled until separate qualification and clearance")
    args = parser.parse_args(argv)
    if args.live:
        print(json.dumps({"ok": False, "reason": "Live mode is disabled; REST qualification and parent clearance are required"}))
        return 2
    try:
        result = run_replay(read_json(args.cases), read_json(args.responses))
        payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output:
            write_result(args.output, payload)
        else:
            print(payload, end="")
        return 0 if result["status"] == "prepared" else 2
    except SpikeError as error:
        print(json.dumps({"ok": False, "reason": str(error)}))
        return 2
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        print(json.dumps({"ok": False, "reason": "Invalid or unavailable offline fixture input"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
