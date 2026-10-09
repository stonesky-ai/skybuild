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


@pytest.fixture
def http_setup(fixtures, tmp_path):
    import time
    manifest, recordings = fixtures
    root = tmp_path / "journal"
    root.mkdir(mode=0o700)
    authority = {"synthetic": True, "assignment_id": "synthetic-http-preparation",
                 "head": "a" * 40, "expires_at": time.time() + 300}
    return manifest, recordings, root, authority


def http_transport(manifest, recordings, *, generation_change=None, discovery_change=None):
    """Assertions run inside the owned child; failures stop the parent run."""
    import httpx

    def handler(request):
        assert request.headers["authorization"] == "Bearer " + spike.SYNTHETIC_TOKEN
        assert str(request.url).startswith(spike.HTTP_ORIGIN + "/v1/")
        if request.method == "GET":
            assert request.url.path == "/v1/models"
            document = {"data": [{"id": spike.HTTP_MODEL, "status": "loaded",
                                   "synthetic_capacity_reserved": True}]}
            if discovery_change:
                document = discovery_change(document)
        else:
            assert request.method == "POST" and request.url.path == "/v1/chat/completions"
            body = json.loads(request.content)
            assert body["model"] == spike.HTTP_MODEL
            assert body["stream"] is False and body["max_tokens"] == 512
            assert body["temperature"] == 0
            assert body["chat_template_kwargs"] == {"enable_thinking": False}
            messages = body["messages"]
            assert len(messages) in (1, 3)
            case = next(case for case in manifest["cases"]
                        if spike.sanitized(case["prompt"]) == messages[0]["content"])
            index = int(len(messages) == 3)
            if index:
                assert messages[1]["role"] == "assistant"
                assert "final attempt" in messages[2]["content"]
            recorded = recordings["cases"][case["id"]][index]
            usage = recorded["synthetic_usage"]
            document = {"model": spike.HTTP_MODEL, "choices": [{"finish_reason": "stop",
                        "message": {"role": "assistant", "content": recorded["content"]}}],
                        "usage": usage | {"total_tokens": sum(usage.values())}}
            if generation_change:
                document = generation_change(document)
        return httpx.Response(200, json=document)

    return httpx.MockTransport(handler)


def run_http(setup, transport=None, **options):
    manifest, recordings, root, authority = setup
    return spike.run_fake_http(manifest, recordings,
                               transport or http_transport(manifest, recordings), root,
                               authority=authority, count_tokens=options.pop("count_tokens", lambda _: 32),
                               **options)


def test_fake_http_roundtrip_and_restart_keep_original_requests(http_setup):
    import httpx
    result = run_http(http_setup)
    assert result["status"] == "prepared" and result["live_calls"] == 0
    assert result["synthetic"] is True and result["synthetic_tokenization"] is True
    assert (result["consumed_discovery"], result["consumed_generation"]) == (1, 5)
    assert [row["accepted"] for row in result["cases"]] == [False, True, True, False, True]
    root = http_setup[2]
    before = (root / "attempts.json").read_bytes()
    state = json.loads(before)
    assert len(state["entries"]) == 6
    assert all(entry["status"] == "completed" for entry in state["entries"])
    assert all(entry["packet"]["latency_ms"] >= 0 for entry in state["entries"])
    assert stat.S_IMODE((root / "attempts.json").stat().st_mode) == 0o600

    def forbidden(request):
        raise AssertionError("completed restart must not request again")

    assert run_http(http_setup, httpx.MockTransport(forbidden)) == result
    assert (root / "attempts.json").read_bytes() == before


