"""Offline qualification tests: synthetic replies, no endpoint requests or secrets."""

import asyncio
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts import brodson_qualification as q
from scripts import brodson_spike as spike


TEMPLATE = "Reviewed time-independent synthetic template"
BUILD = "b11371-99b95488c"


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def models(args=None, status="loaded"):
    return {"data": [{"id": q.MODEL, "aliases": [q.MODEL], "meta": {"n_ctx": 16384},
                      "status": {"value": status, "args": args if args is not None else
                                 ["llama-server", "--sleep-idle-seconds", "-1", "--api-key", "never-record-this"]}}]}


def props():
    return {"model_alias": q.MODEL, "build_info": BUILD, "is_sleeping": False,
            "chat_template": TEMPLATE, "total_slots": 4, "default_generation_settings": {"n_ctx": 16384}}


def completion(content, *, prompt=123, completion=30, **changes):
    return {"model": q.MODEL, "system_fingerprint": BUILD,
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                      "total_tokens": prompt + completion, "prompt_tokens_details": {"cached_tokens": prompt}}, **changes}


class FakeTransport:
    def __init__(self, path, replies):
        self.path, self.replies = path, list(replies)
        self.calls = []

    async def request(self, method, path, body, *, seconds, byte_limit):
        # The actual durable file, not in-memory state, must precede every effect.
        state = json.loads(self.path.read_bytes())
        assert state["attempts"][-1]["status"] == "pending"
        assert state["attempts"][-1]["request_sha256"] == q.digest(body or b"")
        assert state["reserved_tokens"] == sum(row["reserved_tokens"] for row in state["attempts"])
        self.calls.append((method, path, body, seconds, byte_limit))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, bytes) else q.encode(reply)


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "run.json"
    clock = Clock()
    auth = {"run_id": "qualification-011", "reviewed_head": "a" * 40,
            "rest_assignment_sha256": "b" * 64, "state_dir": str(tmp_path),
            "env_file": "/private/.env", "zero_charge_profile": q.PROFILE, "limits": q.LIMITS.copy(),
            "phase": "metadata", "operator_approval_id": "explicit-metadata-approval",
            "approved_build_info": None, "template_sha256": None, "metadata_sha256": None}
    manifest = spike.read_json(spike.FIXTURES / "cases.json")
    transport = FakeTransport(path, [models(), props()])
    def runner(selected=auth, selected_transport=transport, **kwargs):
        return q.Runner(path, selected, selected_transport, deadline=2000, boot_id="same-boot",
                        clock=clock, monotonic=clock, **kwargs)
    return path, clock, auth, manifest, transport, runner


def metadata_then_auth(setup):
    path, clock, auth, manifest, transport, runner = setup
    state = asyncio.run(runner().run(manifest))
    assert state["status"] == "metadata_ready"
    return auth | {"phase": "spike", "operator_approval_id": "separate-spike-approval",
                   "approved_build_info": BUILD, "metadata_sha256": state["metadata_sha256"],
                   "template_sha256": q.digest(TEMPLATE.encode()), "deterministic_template_reviewed": True,
                   "profile_unchanged_since_metadata": True}


def good_replies():
    recordings = spike.read_json(spike.FIXTURES / "recorded-responses.json")
    replies = []
    for case_id in spike.SOURCES:
        content = recordings["cases"][case_id][-1]["content"]
        replies.extend([{"object": "response.input_tokens", "input_tokens": 123}, completion(content)])
    return replies


def test_metadata_always_stops_before_count_and_sanitizes_args(setup):
    path, _, _, manifest, transport, runner = setup
    state = asyncio.run(runner().run(manifest))
    assert state["status"] == "metadata_ready" and len(transport.calls) == 2
    assert "never-record-this" not in path.read_text()
    assert state["reserved_tokens"] == 0
    assert path.stat().st_mode & 0o777 == 0o600
    again = asyncio.run(runner().run(manifest))
    assert again == state and len(transport.calls) == 2


def test_full_spike_uses_same_body_for_count_and_generation_and_retains_reservations(setup):
    path, _, _, manifest, transport, runner = setup
    auth = metadata_then_auth(setup)
    transport.replies.extend(good_replies())
    state = asyncio.run(runner(auth).run(manifest))
    assert state["status"] == "complete" and len(state["attempts"]) == 8
    assert state["reserved_tokens"] == 3 * (123 + 512)
    assert state["returned_tokens"] == 3 * (123 + 30)  # Cached input is still counted.
    for count, generation in zip(transport.calls[2::2], transport.calls[3::2]):
        assert count[2] == generation[2]
        assert count[1].endswith("input_tokens?autoload=false")
        assert generation[1].endswith("completions?autoload=false")
        body = json.loads(count[2])
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert body["max_tokens"] == 512 and body["n"] == 1
    asyncio.run(runner(auth).run(manifest))
    assert len(transport.calls) == 8


