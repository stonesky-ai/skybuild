"""Offline evidence must not imply live inference or accept unchecked proposals."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import stat

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("brodson_spike", ROOT / "scripts/brodson_spike.py")
spike = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spike)


@pytest.fixture
def fixtures():
    return (spike.read_json(spike.FIXTURES / "cases.json"),
            spike.read_json(spike.FIXTURES / "recorded-responses.json"))


def test_synthetic_replay_measures_acceptance_and_correction_cost(fixtures):
    manifest, responses = fixtures
    original = deepcopy(responses)
    result = spike.run_replay(manifest, responses)
    assert result["status"] == "prepared"
    assert result["synthetic"] is True and result["live_calls"] == 0
    assert result["model"] == "synthetic-fixture"
    assert result["simulated_requests"] == 5
    assert result["first_pass_accepted"] == 1
    assert result["corrected_accepted"] == 2
    assert result["synthetic_returned_tokens"] == 1346
    assert result["usage_complete"] is True
    assert all(case["status"] == "accepted" for case in result["cases"])
    assert len(result["cases_sha256"]) == len(result["recordings_sha256"]) == 64
    assert responses == original


def test_code_case_rejects_byte_overshoot_and_accepts_valid_expression(fixtures):
    manifest, responses = fixtures
    case = manifest["cases"][0]
    attempts = responses["cases"]["utf8-clip"]
    assert spike.evaluate(case, json.loads(attempts[0]["content"]))["accepted"] is False
    assert spike.evaluate(case, json.loads(attempts[1]["content"]))["accepted"] is True
    alternate = {"replacement": 'text.encode()[:byte_limit].decode(errors="ignore")'}
    assert spike.evaluate(case, alternate)["accepted"] is True


@pytest.mark.parametrize("text,limit,expected", [
    ("café", 4, "caf"), ("café", 5, "café"), ("🙂ok", 3, ""),
    ("🙂ok", 4, "🙂"), ("e\u0301", 2, "e"), ("", 0, ""),
])
def test_code_point_prefix_boundaries(text, limit, expected):
    expression = 'text.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore")'
    assert spike.clip_expression(expression, text, limit) == expected


@pytest.mark.parametrize("text,limit", [("x", True), ("x", 1.0), ("x", "1"),
                                        ("x", None), ("x", -1), ("x", 513), (7, 1)])
def test_fixed_fixture_argument_validation_is_preserved(text, limit):
    with pytest.raises(ValueError, match="Invalid clipping arguments"):
        spike.clip_expression("text", text, limit)


@pytest.mark.parametrize("expression", [
    '__import__("os").getcwd()', 'open("private-file").read()',
    "text.__class__", "text * 999999999", "[char for char in text]",
    'text.encode("utf-16").decode("utf-16")', 'text.encode(errors="ignore")',
    'text.encode("utf-8", encoding="utf-8")', "text[::0]",
])
def test_code_case_refuses_operations_outside_pure_grammar(expression):
    with pytest.raises((spike.CandidateError, ValueError, SyntaxError)):
        spike.clip_expression(expression, "café", 4)


def test_review_requires_actual_counterexample_and_rejects_false_positive(fixtures):
    manifest, responses = fixtures
    case = manifest["cases"][1]
    good = json.loads(responses["cases"]["interval-review"][0]["content"])
    assert spike.evaluate(case, good)["accepted"] is True
    false_positive = deepcopy(good)
    false_positive["findings"][0]["b"] = [1, 3]
    assert spike.evaluate(case, false_positive)["accepted"] is False
    wrong_line = deepcopy(good)
    wrong_line["findings"][0]["line"] = 1
    assert spike.evaluate(case, wrong_line)["accepted"] is False
    extra = deepcopy(good)
    extra["findings"].append(deepcopy(extra["findings"][0]))
    assert spike.evaluate(case, extra)["accepted"] is False


def test_test_design_requires_valid_expectations_and_kills_both_mutants(fixtures):
    manifest, responses = fixtures
    case = manifest["cases"][2]
    weak, strong = [json.loads(row["content"]) for row in responses["cases"]["rle-test-design"]]
    assert spike.evaluate(case, weak)["accepted"] is False
    assert spike.evaluate(case, strong)["accepted"] is True
    bad_expectation = deepcopy(strong)
    bad_expectation["tests"][2]["expected"] = [["a", 2], ["b", 1]]
    assert spike.evaluate(case, bad_expectation)["accepted"] is False
    boolean_count = deepcopy(strong)
    boolean_count["tests"][1]["expected"] = [["a", True]]
    assert spike.evaluate(case, boolean_count)["accepted"] is False


@pytest.mark.parametrize("change", [
    {"synthetic_usage": None},
    {"synthetic_usage": {"prompt_tokens": True, "completion_tokens": 1}},
    {"synthetic_usage": {"prompt_tokens": 2049, "completion_tokens": 1}},
    {"synthetic_usage": {"prompt_tokens": 1, "completion_tokens": 513}},
    {"synthetic_latency_ms": 60001}, {"synthetic_latency_ms": None},
    {"finish_reason": "length"}, {"content": "{"}, {"content": "x" * 16385},
    {"ambiguous_completion": True}, {"error": "synthetic unavailable"},
])
def test_unknown_completion_usage_or_budget_failure_stops_without_retry(fixtures, change):
    manifest, responses = fixtures
    responses["cases"]["utf8-clip"][0].update(change)
    result = spike.run_replay(manifest, responses)
    assert result["status"] == "stopped"
    assert result["simulated_requests"] == 1 and result["live_calls"] == 0
    assert result["usage_complete"] is False
    assert result["cases"][1]["status"] == result["cases"][2]["status"] == "not_run"


def test_second_defective_proposal_stops_other_cases(fixtures):
    manifest, responses = fixtures
    responses["cases"]["utf8-clip"][1] = deepcopy(responses["cases"]["utf8-clip"][0])
    result = spike.run_replay(manifest, responses)
    assert result["status"] == "rejected"
    assert result["simulated_requests"] == 2
    assert result["first_pass_accepted"] == result["corrected_accepted"] == 0
    assert result["cases"][1]["status"] == "not_run"


def test_fixture_identity_prompt_and_attempt_limits_fail_closed(fixtures):
    manifest, responses = fixtures
    responses["synthetic"] = False
    with pytest.raises(spike.SpikeError, match="synthetic_recordings_required"):
        spike.run_replay(manifest, responses)
    responses["synthetic"] = True
    responses["cases"]["utf8-clip"].append(deepcopy(responses["cases"]["utf8-clip"][0]))
    with pytest.raises(spike.SpikeError, match="attempt_budget_invalid"):
        spike.run_replay(manifest, responses)
    responses["cases"]["utf8-clip"].pop()
    manifest["cases"][0]["prompt"] = "x" * 8193
    with pytest.raises(spike.SpikeError, match="prompt_budget_exceeded"):
        spike.run_replay(manifest, responses)


def test_live_mode_refuses_before_any_input_read(monkeypatch, capsys):
    def forbidden_read(path):
        raise AssertionError("live refusal must precede reading inputs")
    monkeypatch.setattr(spike, "read_json", forbidden_read)
    assert spike.main(["--live", "--cases", "/does-not-exist"]) == 2
    assert "Live mode is disabled" in capsys.readouterr().out


def test_cli_writes_private_atomic_offline_result(tmp_path):
    destination = tmp_path / "result.json"
    assert spike.main(["--output", str(destination)]) == 0
    result = json.loads(destination.read_text())
    assert result["status"] == "prepared" and result["live_calls"] == 0
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [destination]


def test_oversized_fixture_read_is_bounded(tmp_path):
    path = tmp_path / "oversized.json"
    path.write_bytes(b" " * 131073)
    with pytest.raises(spike.SpikeError, match="fixture_file_too_large"):
        spike.read_json(path)
