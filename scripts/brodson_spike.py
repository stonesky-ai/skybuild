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
          "concurrency": 1, "total_wall_seconds": 60}
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


# This protocol exercise has no live implementation. A MockTransport is mandatory;
# neither the authority below nor a journal constitutes permission for real calls.
HTTP_ORIGIN = "https://llm.brodson.net"
HTTP_MODEL = "qwen3.5-think"
HTTP_STATE_BYTES = 524288
SYNTHETIC_TOKEN = "synthetic-not-a-credential"


def live_admission(*_args, **_kwargs):
    raise SpikeError("live_disabled_pending_reviewed_REST_authority")


def sanitized(value):
    """Remove authentication fields and bearer-shaped strings from evidence."""
    import re
    if isinstance(value, dict):
        return {key: ("[redacted]" if re.search(
            r"authorization|api.?key|access.?token|secret|password|credential|^token$|token.?file", key, re.I)
            else sanitized(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitized(item) for item in value]
    if isinstance(value, str):
        # Chat content may itself be structured JSON; sanitize authentication
        # fields there as well as in the surrounding endpoint envelope.
        if value.lstrip().startswith(("{", "[")):
            try:
                structured = json.loads(value)
            except (ValueError, RecursionError):
                pass
            else:
                return canonical(sanitized(structured))
        return re.sub(r"(?i)\bBearer\s+[^\s\"',;]+", "Bearer [redacted]",
                      value.replace(SYNTHETIC_TOKEN, "[redacted]"))
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _boot_id():
    """Identify the monotonic-clock epoch; fail closed after a host reboot."""
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        raise SpikeError("journal_boot_id_unavailable") from None
    if len(value) != 36 or any(char not in "0123456789abcdef-" for char in value):
        raise SpikeError("journal_boot_id_invalid")
    return value


def _fake_http_child(connection, transport, method, path, body, deadline_at):
    """Fixed owned child: stream, bound and sanitize before crossing IPC."""
    import httpx
    started = time.monotonic()
    try:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise SpikeError("wall_deadline_before_dispatch")
        with httpx.Client(transport=transport, base_url=HTTP_ORIGIN,
                          trust_env=False, follow_redirects=False,
                          timeout=httpx.Timeout(remaining),
                          headers={"Authorization": "Bearer " + SYNTHETIC_TOKEN}) as client:
            if time.monotonic() >= deadline_at:
                raise SpikeError("wall_deadline_before_dispatch")
            with client.stream(method, path, json=body if body is not None else None) as response:
                if response.status_code != 200:
                    raise SpikeError("HTTP_status_error")
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise SpikeError("compressed_response_refused")
                raw = bytearray()
                chunks = ([response.content] if response.is_stream_consumed
                          else response.iter_raw())
                for chunk in chunks:
                    if len(raw) + len(chunk) > LIMITS["response_bytes"]:
                        raise SpikeError("response_too_large")
                    raw.extend(chunk)
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise SpikeError("malformed_response")
                packet = {"response": sanitized(value),
                          "latency_ms": round((time.monotonic() - started) * 1000, 3)}
    except SpikeError as error:
        packet = {"failure": str(error)}
    except BaseException:
        # Never persist exception text, headers, URLs or raw error bodies.
        packet = {"failure": "transport_or_response_error"}
    try:
        connection.send_bytes(canonical(packet).encode("utf-8"))
    finally:
        connection.close()


def fake_http_request(transport, method, path, body, *, deadline_seconds=60, deadline_at=None):
    """Hard parent deadline covers dispatch, headers, streaming and parsing."""
    import httpx
    import multiprocessing
    import select
    if type(transport) is not httpx.MockTransport:
        raise SpikeError("only_explicit_fake_transport_allowed")
    if (method, path) not in {("GET", "/v1/models"), ("POST", "/v1/chat/completions")}:
        raise SpikeError("request_scope_invalid")
    if type(deadline_seconds) not in (float, int) or not 0 < deadline_seconds <= 60:
        raise SpikeError("invalid_wall_deadline")
    now = time.monotonic()
    end = min(now + deadline_seconds, deadline_at) if deadline_at is not None else now + deadline_seconds
    if end <= now:
        raise SpikeError("wall_deadline_before_dispatch")
    context = multiprocessing.get_context("fork")
    receiving, sending = context.Pipe(duplex=False)
    child = context.Process(target=_fake_http_child, args=(sending, transport, method, path, body, end))
    if time.monotonic() >= end:
        receiving.close()
        sending.close()
        raise SpikeError("wall_deadline_before_dispatch")
    child.start()
    sending.close()
    try:
        # Read the framed pipe nonblockingly so a partially written packet cannot
        # bypass the wall deadline. The child only sends bounded sanitized data.
        os.set_blocking(receiving.fileno(), False)
        data = bytearray()
        expected = None
        while time.monotonic() < end:
            ready, _, _ = select.select([receiving.fileno()], [], [], max(0, end - time.monotonic()))
            if not ready:
                break
            chunk = os.read(receiving.fileno(), 65536)
            if not chunk:
                raise SpikeError("transport_child_incomplete")
            data.extend(chunk)
            if len(data) >= 4 and expected is None:
                expected = int.from_bytes(data[:4], "big", signed=True)
                if not 0 <= expected <= HTTP_STATE_BYTES:
                    raise SpikeError("transport_packet_too_large")
            if expected is not None and len(data) >= expected + 4:
                packet = json.loads(data[4:expected + 4])
                if time.monotonic() >= end:
                    raise SpikeError("wall_deadline_ambiguous_exposure")
                return packet
        raise SpikeError("wall_deadline_ambiguous_exposure")
    finally:
        receiving.close()
        if child.is_alive():
            child.kill()  # Only the child created above; no host process cleanup.
        child.join(timeout=1)


class FakeHTTPJournal:
    """Private synthetic attempt journal. A reservation survives uncertain effects."""

    def __init__(self, root, identity):
        import fcntl
        import stat
        self.root = Path(root)
        metadata = self.root.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700):
            raise SpikeError("journal_root_must_be_owned_private_directory")
        if self.root.absolute() != self.root.resolve():
            raise SpikeError("journal_root_not_canonical")
        self.path = self.root / "attempts.json"
        lock = self.root / "attempts.lock"
        created = False
        try:
            self.lock = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
        except FileExistsError:
            self.lock = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
        try:
            info = os.fstat(self.lock)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise SpikeError("invalid_journal_lock")
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if created:
                if self.path.exists():
                    raise SpikeError("journal_lock_missing")
                os.fsync(self.lock)
                self.state = {"identity": identity, "synthetic": True, "entries": []}
                self.save()
            else:
                descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    info = os.fstat(descriptor)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                        raise SpikeError("invalid_journal_state")
                    with os.fdopen(descriptor, "rb", closefd=False) as stream:
                        raw = stream.read(HTTP_STATE_BYTES + 1)
                    if len(raw) > HTTP_STATE_BYTES:
                        raise SpikeError("journal_too_large")
                    self.state = json.loads(raw)
                    # Re-establish durability before trusting a replacement that
                    # may have survived an earlier failed directory sync.
                    os.fsync(descriptor)
                    directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                finally:
                    os.close(descriptor)
                saved_identity = self.state.get("identity")
                deadline_fields = {"run_deadline_unix", "run_deadline_monotonic", "run_deadline_boot_id"}
                expected_identity = {key: value for key, value in identity.items()
                                     if key not in deadline_fields}
                existing_identity = ({key: value for key, value in saved_identity.items()
                                      if key not in deadline_fields}
                                     if isinstance(saved_identity, dict) else None)
                if (existing_identity != expected_identity or self.state.get("synthetic") is not True
                        or not isinstance(self.state.get("entries"), list)):
                    raise SpikeError("journal_identity_changed")
                if saved_identity.get("run_deadline_boot_id") != identity.get("run_deadline_boot_id"):
                    raise SpikeError("journal_boot_changed")
            saved_deadline = self.state["identity"].get("run_deadline_unix")
            requested_deadline = identity.get("run_deadline_unix")
            saved_monotonic = self.state["identity"].get("run_deadline_monotonic")
            requested_monotonic = identity.get("run_deadline_monotonic")
            boot_id = identity.get("run_deadline_boot_id")
            if (type(saved_deadline) not in (int, float) or type(requested_deadline) not in (int, float)
                    or not 0 < saved_deadline < float("inf")
                    or not 0 < requested_deadline < float("inf")
                    or type(saved_monotonic) not in (int, float) or not 0 < saved_monotonic < float("inf")
                    or type(requested_monotonic) not in (int, float) or not 0 < requested_monotonic < float("inf")
                    or not isinstance(boot_id, str) or len(boot_id) != 36):
                raise SpikeError("journal_deadline_invalid")
            self.deadline_at = min(saved_monotonic, requested_monotonic)
            tightened_unix = min(saved_deadline, requested_deadline)
            if self.deadline_at != saved_monotonic or tightened_unix != saved_deadline:
                self.state["identity"]["run_deadline_monotonic"] = self.deadline_at
                self.state["identity"]["run_deadline_unix"] = tightened_unix
                self.save()
            self.cursor = 0
        except BlockingIOError:
            os.close(self.lock)
            raise SpikeError("journal_locked") from None
        except SpikeError:
            os.close(self.lock)
            raise
        except (OSError, ValueError, TypeError, RecursionError):
            os.close(self.lock)
            raise SpikeError("journal_missing_or_invalid") from None
        except BaseException:
            os.close(self.lock)
            raise

    def close(self):
        os.close(self.lock)

    def save(self):
        payload = canonical(self.state)
        if len(payload.encode("utf-8")) > HTTP_STATE_BYTES:
            raise SpikeError("journal_too_large")
        write_result(self.path, payload)

    def obtain(self, request, transport, *, deadline_seconds, run_deadline=None):
        if (not isinstance(request, dict) or set(request) != {"method", "path", "body", "case_id"}
                or (request["method"], request["path"]) not in {
                    ("GET", "/v1/models"), ("POST", "/v1/chat/completions")}
                or (request["method"] == "GET" and (request["body"] is not None or request["case_id"] is not None))
                or (request["method"] == "POST" and request["case_id"] not in SOURCES)):
            raise SpikeError("request_scope_invalid")
        entries = self.state["entries"]
        if self.cursor < len(entries):
            entry = entries[self.cursor]
            if entry.get("request") != request:
                raise SpikeError("journal_request_identity_changed")
            if entry.get("status") != "completed":
                raise SpikeError("consumed_attempt_not_reconciled")
            self.cursor += 1
            return entry["packet"]
        if any(entry.get("status") != "completed" for entry in entries):
            raise SpikeError("consumed_attempt_not_reconciled")
        if time.time() >= self.state["identity"]["expires_at"]:
            raise SpikeError("synthetic_authority_expired")
        deadline = min(self.deadline_at, run_deadline) if run_deadline is not None else self.deadline_at
        if time.monotonic() >= deadline:
            raise SpikeError("total_wall_deadline_exhausted")
        generations = [entry for entry in entries if entry["request"]["method"] == "POST"]
        discovery = [entry for entry in entries if entry["request"]["method"] == "GET"]
        if request["method"] == "GET":
            if discovery or generations:
                raise SpikeError("discovery_budget_exhausted")
        elif (len(discovery) != 1 or len(generations) >= LIMITS["requests"]
              or sum(entry["request"]["case_id"] == request["case_id"] for entry in generations) >= 2
              or (len(generations) + 1) * (LIMITS["input_tokens"] + LIMITS["output_tokens"]) > LIMITS["returned_tokens"]):
            raise SpikeError("generation_budget_exhausted")
        entry = {"request": request, "status": "consumed_uncertain"}
        entries.append(entry)
        self.save()  # File and directory fsync BEFORE the simulated external effect.
        started = time.monotonic()
        try:
            packet = fake_http_request(transport, request["method"], request["path"], request["body"],
                                       deadline_seconds=deadline_seconds, deadline_at=deadline)
        except SpikeError as error:
            entry["parent_elapsed_wall_ms"] = round((time.monotonic() - started) * 1000, 3)
            entry["failure"] = str(error)
            self.save()
            raise
        entry["parent_elapsed_wall_ms"] = round((time.monotonic() - started) * 1000, 3)
        entry["packet"] = packet
        if "failure" in packet:
            entry["failure"] = packet["failure"]
            self.save()
            raise SpikeError(packet["failure"])
        entry["status"] = "completed"
        self.save()
        self.cursor += 1
        return packet


def _http_candidate(packet):
    value = packet["response"]
    if "error" in value or value.get("model") != HTTP_MODEL:
        raise SpikeError("endpoint_error_or_model_mismatch")
    choices = value.get("choices")
    if (not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict)
            or choices[0].get("finish_reason") != "stop"
            or not isinstance(choices[0].get("message"), dict)
            or choices[0]["message"].get("role") != "assistant"):
        raise SpikeError("partial_or_ambiguous_completion")
    usage = value.get("usage")
    if (not isinstance(usage, dict)
            or not integer(usage.get("prompt_tokens"), 1, LIMITS["input_tokens"])
            or not integer(usage.get("completion_tokens"), 1, LIMITS["output_tokens"])
            or not integer(usage.get("total_tokens"), 2, LIMITS["input_tokens"] + LIMITS["output_tokens"])
            or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]):
        raise SpikeError("usage_unknown_or_budget_exceeded")
    content = choices[0]["message"].get("content")
    if not isinstance(content, str):
        raise SpikeError("missing_response_content")
    try:
        return json.loads(content), content, usage
    except (ValueError, RecursionError):
        raise SpikeError("malformed_candidate_JSON") from None