@pytest.mark.parametrize("change,reason", [
    ({"usage": None}, "usage_unknown"),
    ({"usage": {}}, "usage_unknown"),
    ({"usage": {"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0}}, "usage_unknown"),
    ({"usage": {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2}}, "usage_unknown"),
    ({"usage": {"prompt_tokens": 1, "completion_tokens": 513, "total_tokens": 514}}, "usage_unknown"),
    ({"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 3}}, "usage_unknown"),
    ({"model": "another-model"}, "model_mismatch"),
    ({"choices": []}, "partial_or_ambiguous"),
    ({"choices": [{}, {}]}, "partial_or_ambiguous"),
    ({"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": "{}"}}]}, "partial_or_ambiguous"),
    ({"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "{"}}]}, "malformed_candidate"),
])
def test_fake_http_invalid_generation_stops_without_correction(http_setup, change, reason):
    manifest, recordings, root, _ = http_setup
    transport = http_transport(manifest, recordings, generation_change=lambda doc: doc | change)
    result = run_http(http_setup, transport)
    assert result["status"] == "stopped" and reason in result["stop_reason"]
    assert result["consumed_generation"] == 1
    assert len(json.loads((root / "attempts.json").read_text())["entries"]) == 2
    assert run_http(http_setup, transport)["consumed_generation"] == 1


@pytest.mark.parametrize("model", [{"id": "unknown"}, {"id": "qwen3.5-think", "status": "unloaded"},
                                   {"id": "qwen3.5-think", "status": "loaded", "synthetic_capacity_reserved": False}])
def test_fake_http_unknown_discovery_never_generates(http_setup, model):
    manifest, recordings, _, _ = http_setup
    result = run_http(http_setup, http_transport(manifest, recordings,
                      discovery_change=lambda _: {"data": [model]}))
    assert result["status"] == "stopped"
    assert result["consumed_discovery"] == 1 and result["consumed_generation"] == 0


@pytest.mark.parametrize("mode", ["blocked", "trickle"])
def test_fake_http_hard_deadline_preserves_ambiguous_attempt(http_setup, mode):
    import httpx
    import time

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(100):
                time.sleep(0.03)
                yield b" "

    def handler(request):
        if mode == "blocked":
            time.sleep(5)
        return httpx.Response(200, stream=SlowStream())

    started = time.monotonic()
    result = run_http(http_setup, httpx.MockTransport(handler), deadline_seconds=0.1)
    assert time.monotonic() - started < 2
    assert result["stop_reason"] == "wall_deadline_ambiguous_exposure"
    assert result["consumed_discovery"] == 1 and result["consumed_generation"] == 0
    state = json.loads((http_setup[2] / "attempts.json").read_text())
    assert state["entries"][0]["status"] == "consumed_uncertain"
    restarted = run_http(http_setup)
    assert restarted["stop_reason"] == "consumed_attempt_not_reconciled"
    assert restarted["consumed_discovery"] == 1


@pytest.mark.parametrize("failure,reason", [("oversized", "response_too_large"),
                                            ("compressed", "compressed_response_refused"),
                                            ("malformed", "transport_or_response_error"),
                                            ("redirect", "HTTP_status_error"),
                                            ("transport", "transport_or_response_error")])
def test_fake_http_wire_failures_are_consumed_and_never_retried(http_setup, failure, reason):
    import httpx

    def handler(request):
        if failure == "transport":
            raise httpx.ReadTimeout("Bearer secret-that-must-not-be-persisted")
        if failure == "oversized":
            return httpx.Response(200, content=b"x" * (spike.LIMITS["response_bytes"] + 1))
        if failure == "compressed":
            return httpx.Response(200, headers={"content-encoding": "gzip"},
                                  stream=httpx.ByteStream(b"garbage"))
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://example.invalid"})
        return httpx.Response(200, content=b"{")

    result = run_http(http_setup, httpx.MockTransport(handler))
    assert result["stop_reason"] == reason and result["consumed_discovery"] == 1
    assert result["consumed_generation"] == 0
    persisted = (http_setup[2] / "attempts.json").read_text()
    assert "secret-that-must-not-be-persisted" not in persisted
    assert run_http(http_setup)["stop_reason"] == "consumed_attempt_not_reconciled"


def test_fake_http_sanitizes_nested_metadata_and_prompts(http_setup):
    manifest, recordings, root, _ = http_setup
    manifest["cases"][0]["prompt"] += " Authorization: Bearer " + spike.SYNTHETIC_TOKEN

    def secrets(document):
        return document | {"authorization": "opaque-secret", "metadata": {
            "api_key": "another-opaque-secret", "token": "standalone-token-secret",
            "token_file": "/private/credential-reference", "note": "Bearer secret-echo " + spike.SYNTHETIC_TOKEN}}

    result = run_http(http_setup, http_transport(manifest, recordings,
                      generation_change=secrets, discovery_change=secrets))
    assert result["status"] == "prepared"
    persisted = (root / "attempts.json").read_text()
    for secret in ("opaque-secret", "another-opaque-secret", "standalone-token-secret",
                   "/private/credential-reference", "secret-echo", spike.SYNTHETIC_TOKEN):
        assert secret not in persisted
    assert "[redacted]" in persisted and '"usage"' in persisted


def test_fake_http_second_defect_stops_later_cases(http_setup):
    manifest, recordings, _, _ = http_setup
    recordings["cases"]["utf8-clip"][1] = deepcopy(recordings["cases"]["utf8-clip"][0])
    result = run_http(http_setup)
    assert result["stop_reason"] == "second_defective_proposal"
    assert result["consumed_generation"] == 2
    assert [row["case_id"] for row in result["cases"]] == ["utf8-clip", "utf8-clip"]


@pytest.mark.parametrize("tokens", [None, True, 0, 2049])
def test_fake_http_unknown_tokenization_never_reserves_generation(http_setup, tokens):
    result = run_http(http_setup, count_tokens=lambda _: tokens)
    assert result["stop_reason"] == "tokenization_unknown_or_input_budget_exceeded"
    assert result["consumed_discovery"] == 1 and result["consumed_generation"] == 0


def test_fake_http_rejects_real_transport_and_live_authority_before_journal(http_setup):
    import httpx
    manifest, recordings, root, authority = http_setup
    with pytest.raises(spike.SpikeError, match="only_explicit_fake"):
        spike.run_fake_http(manifest, recordings, object(), root,
                            authority=authority, count_tokens=lambda _: 1)
    with pytest.raises(spike.SpikeError, match="explicit_synthetic_authority"):
        spike.run_fake_http(manifest, recordings, httpx.MockTransport(lambda _: None), root,
                            authority=authority | {"synthetic": False}, count_tokens=lambda _: 1)
    assert list(root.iterdir()) == []


def test_fake_http_expired_authority_consumes_no_request(http_setup):
    http_setup[3]["expires_at"] = 1
    result = run_http(http_setup)
    assert result["stop_reason"] == "synthetic_authority_expired"
    assert result["consumed_discovery"] == result["consumed_generation"] == 0


def test_fake_http_changed_manifest_cannot_reuse_journal(http_setup):
    assert run_http(http_setup)["status"] == "prepared"
    http_setup[0]["cases"][0]["prompt"] += " changed"
    with pytest.raises(spike.SpikeError, match="journal_identity_changed"):
        run_http(http_setup)


@pytest.mark.parametrize("removed", ["attempts.json", "attempts.lock"])
def test_fake_http_missing_durable_state_never_renews_budget(http_setup, removed):
    assert run_http(http_setup)["status"] == "prepared"
    (http_setup[2] / removed).unlink()
    with pytest.raises((OSError, spike.SpikeError)):
        run_http(http_setup)


def test_fake_http_reservation_survives_crash_before_transport(http_setup, monkeypatch):
    def interrupted(*args, **kwargs):
        raise SystemExit("simulated process interruption")

    monkeypatch.setattr(spike, "fake_http_request", interrupted)
    with pytest.raises(SystemExit):
        run_http(http_setup)
    state = json.loads((http_setup[2] / "attempts.json").read_text())
    assert len(state["entries"]) == 1 and state["entries"][0]["status"] == "consumed_uncertain"
    assert run_http(http_setup)["stop_reason"] == "consumed_attempt_not_reconciled"


def test_fake_http_private_journal_lock_blocks_concurrent_run(http_setup):
    import fcntl
    import os
    root = http_setup[2]
    lock = os.open(root / "attempts.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises((BlockingIOError, spike.SpikeError)):
            run_http(http_setup)
        assert not (root / "attempts.json").exists()
    finally:
        os.close(lock)


@pytest.mark.parametrize("kind", ["discovery", "generation", "case"])
def test_fake_http_journal_enforces_consumed_budget_before_transport(http_setup, monkeypatch, kind):
    run_http(http_setup)
    root = http_setup[2]
    identity = json.loads((root / "attempts.json").read_text())["identity"]
    journal = spike.FakeHTTPJournal(root, identity)
    try:
        entries = journal.state["entries"]
        request = deepcopy(entries[0 if kind == "discovery" else 1]["request"])
        if kind == "generation":
            entries.append(deepcopy(entries[1]))
            request["case_id"] = "interval-review"
        journal.cursor = len(entries)

        def forbidden(*args, **kwargs):
            raise AssertionError("exhausted budgets must not request")

        monkeypatch.setattr(spike, "fake_http_request", forbidden)
        with pytest.raises(spike.SpikeError, match="budget_exhausted"):
            journal.obtain(request, object(), deadline_seconds=60)
    finally:
        journal.close()


@pytest.mark.parametrize("failed_save", [2, 3])
def test_fake_http_failed_reservation_or_receipt_write_preserves_effect_boundary(
        http_setup, monkeypatch, failed_save):
    real_save = spike.FakeHTTPJournal.save
    writes, effects = [], []

    def save(journal):
        writes.append(len(journal.state["entries"]))
        if len(writes) == failed_save:
            raise OSError("simulated durable write failure")
        return real_save(journal)

    def request(*args, **kwargs):
        effects.append(True)
        return {"latency_ms": 1, "response": {"data": [{"id": spike.HTTP_MODEL,
                "status": "loaded", "synthetic_capacity_reserved": True}]}}

    monkeypatch.setattr(spike.FakeHTTPJournal, "save", save)
    monkeypatch.setattr(spike, "fake_http_request", request)
    with pytest.raises(OSError, match="durable write failure"):
        run_http(http_setup)
    state = json.loads((http_setup[2] / "attempts.json").read_text())
    if failed_save == 2:
        assert effects == [] and state["entries"] == []
    else:
        assert effects == [True]
        assert state["entries"][0]["status"] == "consumed_uncertain"
        monkeypatch.setattr(spike.FakeHTTPJournal, "save", real_save)
        assert run_http(http_setup)["stop_reason"] == "consumed_attempt_not_reconciled"
        assert effects == [True]


def test_fake_http_prompt_envelope_cap_stops_before_generation(http_setup):
    http_setup[0]["cases"][0]["prompt"] = "x" * spike.LIMITS["prompt_bytes"]
    result = run_http(http_setup)
    assert result["stop_reason"] == "prompt_budget_exceeded"
    assert result["consumed_discovery"] == 1 and result["consumed_generation"] == 0


def test_live_cli_refuses_before_file_or_client_access(monkeypatch, capsys):
    import httpx

    def forbidden(*args, **kwargs):
        raise AssertionError("live refusal must precede credentials, files and client construction")

    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(spike, "fake_http_request", forbidden)
    assert spike.main(["--live", "--cases", "/private/nonexistent"]) == 2
    assert "Live mode is disabled" in capsys.readouterr().out


def test_fake_http_rejects_live_admission_before_file_or_client_access(http_setup, monkeypatch):
    import httpx
    manifest, recordings, root, authority = http_setup

    def forbidden(*args, **kwargs):
        raise AssertionError("live admission must not inspect files or construct a client")

    transport = httpx.MockTransport(forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    with pytest.raises(spike.SpikeError, match="explicit_synthetic_authority"):
        spike.run_fake_http(manifest, recordings, transport, root,
                            authority=authority | {"synthetic": False}, count_tokens=lambda _: 1)
    with pytest.raises(spike.SpikeError, match="live_disabled"):
        spike.live_admission(authority=authority)


def test_fake_http_discovery_endpoint_error_stops_even_with_model_metadata(http_setup):
    manifest, recordings, _, _ = http_setup
    transport = http_transport(manifest, recordings,
                              discovery_change=lambda doc: doc | {"error": {"message": "endpoint unavailable"}})
    result = run_http(http_setup, transport)
    assert result["status"] == "stopped"
    assert result["consumed_discovery"] == 1 and result["consumed_generation"] == 0