def test_only_semantic_failure_earns_one_correction(setup):
    _, _, _, manifest, transport, runner = setup
    auth = metadata_then_auth(setup)
    bad = completion('{"replacement":"text[:byte_limit]"}')
    transport.replies.extend([{"object": "response.input_tokens", "input_tokens": 123}, bad] + good_replies())
    state = asyncio.run(runner(auth).run(manifest))
    assert state["status"] == "complete" and len(transport.calls) == 10
    correction = json.loads(transport.calls[4][2])
    assert correction["messages"][1]["content"] == bad["choices"][0]["message"]["content"]
    assert correction["messages"][2]["content"].startswith("Independent validation failed:")
    assert state["cases"][0]["attempts"][0]["accepted"] is False
    assert state["cases"][0]["attempts"][1]["accepted"] is True


@pytest.mark.parametrize("args", [[], ["--sleep-idle-seconds", "1"], ["--sleep-idle-seconds=-1"],
                                  ["--sleep-idle-seconds", "-1", "--sleep-idle-seconds", "-1"]])
def test_absent_ambiguous_or_noncanonical_sleep_setting_blocks_count(setup, args):
    _, _, _, manifest, transport, runner = setup
    transport.replies[0] = models(args=args)
    auth = metadata_then_auth(setup)
    state = asyncio.run(runner(auth).run(manifest))
    assert state["status"] == "stopped" and len(transport.calls) == 2
    assert state["stop_reason"] == "loaded_target_and_explicit_disabled_sleep_required"


@pytest.mark.parametrize("status", ["unloaded", "sleeping", "loading", "downloading", None])
def test_nonloaded_target_never_reaches_props_or_count(setup, status):
    _, _, _, manifest, transport, runner = setup
    transport.replies[0] = models(status=status)
    assert asyncio.run(runner().run(manifest))["status"] == "stopped"
    assert len(transport.calls) == 1


@pytest.mark.parametrize("field,value", [("build_info", "b99999-unknown"), ("is_sleeping", True),
                                        ("model_alias", "another-model")])
def test_unknown_or_changed_profile_collects_metadata_then_blocks(setup, field, value):
    _, _, _, manifest, transport, runner = setup
    transport.replies[1] = props() | {field: value}
    auth = metadata_then_auth(setup)
    state = asyncio.run(runner(auth).run(manifest))
    assert state["status"] == "stopped" and len(transport.calls) == 2


@pytest.mark.parametrize("change", [{"template_sha256": "0" * 64}, {"metadata_sha256": "0" * 64},
                                   {"approved_build_info": "b99999-99b95488c"}])
def test_review_binding_mismatch_blocks_count(setup, change):
    _, _, _, manifest, transport, runner = setup
    auth = metadata_then_auth(setup) | change
    state = asyncio.run(runner(auth).run(manifest))
    assert state["status"] == "stopped" and len(transport.calls) == 2


def test_stale_metadata_does_not_rediscover_or_reset_budget(setup):
    _, clock, _, manifest, transport, runner = setup
    auth = metadata_then_auth(setup)
    clock.now += 1801
    state = asyncio.run(runner(auth).run(manifest))
    assert state["stop_reason"] == "metadata_stale_new_authorization_required"
    assert len(transport.calls) == 2


def test_metadata_expiring_during_count_prevents_generation(setup):
    _, clock, _, manifest, transport, runner = setup
    auth = metadata_then_auth(setup)
    transport.replies.append({"object": "response.input_tokens", "input_tokens": 123})
    original = transport.request
    async def delayed_count(*args, **kwargs):
        result = await original(*args, **kwargs)
        clock.now += 1801
        return result
    transport.request = delayed_count
    state = asyncio.run(runner(auth).run(manifest))
    assert state["stop_reason"] == "metadata_stale_new_authorization_required" and len(transport.calls) == 3
    assert state["reserved_tokens"] == 0


def test_approval_rollback_cannot_extend_original_monotonic_endpoint(setup):
    path, clock, auth, manifest, transport, _ = setup
    first = q.Runner(path, auth, transport, deadline=102, boot_id="same-boot", clock=lambda: 100,
                     monotonic=clock, approval_monotonic_limit=102)
    asyncio.run(first.run(manifest))
    clock.now = 103
    second = q.Runner(path, auth, transport, deadline=102, boot_id="same-boot", clock=lambda: 1,
                      monotonic=clock)
    with pytest.raises(q.QualificationError, match="approval_expired"):
        asyncio.run(second.run(manifest))
    assert len(transport.calls) == 2


def test_context_metadata_must_accommodate_reserved_maximum(setup):
    _, _, _, manifest, transport, runner = setup
    transport.replies[1] = props() | {"default_generation_settings": {"n_ctx": 2048}}
    auth = metadata_then_auth(setup)
    state = asyncio.run(runner(auth).run(manifest))
    assert state["stop_reason"] == "reported_context_or_slot_limit_unqualified" and len(transport.calls) == 2


