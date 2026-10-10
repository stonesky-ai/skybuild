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
    for number, principal in enumerate(("owner", "integration:attest", "reviewer-one", "reviewer-two")):
        key_path = write(authority / ("key-" + str(number)), bytes([number + 1]) * 32)
        specs.append({"key_id": "key-" + str(number), "principal": principal, "key_path": str(key_path)})
    state = authority / "state"
    state.mkdir(mode=0o700)
    trust = {"schema": policy.TRUST, "approval": specs[0], "integration": specs[1],
             "reviewers": specs[2:], "consume_dir": str(state)}
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
                "base_sha": base, "definition_revision": 1, "policy_version": "v1"}
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
                           "gate_trust_sha256": trust_sha, "pr_source_ref": "task/combined", "state_dir": str(state)},
              "issued_at": issued, "expires_at": (now + timedelta(minutes=30)).isoformat(), "max_runs": 1}
    policy_path = authority / "policy.json"
    input_path = authority / "input.json"

    def freeze():
        write(policy_path, signed(permit, policy.APPROVAL, specs[0]))
        permit_sha = policy.digest(policy_path.read_bytes())
        conductor = {"schema": "skybuild.conductor-permit-consumption.v1", "permit_id": permit["permit_id"],
                     "policy_sha256": permit_sha, "conductor_source_sha": permit["delivery"]["conductor_source_sha"],
                     "integration_source_sha": permit["delivery"]["integration_source_sha"], "consumed_at": reviewed,
                     "state": "consumed_hold_on_unknown"}
        record = write(state / (policy.digest(permit["permit_id"].encode()) + ".conductor.consumed.json"), conductor)
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
