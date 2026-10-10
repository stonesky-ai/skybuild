"""CPU-only proofs of signed two-task authorization and nonreplayable consumption."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import subprocess

import pytest

import gate_policy as policy
import isolated_full_test_gate as gate


def write(path, value):
    path.write_bytes(value if isinstance(value, bytes) else policy.canonical(value))
    path.chmod(0o600)
    return path


def signed(payload, domain, spec):
    signature = hmac.new(Path(spec["key_path"]).read_bytes(), domain.encode() + b"\0" + policy.canonical(payload),
                         hashlib.sha256).hexdigest()
    return {"schema": domain, "key_id": spec["key_id"], "principal": spec["principal"],
            "payload": payload, "signature": signature}


def fixture(tmp_path, monkeypatch):
    authority = tmp_path / "authority"
    authority.mkdir(mode=0o700)
    checkout = tmp_path / "candidate"
    checkout.mkdir()

    def git(*args):
        result = subprocess.run(["git", "-C", str(checkout), "-c", "core.hooksPath=/dev/null",
                                 *args], capture_output=True, check=True)
        return result.stdout.decode().strip()

    git("init", "-q")
    git("config", "user.name", "Policy Fixture")
    git("config", "user.email", "policy@example.invalid")
    for name in ("one.txt", "two.txt"):
        (checkout / name).write_text("base\n")
    git("add", ".")
    git("commit", "-qm", "Base")
    base = git("rev-parse", "HEAD")
    heads = []
    patches = []
    for number, name in enumerate(("one.txt", "two.txt")):
        git("checkout", "-qb", "task/member-" + str(number), base)
        (checkout / name).write_text("approved\n")
        git("commit", "-qam", "Approved member")
        heads.append(git("rev-parse", "HEAD"))
        patches.append(write(authority / ("patch-" + str(number)), git("diff", "--binary", base, heads[-1]).encode() + b"\n"))
    git("checkout", "-qb", "task/combined", base)
    for head in heads:
        git("merge", "--no-ff", "--no-edit", head)
    specs = []
    for number, principal in enumerate(("owner", "integration:attest", "reviewer-one", "reviewer-two", "validation:attest")):
        key_path = write(authority / ("key-" + str(number)), bytes([number + 1]) * 32)
        specs.append({"key_id": "key-" + str(number), "principal": principal, "key_path": str(key_path)})
    state = authority / "state"
    state.mkdir(mode=0o700)
    trust = {"schema": policy.TRUST, "approval": specs[0], "integration": specs[1],
             "reviewers": specs[2:4], "validation": specs[4], "consume_dir": str(state)}
    trust_path = write(authority / "trust.json", trust)
    trust_sha = policy.digest(trust_path.read_bytes())
    now = datetime.now(timezone.utc)
    issued = (now - timedelta(minutes=5)).isoformat()
    reviewed = (now - timedelta(minutes=4)).isoformat()
    frozen_at = (now - timedelta(minutes=3)).isoformat()
    source = {name: ("a" * 40 if name in ("commit", "tree") else "b" * 64) for name in policy.SOURCE}
    predicate = {"bundle_id": "bundle", "pr_number": 80, "target_ref": "refs/heads/dev-006",
                 "target_base": base, "candidate_commit": git("rev-parse", "HEAD"),
                 "candidate_tree": git("rev-parse", "HEAD^{tree}"),
                 "candidate_archive_sha256": "c" * 64, "candidate_history_sha256": "d" * 64,
                 "execution_host": "wonko", "runner_image_id": "sha256:" + "e" * 64,
                 "postgres_image_id": "sha256:" + "f" * 64, "firewall_image_id": "sha256:" + "1" * 64}
    predicate.update({name: (True if name == "candidate_blocked_until_probe" else
                            gate.DEFAULT_GATE_COMMAND if name == "gate_argv" else "profile-value")
                      for name in policy.PROFILE_FIELDS})
    tasks = []
    members = []
    for number, (head, patch) in enumerate(zip(heads, patches, strict=True)):
        task = {"task_id": "task-" + str(number), "assignment_id": "assignment-" + str(number),
                "worker_id": "worker-" + str(number), "task_branch": "task/member-" + str(number),
                "brief_sha256": "2" * 64, "approved_patch_sha256": policy.digest(patch.read_bytes()),
                "patch_path": str(patch), "owned_paths": [("one.txt", "two.txt")[number]],
                "base_sha": base, "definition_revision": 1, "policy_version": "v1",
                "focused_profiles": {"unit": ("petri-client", "session-scan")[number] + "-unit-v1",
                                     "long": ("petri-client", "session-scan")[number] + "-long-v1"}}
        brief = write(authority / ("brief-" + str(number) + ".json"), {"task_id": task["task_id"]})
        task["brief_path"] = str(brief)
        task["brief_sha256"] = policy.digest(brief.read_bytes())
        tasks.append(task)
        artifact = write(authority / ("review-" + str(number)), b"Independent exact-source review passed.\n")
        artifact_sha = policy.digest(artifact.read_bytes())
        workflow = {"attempt_id": "attempt-" + str(number), "claim_fence": 1, "input_generation": 1,
                    "definition_revision": 1, "policy_version": "v1", "source_sha": head, "base_sha": base}
        review = {"project_id": "skybuild", "task_id": task["task_id"], "source_head": head,
                  "source_branch": task["task_branch"], **workflow, "reviewer_principal": specs[number + 2]["principal"],
                  "verdict": "pass", "artifact_uri": "review://" + str(number),
                  "artifact_sha256": artifact_sha, "issued_at": reviewed}
        members.append({"task_id": task["task_id"], "assignment_id": task["assignment_id"],
                        "worker_id": task["worker_id"], "brief_sha256": task["brief_sha256"], "head_sha": head,
                        "workflow": workflow, "code_review": {"kind": "independent_code_review",
                        "producer": specs[number + 2]["principal"], "artifact_uri": review["artifact_uri"],
                        "artifact_sha256": artifact_sha}, "review": signed(review, policy.REVIEW, specs[number + 2]),
                        "review_artifact_path": str(artifact)})
    permit = {"schema": policy.POLICY, "permit_id": "one-shot-42", "project_id": "skybuild",
              "target_ref": predicate["target_ref"], "base_sha": base, "tasks": tasks,
              "source_profile": policy.PROFILE, "runner_source": source,
              "images": {name: predicate[name] for name in ("runner_image_id", "postgres_image_id", "firewall_image_id")},
              "gate_profile": {name: predicate[name] for name in policy.PROFILE_FIELDS},
              "execution_host": "wonko", "resource_limits": gate.RESOURCE_LIMITS, "attestation_key_id": "gate-key",
              "usage": {"path": str(authority / "usage.json"), "sha256": "3" * 64,
                        "valid_until": (now + timedelta(hours=1)).isoformat()},
              "host_watch": {"path": str(authority / "watch.json"), "max_age_seconds": 120,
                             "reserve_bytes": 8 * 1024**3, "required_available_bytes": 14 * 1024**3,
                             "disk_path": str(authority), "disk_reserve_bytes": 4 * 1024**3},
              "delivery": {"conductor_source_sha": "4" * 40, "integration_source_sha": "5" * 40,
                           "publisher_source_sha": "6" * 40, "publisher_trust_sha256": "7" * 64,
                           "gate_trust_sha256": trust_sha, "validation_runtime_sha256": "8" * 64,
                           "pr_source_ref": "task/combined", "state_dir": str(state)},
              "issued_at": issued, "expires_at": (now + timedelta(minutes=30)).isoformat(), "max_runs": 1}
    policy_path = authority / "policy.json"
    input_path = authority / "input.json"

    def freeze(*, prior_results=True):
        write(policy_path, signed(permit, policy.APPROVAL, specs[0]))
        permit_sha = policy.digest(policy_path.read_bytes())
        conductor = {"schema": "skybuild.conductor-permit-consumption.v1", "permit_id": permit["permit_id"],
                     "policy_sha256": permit_sha, "conductor_source_sha": permit["delivery"]["conductor_source_sha"],
                     "integration_source_sha": permit["delivery"]["integration_source_sha"], "consumed_at": reviewed,
                     "state": "consumed_hold_on_unknown"}
        record = write(state / (policy.digest(permit["permit_id"].encode()) + ".conductor.consumed.json"), conductor)
        for number, member in enumerate(members if prior_results else []):
            results = {}
            for stage in ("unit", "long"):
                stage_name = "focused-" + str(number) + "-" + stage
                intent = write(state / (policy.digest(permit["permit_id"].encode()) + "." + stage_name + ".consumed.json"), {
                    "schema": "skybuild.gate-permit-consumption.v1", "permit_id": permit["permit_id"],
                    "policy_sha256": permit_sha, "input_sha256": "9" * 64,
                    "conductor_intent_sha256": policy.digest(record.read_bytes()),
                    "state": "consumed_hold_on_unknown", "consumed_at": reviewed, "stage": stage_name})
                authorization = policy.Authorization(permit, intent, "9" * 64)
                authorization.trust = trust
                authorization.policy_sha256 = permit_sha
                authorization.focused = {"task": tasks[number], "workflow": member["workflow"], "stage": stage,
                    "profile": tasks[number]["focused_profiles"][stage], "source_head": member["head_sha"],
                    "conductor_intent_sha256": policy.digest(record.read_bytes())}
                command = policy.FOCUSED_COMMANDS[authorization.focused["profile"]]
                isolation = {"candidate_tree": git("rev-parse", member["head_sha"] + "^{tree}"),
                    "candidate_commit": member["head_sha"], "gate_argv": command,
                    "gate_command_sha256": policy.digest(json.dumps(command).encode()),
                    "result": {"exit_code": 0}, "cleanup": {"status": "confirmed"}}
                counts = {"collected": 1, "selected": 1, "passed": 1, "failed": 0, "errors": 0,
                          "skipped": 0, "deselected": 0, "xfailed": 0, "xpassed": 0}
                receipt = policy.sign_focused(isolation, authorization, Path(specs[4]["key_path"]), specs[4]["key_id"], counts)
                receipt_path = write(authority / (stage_name + ".json"), receipt)
                results[stage] = {"path": str(receipt_path), "sha256": policy.digest(receipt_path.read_bytes())}
            member["focused_results"] = results
        frozen = {"schema": policy.INPUT, "policy_sha256": permit_sha, "project_id": "skybuild",
                  "target_ref": permit["target_ref"], "base_sha": base, "members": members,
                  "prepared_manifest_sha256": "8" * 64,
                  **{name: predicate[name] for name in ("candidate_commit", "candidate_tree", "candidate_archive_sha256",
                                                       "candidate_history_sha256", "bundle_id", "pr_number")},
                  "pr_head_sha": predicate["candidate_commit"], "pr_base_sha": base, "frozen_at": frozen_at,
                  "pr_source_ref": permit["delivery"]["pr_source_ref"], "conductor_intent_sha256": policy.digest(record.read_bytes())}
        write(input_path, signed(frozen, policy.INTEGRATION, specs[1]))
        return permit_sha

    monkeypatch.setattr(policy.Authorization, "check", lambda *args, **kwargs: None)
    return {"checkout": checkout, "authority": authority, "permit": permit, "members": members,
            "predicate": predicate, "source": source, "trust": trust, "policy_path": policy_path,
            "trust_path": trust_path, "trust_sha": trust_sha, "input_path": input_path, "freeze": freeze,
            "specs": specs, "state": state}


def authorize(values, permit_sha):
    return policy.authorize(values["checkout"], values["predicate"], "gate-key", values["policy_path"], permit_sha,
                            values["trust_path"], values["trust_sha"], values["input_path"], values["source"], gate.RESOURCE_LIMITS)


def test_two_real_git_merges_signed_reviews_and_durable_one_shot(tmp_path, monkeypatch):
    values = fixture(tmp_path, monkeypatch)
    permit_sha = values["freeze"]()
    result = authorize(values, permit_sha)
    record = json.loads(result.record.read_bytes())
    assert record["state"] == "consumed_hold_on_unknown"
    assert record["input_sha256"] == policy.digest(values["input_path"].read_bytes())
    with pytest.raises(policy.PolicyError, match="already consumed"):
        authorize(values, permit_sha)


def test_linked_worktree_uses_its_actual_common_object_directory(tmp_path, monkeypatch):
    values = fixture(tmp_path, monkeypatch)
    linked = tmp_path / "linked"
    subprocess.run(["git", "-C", str(values["checkout"]), "worktree", "add", "--detach", str(linked), "HEAD"],
                   check=True, capture_output=True)
    assert (linked / ".git").is_file()
    values["checkout"] = linked
    assert authorize(values, values["freeze"]()).record.is_file()


def test_focused_stage_verifies_one_exact_approved_head_without_bundle_or_pr(tmp_path, monkeypatch):
    values = fixture(tmp_path, monkeypatch)
    permit_sha = values["freeze"](prior_results=False)
    task, member = values["permit"]["tasks"][0], values["members"][0]
    predicate = {name: value for name, value in values["predicate"].items() if name not in {"bundle_id", "pr_number"}}
    predicate["candidate_commit"] = member["head_sha"]
    predicate["candidate_tree"] = policy._tree(values["checkout"], member["head_sha"])
    conductor = next(values["state"].glob("*.conductor.consumed.json"))
    focused = {"schema": policy.FOCUSED_INPUT, "policy_sha256": permit_sha, "project_id": "skybuild",
               "target_ref": values["permit"]["target_ref"], "base_sha": values["permit"]["base_sha"],
               **{name: task[name] for name in ("task_id", "assignment_id", "worker_id", "brief_sha256")},
               "head_sha": member["head_sha"], "workflow": member["workflow"],
               **{name: predicate[name] for name in ("candidate_tree", "candidate_archive_sha256", "candidate_history_sha256")},
               "conductor_intent_sha256": policy.digest(conductor.read_bytes()),
               "submitted_at": datetime.now(timezone.utc).isoformat(), "stage": "unit"}
    write(values["input_path"], signed(focused, policy.INTEGRATION, values["specs"][1]))
    arguments = (values["checkout"], predicate, values["specs"][4]["key_id"], values["policy_path"], permit_sha,
                 values["trust_path"], values["trust_sha"], values["input_path"], values["source"], gate.RESOURCE_LIMITS)
    authorization = policy.authorize(*arguments, focused_stage="focused-0-unit")
    assert authorization.focused["profile"] == "petri-client-unit-v1"
    assert "bundle_id" not in predicate and "pr_number" not in predicate
    with pytest.raises(policy.PolicyError, match="already consumed"):
        policy.authorize(*arguments, focused_stage="focused-0-unit")


def test_focused_whitelist_matches_trusted_launcher_and_preserves_full_command():
    import gate_container_entrypoint as entrypoint
    assert entrypoint.FULL_COMMAND == gate.DEFAULT_GATE_COMMAND == policy.FULL_COMMAND
    assert entrypoint.FOCUSED_COMMANDS == policy.FOCUSED_COMMANDS
    for command in policy.FOCUSED_COMMANDS.values():
        assert entrypoint.fixed_command(command) == command
    with pytest.raises(RuntimeError, match="whitelist"):
        entrypoint.fixed_command(["python", "candidate.py"])


@pytest.mark.parametrize("summary,passed,skipped", [("5 passed, 19 deselected in 0.20s", 5, 0),
                                                   ("2 skipped in 0.20s", 0, 2), ("no tests ran in 0.01s", 0, 0)])
def test_bounded_focused_pytest_count_parser(tmp_path, summary, passed, skipped):
    log = tmp_path / "candidate.log"
    log.write_text("preflight\n" + summary + "\n")
    counts = gate._pytest_counts(log)
    assert counts["passed"] == passed and counts["skipped"] == skipped
    assert counts["collected"] == counts["selected"] + counts["deselected"]


@pytest.mark.parametrize("mutation", ["patch", "review", "workflow", "artifact", "source", "unknown", "candidate", "brief"])
def test_invalid_frozen_proofs_consume_and_hold_without_replay(tmp_path, monkeypatch, mutation):
    values = fixture(tmp_path, monkeypatch)
    if mutation == "patch":
        Path(values["permit"]["tasks"][0]["patch_path"]).write_bytes(b"hostile patch")
    elif mutation == "review":
        values["members"][0]["review"]["signature"] = "0" * 64
    elif mutation == "workflow":
        values["members"][0]["workflow"]["claim_fence"] = 2
    elif mutation == "artifact":
        Path(values["members"][0]["review_artifact_path"]).write_bytes(b"different report")
    elif mutation == "source":
        values["permit"]["runner_source"] = {**values["source"], "commit": "9" * 40}
    elif mutation == "unknown":
        values["members"][0]["unknown"] = True
    elif mutation == "candidate":
        values["predicate"]["candidate_tree"] = "a" * 40
    elif mutation == "brief":
        values["members"][0]["brief_sha256"] = "a" * 64
    permit_sha = values["freeze"]()
    with pytest.raises(policy.PolicyError):
        authorize(values, permit_sha)
    assert len(list(values["state"].glob("*.gate.consumed.json"))) == 1
    with pytest.raises(policy.PolicyError, match="already consumed"):
        authorize(values, permit_sha)


def test_wrong_owner_signature_never_consumes_or_prepares(tmp_path, monkeypatch):
    values = fixture(tmp_path, monkeypatch)
    values["freeze"]()
    envelope = json.loads(values["policy_path"].read_bytes())
    envelope["signature"] = "0" * 64
    write(values["policy_path"], envelope)
    with pytest.raises(policy.PolicyError, match="signature"):
        authorize(values, policy.digest(values["policy_path"].read_bytes()))
    assert not list(values["state"].glob("*.gate.consumed.json"))


def test_predicate_replacement_after_authorization_stops_before_preparation(tmp_path, monkeypatch):
    values = fixture(tmp_path, monkeypatch)
    authorization = authorize(values, values["freeze"]())
    changed = {**values["predicate"], "candidate_commit": "0" * 40}
    monkeypatch.setattr(gate, "_read_predicate", lambda *args, **kwargs: changed)
    monkeypatch.setattr(gate, "prepare", lambda *args, **kwargs: pytest.fail("Preparation must not begin"))
    with pytest.raises(gate.GateError, match="changed after one-shot"):
        gate.execute(values["checkout"], values["input_path"], None, Path(values["specs"][4]["key_path"]),
                     "gate-key", values["authority"], _policy_authorization=authorization)
    assert authorization.record.is_file()


def test_source_object_alternates_are_rejected(tmp_path, monkeypatch):
    values = fixture(tmp_path, monkeypatch)
    info = values["checkout"] / ".git/objects/info"
    alternate = values["authority"] / "untrusted-objects"
    alternate.mkdir()
    (info / "alternates").write_text(str(alternate) + "\n")
    with pytest.raises(policy.PolicyError, match="alternates"):
        authorize(values, values["freeze"]())


def test_duplicate_json_and_unknown_signature_fields_are_rejected():
    with pytest.raises(policy.PolicyError, match="Duplicate"):
        policy.parse(b'{"schema":"one","schema":"two"}')
    with pytest.raises(policy.PolicyError, match="unknown"):
        policy.fields({"required": 1, "unknown": 2}, {"required"})


def test_policy_cli_cannot_mix_authorities_or_silently_prepare(tmp_path):
    common = ["--checkout", str(tmp_path), "--expected-predicate", str(tmp_path / "p"),
              "--output-dir", str(tmp_path / "out")]
    assert gate.main([*common, "--execute-policy", "--execute"]) == 2
    assert gate.main([*common, "--policy-input", str(tmp_path / "input")]) == 2


def test_continuous_watch_failure_blocks_docker_but_cleanup_can_proceed(monkeypatch):
    authorization = policy.Authorization({}, Path("/unused"), "0" * 64)
    authorization.failure = policy.PolicyError("reserve lost")
    monkeypatch.setattr(gate, "_ACTIVE_POLICY_AUTHORIZATION", authorization)
    monkeypatch.setattr(gate.subprocess, "run", lambda *args, **kwargs: pytest.fail("Docker must not run"))
    with pytest.raises(policy.PolicyError, match="Continuous"):
        gate._docker("start", "candidate")


def usage_fixture(tmp_path, *, revised=True):
    now = datetime.now(timezone.utc)
    usage = {"confirmed_at": (now - timedelta(minutes=1)).isoformat(),
             "valid_until": (now + timedelta(minutes=10)).isoformat(),
             "weekly_used_percent": 56, "production_must_drain": True,
             "stop_production_percent": 50}
    usage_path = write(tmp_path / "usage.json", usage)
    pin = {"path": str(usage_path), "sha256": policy.digest(usage_path.read_bytes()),
           "valid_until": usage["valid_until"]}
    owner = {"schema": "skybuild.usage-policy-owner-revision.v1",
             "production_allowed": True, "weekly_production_stop_percent": None}
    owner_path = write(tmp_path / "owner-policy.json", owner)
    if revised:
        pin["owner_policy"] = {"path": str(owner_path), "sha256": policy.digest(owner_path.read_bytes())}
    return now, usage, usage_path, pin, owner, owner_path


def test_explicit_owner_revision_accepts_truthful_56_percent(tmp_path):
    now, usage, path, pin, _, _ = usage_fixture(tmp_path)
    policy._check_usage(pin, now)
    assert policy.parse(path.read_bytes())["weekly_used_percent"] == 56


def test_legacy_weekly_cutoff_remains_unchanged(tmp_path):
    now, usage, path, pin, _, _ = usage_fixture(tmp_path, revised=False)
    with pytest.raises(policy.PolicyError, match="Legacy"):
        policy._check_usage(pin, now)
    usage.update(weekly_used_percent=42, production_must_drain=False)
    write(path, usage); pin["sha256"] = policy.digest(path.read_bytes())
    policy._check_usage(pin, now)


@pytest.mark.parametrize("change", ["changed_hash", "denied", "threshold_present", "missing_threshold", "wrong_schema", "symlink", "expired", "usage_changed", "nonfinite"])
def test_owner_revision_never_bypasses_pins_or_freshness(tmp_path, change):
    now, usage, path, pin, owner, owner_path = usage_fixture(tmp_path)
    if change == "changed_hash":
        pin["owner_policy"]["sha256"] = "0" * 64
    elif change == "symlink":
        link = tmp_path / "linked-owner.json"; link.symlink_to(owner_path)
        pin["owner_policy"]["path"] = str(link)
    elif change in ("denied", "threshold_present", "missing_threshold", "wrong_schema"):
        if change == "denied": owner["production_allowed"] = False
        elif change == "threshold_present": owner["weekly_production_stop_percent"] = 50
        elif change == "missing_threshold": del owner["weekly_production_stop_percent"]
        else: owner["schema"] = "unknown"
        write(owner_path, owner); pin["owner_policy"]["sha256"] = policy.digest(owner_path.read_bytes())
    elif change == "expired":
        usage["valid_until"] = (now - timedelta(seconds=1)).isoformat()
        write(path, usage); pin.update(sha256=policy.digest(path.read_bytes()), valid_until=usage["valid_until"])
    elif change == "usage_changed":
        usage["weekly_used_percent"] = 57; write(path, usage)
    elif change == "nonfinite":
        usage["weekly_used_percent"] = float("nan")
        path.write_text(json.dumps(usage)); pin["sha256"] = policy.digest(path.read_bytes())
    with pytest.raises(policy.PolicyError):
        policy._check_usage(pin, now)