@pytest.mark.parametrize("reply", [b"not-json", b"x" * 4097, {"input_tokens": 123},
                                  {"object": "response.input_tokens", "input_tokens": True},
                                  {"object": "response.input_tokens", "input_tokens": 2049}])
def test_invalid_count_never_generates(setup, reply):
    _, _, _, manifest, transport, runner = setup
    auth = metadata_then_auth(setup)
    transport.replies.append(reply)
    state = asyncio.run(runner(auth).run(manifest))
    assert state["status"] == "stopped" and len(transport.calls) == 3
    assert state["reserved_tokens"] == 0


@pytest.mark.parametrize("kind", ["malformed", "missing-usage", "count-mismatch", "truncated", "wrong-model", "wrong-build", "scalar"])
def test_protocol_failures_stop_without_correction(setup, kind):
    _, _, _, manifest, transport, runner = setup
    auth = metadata_then_auth(setup)
    reply = good_replies()[1]
    if kind == "malformed":
        reply["choices"][0]["message"]["content"] = "not-json"
    elif kind == "scalar":
        reply["choices"][0]["message"]["content"] = "null"
    elif kind == "missing-usage":
        del reply["usage"]
    elif kind == "count-mismatch":
        reply["usage"]["prompt_tokens"] += 1
    elif kind == "truncated":
        reply["choices"][0]["finish_reason"] = "length"
    elif kind == "wrong-model":
        reply["model"] = "other"
    else:
        reply["system_fingerprint"] = "b99999-99b95488c"
    transport.replies.extend([{"object": "response.input_tokens", "input_tokens": 123}, reply])
    state = asyncio.run(runner(auth).run(manifest))
    assert state["status"] == "stopped" and len(transport.calls) == 4
    assert state["reserved_tokens"] == 635


def test_pending_generation_survives_crash_without_redispatch(setup):
    class Crash(BaseException):
        pass
    path, _, _, manifest, transport, runner = setup
    auth = metadata_then_auth(setup)
    transport.replies.extend([{"object": "response.input_tokens", "input_tokens": 123}, Crash()])
    with pytest.raises(Crash):
        asyncio.run(runner(auth).run(manifest))
    saved = json.loads(path.read_bytes())
    assert saved["attempts"][-1]["status"] == "pending" and saved["reserved_tokens"] == 635
    with pytest.raises(q.QualificationError, match="uncertain_attempt"):
        asyncio.run(runner(auth).run(manifest))
    assert len(transport.calls) == 4 and json.loads(path.read_bytes()) == saved


def test_crash_between_completed_metadata_requests_never_repeats_get(setup, monkeypatch):
    path, _, _, manifest, transport, runner = setup
    original = q.persist
    class Crash(BaseException):
        pass
    def interrupted(path, state):
        original(path, state)
        if len(state["attempts"]) == 1 and state["attempts"][0]["status"] == "complete":
            raise Crash()
    monkeypatch.setattr(q, "persist", interrupted)
    with pytest.raises(Crash):
        asyncio.run(runner().run(manifest))
    monkeypatch.setattr(q, "persist", original)
    with pytest.raises(q.QualificationError, match="interrupted_phase"):
        asyncio.run(runner().run(manifest))
    assert len(transport.calls) == 1


def test_failed_durable_intent_prevents_transport(setup, monkeypatch):
    _, _, _, manifest, transport, runner = setup
    original = q.persist
    def fail_intent(path, state):
        if state["attempts"]:
            raise OSError("synthetic fsync failure")
        original(path, state)
    monkeypatch.setattr(q, "persist", fail_intent)
    with pytest.raises(OSError):
        asyncio.run(runner().run(manifest))
    assert not transport.calls


def test_hard_wall_timeout_retains_pending_exposure(setup):
    path, _, _, manifest, transport, runner = setup
    auth = metadata_then_auth(setup)
    async def timeout(*_args, **_kwargs):
        await asyncio.sleep(1)
    transport.request = timeout
    original = q.LIMITS["count_seconds"]
    try:
        q.LIMITS["count_seconds"] = 0.01
        auth["limits"]["count_seconds"] = 0.01
        saved = json.loads(path.read_bytes())
        saved["binding"]["limits"]["count_seconds"] = 0.01
        q.persist(path, saved)
        state = asyncio.run(runner(auth).run(manifest))
    finally:
        q.LIMITS["count_seconds"] = original
    assert state["status"] == "stopped" and state["attempts"][-1]["status"] == "pending"


def test_restart_cannot_change_boot_or_approval(setup):
    path, clock, _, manifest, transport, runner = setup
    auth = metadata_then_auth(setup)
    for kwargs in ({"boot_id": "new-boot", "deadline": 2000}, {"boot_id": "same-boot", "deadline": 2001}):
        other = q.Runner(path, auth, transport, clock=clock, monotonic=clock, **kwargs)
        with pytest.raises(q.QualificationError):
            asyncio.run(other.run(manifest))
    assert len(transport.calls) == 2