def run_fake_http(manifest, recordings, transport, journal_root, *, authority,
                  count_tokens, deadline_seconds=60):
    """Exercise future protocol on explicit mocks; this is NOT live admission."""
    import httpx
    if type(deadline_seconds) not in (float, int) or not 0 < deadline_seconds <= 60:
        raise SpikeError("invalid_total_wall_deadline")
    started_mono = time.monotonic()
    started_wall = time.time()
    run_deadline_unix = started_wall + deadline_seconds
    run_deadline_mono = started_mono + deadline_seconds
    if type(transport) is not httpx.MockTransport:
        raise SpikeError("only_explicit_fake_transport_allowed")
    validate_manifest(manifest, recordings)
    if (not isinstance(authority, dict) or set(authority) != {"synthetic", "assignment_id", "head", "expires_at"}
            or authority["synthetic"] is not True or authority["assignment_id"] != "synthetic-http-preparation"
            or not isinstance(authority["head"], str) or len(authority["head"]) != 40
            or any(char not in "0123456789abcdef" for char in authority["head"])
            or type(authority["expires_at"]) not in (int, float) or not 0 < authority["expires_at"] < float("inf")):
        raise SpikeError("explicit_synthetic_authority_required")
    if not callable(count_tokens):
        raise SpikeError("tokenization_unknown")
    run_deadline_unix = min(run_deadline_unix, authority["expires_at"])
    run_deadline_mono = min(run_deadline_mono,
                            started_mono + max(0, authority["expires_at"] - started_wall))
    boot_id = _boot_id()
    identity = authority | {"run_deadline_unix": run_deadline_unix,
                            "run_deadline_monotonic": run_deadline_mono,
                            "run_deadline_boot_id": boot_id,
                            "manifest_sha256": digest(canonical(manifest)), "origin": HTTP_ORIGIN,
                            "model": HTTP_MODEL, "limits": LIMITS,
                            "journal_root": str(Path(journal_root).resolve())}
    result = {"synthetic": True, "mode": "mock-http", "live_calls": 0, "status": "prepared",
              "synthetic_tokenization": True, "cases": [], "returned_tokens": 0}
    journal = FakeHTTPJournal(journal_root, identity)
    try:
        discovery = journal.obtain({"method": "GET", "path": "/v1/models", "body": None,
                                    "case_id": None}, transport, deadline_seconds=deadline_seconds,
                                    run_deadline=run_deadline_mono)["response"]
        if "error" in discovery:
            raise SpikeError("discovery_endpoint_error")
        # Synthetic metadata only: real loaded identity/capacity remains unqualified.
        models = discovery.get("data")
        if (not isinstance(models, list) or sum(isinstance(item, dict) and item.get("id") == HTTP_MODEL
                                               for item in models) != 1):
            raise SpikeError("model_identity_unknown")
        model = next(item for item in models if isinstance(item, dict) and item.get("id") == HTTP_MODEL)
        if model.get("status") != "loaded" or model.get("synthetic_capacity_reserved") is not True:
            raise SpikeError("model_identity_or_capacity_unqualified")
        for case in manifest["cases"]:
            messages = [{"role": "user", "content": sanitized(case["prompt"])}]
            for attempt in range(2):
                prompt = canonical(messages)
                if len(prompt.encode("utf-8")) > LIMITS["prompt_bytes"]:
                    raise SpikeError("prompt_budget_exceeded")
                try:
                    tokens = count_tokens(messages)
                except Exception:
                    raise SpikeError("tokenization_unknown") from None
                if not integer(tokens, 1, LIMITS["input_tokens"]):
                    raise SpikeError("tokenization_unknown_or_input_budget_exceeded")
                body = {"model": HTTP_MODEL, "messages": messages, "stream": False,
                        "temperature": 0, "max_tokens": LIMITS["output_tokens"],
                        "chat_template_kwargs": {"enable_thinking": False}}
                packet = journal.obtain({"method": "POST", "path": "/v1/chat/completions",
                                         "case_id": case["id"], "body": body}, transport,
                                        deadline_seconds=deadline_seconds,
                                        run_deadline=run_deadline_mono)
                candidate, content, usage = _http_candidate(packet)
                result["returned_tokens"] += usage["total_tokens"]
                if result["returned_tokens"] > LIMITS["returned_tokens"]:
                    raise SpikeError("total_token_budget_exceeded")
                verdict = evaluate(case, candidate)
                journal.state["entries"][journal.cursor - 1]["validation"] = verdict
                journal.save()
                result["cases"].append({"case_id": case["id"], "attempt": attempt + 1, **verdict})
                if verdict["accepted"]:
                    break
                if attempt == 1:
                    raise SpikeError("second_defective_proposal")
                messages = messages + [{"role": "assistant", "content": content},
                    {"role": "user", "content": "Correction required: " + verdict["reason"] +
                     ". Return only corrected JSON. This is the final attempt."}]
    except SpikeError as error:
        result["status"], result["stop_reason"] = "stopped", str(error)
        journal.state["stop_reason"] = str(error)
        journal.save()
    finally:
        result["consumed_discovery"] = sum(entry["request"]["method"] == "GET" for entry in journal.state["entries"])
        result["consumed_generation"] = sum(entry["request"]["method"] == "POST" for entry in journal.state["entries"])
        result["reserved_token_exposure"] = result["consumed_generation"] * (LIMITS["input_tokens"] + LIMITS["output_tokens"])
        journal.close()
    return result


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