def test_expiry_after_intent_never_sends(setup, monkeypatch):
    _, clock, _, manifest, transport, runner = setup
    original = q.persist
    def expire_after_intent(path, state):
        original(path, state)
        if state["attempts"]:
            clock.now = 2001
    monkeypatch.setattr(q, "persist", expire_after_intent)
    state = asyncio.run(runner().run(manifest))
    assert state["status"] == "stopped" and not transport.calls
    assert state["attempts"][0]["status"] == "pending"


def test_six_proposals_and_corrections_use_exact_fourteen_attempt_cap(setup):
    _, _, _, manifest, transport, runner = setup
    auth = metadata_then_auth(setup)
    good = good_replies()
    for index in range(3):
        transport.replies.extend([{"object": "response.input_tokens", "input_tokens": 2048},
                                  completion("{}", prompt=2048),
                                  {"object": "response.input_tokens", "input_tokens": 2048},
                                  completion(good[index * 2 + 1]["choices"][0]["message"]["content"], prompt=2048)])
    state = asyncio.run(runner(auth).run(manifest))
    assert state["status"] == "complete" and len(transport.calls) == 14
    assert state["reserved_tokens"] == 15360
    assert all(len(case["attempts"]) == 2 for case in state["cases"])


def test_second_defective_proposal_stops_remaining_cases(setup):
    _, _, _, manifest, transport, runner = setup
    auth = metadata_then_auth(setup)
    transport.replies.extend([{"object": "response.input_tokens", "input_tokens": 123}, completion("{}")] * 2)
    state = asyncio.run(runner(auth).run(manifest))
    assert state["stop_reason"] == "second_defective_proposal" and len(transport.calls) == 6
    assert len(state["cases"]) == 1


@pytest.fixture
def operator_files(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args]).decode().strip()
    git("init", "--quiet")
    git("config", "user.name", "Offline Test")
    git("config", "user.email", "offline@example.invalid")
    git("remote", "add", "origin", "https://github.com/stonesky-ai/skybuild.git")
    brief = json.loads((q.ROOT / q.BRIEF).read_bytes())
    path = repo / q.BRIEF
    path.parent.mkdir(parents=True)
    path.write_bytes(q.encode(brief))
    (repo / "docs/design/architecture.md").write_text("Fixture architecture")
    git("add", ".")
    git("commit", "--quiet", "-m", "Pinned fixture")
    head = git("rev-parse", "HEAD")
    assignment = {key: value for key, value in brief.items() if key not in {"schema", "next_action"}}
    assignment.update(schema="manual-work-v1", base_sha=head, brief_path=q.BRIEF, brief_sha256=q.digest(path.read_bytes()))
    auth = {"schema": "brodson-operator-authorization-v1", "run_id": "offline-011", "phase": "metadata",
            "reviewed_head": head, "review_artifact_sha256": "a" * 64,
            "rest_assignment_sha256": q.digest(q.encode(assignment)), "rest_receipt_id": "receipt-011",
            "operator_approval_id": "explicit-parent-clearance", "approved_until": "2026-10-09T15:20:53Z",
            "zero_charge_profile": q.PROFILE, "state_dir": str(tmp_path / "private-state"),
            "env_file": str(tmp_path / ".env"), "limits": q.LIMITS.copy(), "metadata_sha256": None,
            "template_sha256": None, "deterministic_template_reviewed": False,
            "profile_unchanged_since_metadata": False, "approved_build_info": None}
    return repo, assignment, auth


@pytest.mark.parametrize("change", [{"phase": "live"}, {"limits": {}}, {"reviewed_head": "0" * 40},
                                   {"rest_assignment_sha256": "0" * 64}, {"zero_charge_profile": "paid"},
                                   {"approved_until": "2000-01-01T00:00:00Z"},
                                   {"approved_until": "2026-10-09T15:20:53"}, {"phase": "spike"}])
def test_explicit_operator_gate_fails_closed(operator_files, change):
    repo, assignment, auth = operator_files
    with pytest.raises(q.QualificationError):
        q.validate_authorization(auth | change, assignment, repo,
                                 now=datetime(2026, 10, 9, 8, tzinfo=timezone.utc).timestamp())


def test_operator_gate_accepts_only_clean_reviewed_source(operator_files):
    repo, assignment, auth = operator_files
    assert q.validate_authorization(auth, assignment, repo, now=100) > 100
    (repo / "unreviewed.py").write_text("pass")
    with pytest.raises(q.QualificationError, match="clean_reviewed"):
        q.validate_authorization(auth, assignment, repo, now=100)


def test_cli_without_authorization_never_reads_profile_or_constructs_transport(tmp_path, monkeypatch, capsys):
    def forbidden(*_args, **_kwargs):
        pytest.fail("No transport before explicit authorization")
    monkeypatch.setattr(q, "Transport", forbidden)
    assert q.main(["--authorization", str(tmp_path / "missing"), "--assignment", str(tmp_path / "missing")]) == 2
    assert "blocked" in capsys.readouterr().out


@pytest.mark.parametrize("locked", [False, True])
def test_cli_validated_metadata_invocation_and_process_lock(operator_files, tmp_path, monkeypatch, locked):
    repo, assignment, auth = operator_files
    auth_file, assignment_file = tmp_path / "authorization.json", tmp_path / "assignment.json"
    for path, value in ((auth_file, auth), (assignment_file, assignment)):
        path.write_bytes(q.encode(value))
        path.chmod(0o600)
    state_dir = Path(auth["state_dir"])
    state_dir.mkdir(mode=0o700)
    transport = FakeTransport(state_dir / "run.json", [models(), props()])
    def factory(*_args):
        return transport
    monkeypatch.setattr(q, "ROOT", repo)
    monkeypatch.setattr(q, "Transport", factory)
    monkeypatch.setattr(q.time, "time", lambda: 100.0)
    descriptor = None
    try:
        if locked:
            descriptor = os.open(state_dir / "run.lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = q.main(["--authorization", str(auth_file), "--assignment", str(assignment_file)])
    finally:
        if descriptor is not None:
            os.close(descriptor)
    assert result == (2 if locked else 0)
    assert len(transport.calls) == (0 if locked else 2)


@pytest.mark.parametrize("status,encoding,payload,reason", [
    (302, "identity", b"{}", "http_status_302"),
    (200, "gzip", b"{}", "compressed_response"),
    (200, "identity", b"x" * 5000, "response_too_large"),
    (200, "identity", b"s" * 32, "secret_echo"),
])
def test_http_transport_boundaries_are_offline(tmp_path, monkeypatch, status, encoding, payload, reason):
    secret = tmp_path / "secret"
    secret.write_bytes(b"s" * 32)
    secret.chmod(0o600)
    env = tmp_path / ".env"
    env.write_text(f"SKYBUILD_INFERENCE_BASE_URL={q.ORIGIN}\nSKYBUILD_INFERENCE_SECRET_FILE={secret}\n")
    env.chmod(0o600)
    class Response:
        status_code = status
        headers = {"content-encoding": encoding}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            pass
        async def aiter_raw(self, *, chunk_size):
            assert chunk_size == 4096
            yield payload
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            pass
        def stream(self, method, url, **kwargs):
            assert method == "GET" and url == q.ORIGIN + "/models"
            return Response()
    monkeypatch.setattr(q.httpx, "AsyncClient", Client)
    with pytest.raises(q.QualificationError, match=reason):
        asyncio.run(q.Transport(env).request("GET", "/models", None, seconds=1, byte_limit=4096))


@pytest.mark.parametrize("stage", ["metadata", "generation-intent", "complete"])
@pytest.mark.parametrize("lost", ["state", "marker"])
def test_loss_of_either_durable_file_never_resets_budget(setup, stage, lost):
    path, _, original_auth, manifest, transport, runner = setup
    auth = metadata_then_auth(setup)
    if stage == "generation-intent":
        class Crash(BaseException):
            pass
        transport.replies.extend([{"object": "response.input_tokens", "input_tokens": 123}, Crash()])
        with pytest.raises(Crash):
            asyncio.run(runner(auth).run(manifest))
    elif stage == "complete":
        transport.replies.extend(good_replies())
        asyncio.run(runner(auth).run(manifest))
    before = len(transport.calls)
    (path if lost == "state" else path.with_name("run.lock")).unlink()
    # Repeating metadata authorization must not recreate the original budget.
    for _ in range(2):
        with pytest.raises(q.QualificationError, match="missing_run"):
            asyncio.run(runner(original_auth).run(manifest))
    assert len(transport.calls) == before


def test_crash_after_marker_before_first_state_is_not_recoverable(setup, monkeypatch):
    path, _, _, manifest, transport, runner = setup
    original = q.persist
    def interrupted(*_args):
        raise OSError("synthetic first-state failure")
    monkeypatch.setattr(q, "persist", interrupted)
    with pytest.raises(OSError):
        asyncio.run(runner().run(manifest))
    marker = json.loads(path.with_name("run.lock").read_bytes())
    assert marker["schema"] == "brodson-run-marker-v1" and not path.exists()
    monkeypatch.setattr(q, "persist", original)
    with pytest.raises(q.QualificationError, match="missing_run_state"):
        asyncio.run(runner().run(manifest))
    assert not transport.calls


@pytest.mark.parametrize("failed_sync", [1, 2])
def test_marker_file_or_directory_sync_failure_blocks_reinitialization(setup, monkeypatch, failed_sync):
    path, _, _, manifest, transport, runner = setup
    original = q.os.fsync
    count = 0
    def fail_sync(fd):
        nonlocal count
        count += 1
        if count == failed_sync:
            raise OSError("synthetic marker durability failure")
        return original(fd)
    monkeypatch.setattr(q.os, "fsync", fail_sync)
    with pytest.raises(OSError):
        asyncio.run(runner().run(manifest))
    monkeypatch.setattr(q.os, "fsync", original)
    assert path.with_name("run.lock").exists() and not path.exists()
    with pytest.raises(q.QualificationError, match="missing_run_state"):
        asyncio.run(runner().run(manifest))
    assert not transport.calls


@pytest.mark.parametrize("marker", [b"", b"{", b"{}"])
def test_incomplete_initial_marker_never_starts_a_run(setup, marker):
    path, _, _, manifest, transport, runner = setup
    lock = path.with_name("run.lock")
    lock.write_bytes(marker)
    lock.chmod(0o600)
    with pytest.raises((q.QualificationError, ValueError)):
        asyncio.run(runner().run(manifest))
    assert not path.exists() and not transport.calls


def test_replaced_valid_marker_cannot_resume_original_state(setup):
    path, _, _, manifest, transport, runner = setup
    metadata_then_auth(setup)
    lock = path.with_name("run.lock")
    marker = json.loads(lock.read_bytes())
    marker["marker_id"] = "0" * 32
    lock.write_bytes(q.encode(marker))
    with pytest.raises(q.QualificationError, match="run_binding"):
        asyncio.run(runner().run(manifest))
    assert len(transport.calls) == 2


@pytest.mark.parametrize("deadline", ["2026-10-09T15:20:54Z", "2026-10-10T15:20:53Z"])
def test_cli_cannot_extend_committed_assignment_cutoff(operator_files, tmp_path, monkeypatch, deadline):
    repo, assignment, auth = operator_files
    auth["approved_until"] = deadline
    auth_path, assignment_path = tmp_path / "auth.json", tmp_path / "assignment.json"
    for path, value in ((auth_path, auth), (assignment_path, assignment)):
        path.write_bytes(q.encode(value))
        path.chmod(0o600)
    monkeypatch.setattr(q, "ROOT", repo)
    monkeypatch.setattr(q.time, "time", lambda: 100.0)
    def forbidden(*_args):
        pytest.fail("Expired assignment cannot construct a transport or read credentials")
    monkeypatch.setattr(q, "Transport", forbidden)
    assert q.main(["--authorization", str(auth_path), "--assignment", str(assignment_path)]) == 2
    assert not Path(auth["state_dir"]).exists()


@pytest.mark.parametrize("deadline", ["2026-10-09T15:20:52Z", "2026-10-09T15:20:53Z"])
def test_operator_deadline_can_only_shorten_assignment_window(operator_files, deadline):
    repo, assignment, auth = operator_files
    assert q.validate_authorization(auth | {"approved_until": deadline}, assignment, repo, now=100) > 100
    brief = json.loads((repo / q.BRIEF).read_bytes())
    assert q.ASSIGNMENT_APPROVED_UNTIL in brief["model_limit"]


@pytest.mark.parametrize("stage", ["models", "props", "count", "generation"])
@pytest.mark.parametrize("error", [{"message": "synthetic endpoint failure"}, "synthetic failure", None])
def test_error_envelopes_cannot_hide_behind_success_fields(setup, stage, error):
    path, _, auth, manifest, transport, runner = setup
    expected = {"models": 1, "props": 2, "count": 3, "generation": 4}[stage]
    if stage in {"models", "props"}:
        transport.replies[expected - 1]["error"] = error
    else:
        auth = metadata_then_auth(setup)
        replies = good_replies()[:expected - 2]
        replies[-1]["error"] = error
        transport.replies.extend(replies)
    state = asyncio.run(runner(auth).run(manifest))
    assert state["status"] == "stopped" and state["stop_reason"] == "endpoint_error_or_invalid_envelope"
    assert len(transport.calls) == expected
    assert state["attempts"][-1]["status"] == "pending"
    assert "response" not in state["attempts"][-1]
    assert "synthetic endpoint failure" not in path.read_text()
    with pytest.raises(q.QualificationError, match="uncertain_attempt"):
        asyncio.run(runner(auth).run(manifest))
    assert len(transport.calls) == expected


@pytest.mark.parametrize("stage", ["props", "generation"])
@pytest.mark.parametrize("encoding", ["literal", "unicode", "nested-unicode", "json-string"])
def test_decoded_credential_echo_never_reaches_persisted_evidence(setup, tmp_path, monkeypatch, stage, encoding):
    path, _, auth, manifest, transport, runner = setup
    # This credential is invented exclusively for this offline fixture.
    secret = "synthetic-credential-for-offline-test-011"
    escaped = "".join("\\u%04x" % ord(char) for char in secret)
    value = {"literal": secret, "unicode": secret, "nested-unicode": escaped,
             "json-string": json.dumps({"nested": escaped})}[encoding]
    if stage == "props":
        replies = [q.encode(models()), q.encode(props() | {"chat_template": value})]
    else:
        auth = metadata_then_auth(setup)
        replies = [q.encode({"object": "response.input_tokens", "input_tokens": 123}),
                   q.encode(completion(json.dumps({"replacement": value})))]
    if encoding == "unicode":
        replies[-1] = replies[-1].replace(secret.encode(), escaped.encode())
    secret_path, env = tmp_path / "synthetic-secret", tmp_path / "synthetic.env"
    secret_path.write_text(secret)
    secret_path.chmod(0o600)
    env.write_text(f"SKYBUILD_INFERENCE_BASE_URL={q.ORIGIN}\nSKYBUILD_INFERENCE_SECRET_FILE={secret_path}\n")
    env.chmod(0o600)
    calls = []
    class Response:
        status_code = 200
        headers = {}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            pass
        async def aiter_raw(self, **_kwargs):
            yield replies.pop(0)
    class Client:
        def __init__(self, **_kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            pass
        def stream(self, method, url, **_kwargs):
            calls.append((method, url))
            return Response()
    monkeypatch.setattr(q.httpx, "AsyncClient", Client)
    state = asyncio.run(runner(auth, q.Transport(env)).run(manifest))
    assert state["status"] == "stopped" and state["stop_reason"] == "secret_echo_refused"
    assert len(calls) == 2 and not replies
    assert "response" not in state["attempts"][-1]
    assert "response_sha256" not in state["attempts"][-1]
    saved = path.read_text()
    assert secret not in saved and escaped not in saved and "synthetic-credential" not in saved
    assert state["attempts"][-1]["error"] == "secret_echo_refused"


@pytest.fixture
def wake_operator_files(operator_files):
    repo, _, auth = operator_files
    brief = json.loads((q.ROOT / q.WAKE_BRIEF).read_bytes())
    path = repo / q.WAKE_BRIEF
    path.write_bytes(q.encode(brief))
    subprocess.check_call(["git", "add", "."], cwd=repo)
    subprocess.check_call(["git", "commit", "--quiet", "-m", "Separate wake permission brief"], cwd=repo)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
    assignment = {key: value for key, value in brief.items() if key not in {"schema", "next_action"}}
    assignment.update(schema="manual-work-v1", base_sha=head, brief_path=q.WAKE_BRIEF,
                      brief_sha256=q.digest(path.read_bytes()))
    auth.update(schema="brodson-operator-authorization-v2", reviewed_head=head,
                rest_assignment_sha256=q.digest(q.encode(assignment)), allow_automatic_wake_reload=False)
    return repo, assignment, auth


@pytest.mark.parametrize("permission", [False, True])
def test_v2_requires_new_pinned_brief_and_explicit_boolean(wake_operator_files, permission):
    repo, assignment, auth = wake_operator_files
    auth["allow_automatic_wake_reload"] = permission
    assert q.validate_authorization(auth, assignment, repo, now=100) > 100
    assert q.automatic_wake_allowed(auth) is permission


@pytest.mark.parametrize("permission", [None, 0, 1, "true", "false", [], {}])
def test_v2_rejects_ambiguous_wake_permission(wake_operator_files, permission):
    repo, assignment, auth = wake_operator_files
    auth["allow_automatic_wake_reload"] = permission
    with pytest.raises(q.QualificationError, match="explicit_boolean"):
        q.validate_authorization(auth, assignment, repo, now=100)


def test_v2_missing_permission_fails_closed(wake_operator_files):
    repo, assignment, auth = wake_operator_files
    del auth["allow_automatic_wake_reload"]
    with pytest.raises(q.QualificationError, match="explicit_boolean"):
        q.validate_authorization(auth, assignment, repo, now=100)


def test_old_brief_never_authorizes_wake(operator_files):
    repo, assignment, auth = operator_files
    with pytest.raises(q.QualificationError, match="explicit_operator"):
        q.validate_authorization(auth | {"allow_automatic_wake_reload": True}, assignment, repo, now=100)
    with pytest.raises(q.QualificationError, match="wrong_rest_assignment"):
        q.validate_authorization(auth | {"schema": "brodson-operator-authorization-v2",
                                        "allow_automatic_wake_reload": True}, assignment, repo, now=100)


def test_new_brief_does_not_reinterpret_v1_authorization(wake_operator_files):
    repo, assignment, auth = wake_operator_files
    auth["schema"] = "brodson-operator-authorization-v1"
    del auth["allow_automatic_wake_reload"]
    with pytest.raises(q.QualificationError, match="wrong_rest_assignment"):
        q.validate_authorization(auth, assignment, repo, now=100)


@pytest.mark.parametrize("status,sleeping", [("loaded", False), ("sleeping", True)])
def test_explicit_wake_permission_admits_existing_child_without_sleep_disable_flag(setup, status, sleeping):
    path, _, auth, manifest, transport, runner = setup
    auth.update(schema="brodson-operator-authorization-v2", allow_automatic_wake_reload=True)
    transport.replies = [models(args=[], status=status), props() | {"is_sleeping": sleeping}]
    continuation = metadata_then_auth(setup)
    saved = json.loads(path.read_bytes())
    assert saved["binding"]["allow_automatic_wake_reload"] is True
    assert len(transport.calls) == 2  # Opt-in still cannot turn metadata into inference.
    transport.replies.extend(good_replies())
    state = asyncio.run(runner(continuation).run(manifest))
    assert state["status"] == "complete" and len(transport.calls) == 8
    assert state["reserved_tokens"] == 1905
    for method, route, *_ in transport.calls:
        assert route in q.ROUTES and q.ROUTES[route] == method
        if route != "/models":
            assert "autoload=false" in route


@pytest.mark.parametrize("status", ["unloaded", "loading", "stopped", "error", "failed", "downloading", None])
def test_opt_in_never_admits_missing_or_nonready_child(setup, status):
    _, _, auth, manifest, transport, runner = setup
    auth.update(schema="brodson-operator-authorization-v2", allow_automatic_wake_reload=True)
    transport.replies[0] = models(args=[], status=status)
    state = asyncio.run(runner().run(manifest))
    assert state["status"] == "stopped" and len(transport.calls) == 1


@pytest.mark.parametrize("sleeping", [None, 0, 1, "false", "true"])
def test_opt_in_still_requires_unambiguous_sleep_snapshot(setup, sleeping):
    _, _, auth, manifest, transport, runner = setup
    auth.update(schema="brodson-operator-authorization-v2", allow_automatic_wake_reload=True)
    transport.replies[1] = props() | {"is_sleeping": sleeping}
    continuation = metadata_then_auth(setup)
    state = asyncio.run(runner(continuation).run(manifest))
    assert state["stop_reason"] == "existing_loaded_or_sleeping_target_required"
    assert len(transport.calls) == 2


@pytest.mark.parametrize("initial,changed", [(False, True), (True, False)])
def test_wake_permission_cannot_change_after_metadata(setup, initial, changed):
    path, _, auth, manifest, transport, runner = setup
    auth.update(schema="brodson-operator-authorization-v2", allow_automatic_wake_reload=initial)
    continuation = metadata_then_auth(setup)
    before = path.read_bytes()
    continuation["allow_automatic_wake_reload"] = changed
    with pytest.raises(q.QualificationError, match="run_binding"):
        asyncio.run(runner(continuation).run(manifest))
    assert path.read_bytes() == before and len(transport.calls) == 2


@pytest.mark.parametrize("permission", [False, True])
def test_existing_v1_state_cannot_be_adopted_by_v2(setup, permission):
    path, _, _, manifest, transport, runner = setup
    continuation = metadata_then_auth(setup)
    before = path.read_bytes()
    assert "authorization_schema" not in json.loads(before)["binding"]
    continuation.update(schema="brodson-operator-authorization-v2", allow_automatic_wake_reload=permission)
    with pytest.raises(q.QualificationError, match="run_binding"):
        asyncio.run(runner(continuation).run(manifest))
    assert path.read_bytes() == before and len(transport.calls) == 2


def test_v2_false_preserves_strict_sleep_guard(setup):
    _, _, auth, manifest, transport, runner = setup
    auth.update(schema="brodson-operator-authorization-v2", allow_automatic_wake_reload=False)
    transport.replies[0] = models(args=[])
    continuation = metadata_then_auth(setup)
    state = asyncio.run(runner(continuation).run(manifest))
    assert state["stop_reason"] == "loaded_target_and_explicit_disabled_sleep_required"
    assert len(transport.calls) == 2


def test_opt_in_keeps_pending_generation_and_permission_after_crash(setup):
    path, _, auth, manifest, transport, runner = setup
    auth.update(schema="brodson-operator-authorization-v2", allow_automatic_wake_reload=True)
    continuation = metadata_then_auth(setup)
    class Crash(BaseException):
        pass
    transport.replies.extend([{"object": "response.input_tokens", "input_tokens": 123}, Crash()])
    with pytest.raises(Crash):
        asyncio.run(runner(continuation).run(manifest))
    saved = json.loads(path.read_bytes())
    assert saved["reserved_tokens"] == 635 and saved["binding"]["allow_automatic_wake_reload"] is True
    with pytest.raises(q.QualificationError, match="uncertain_attempt"):
        asyncio.run(runner(continuation).run(manifest))
    assert len(transport.calls) == 4
