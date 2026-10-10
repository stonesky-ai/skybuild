"""Local bare-repository exercises for the bundle CAS and unknown-ack rules."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bundle_publisher as publisher
import trusted_gate_attestation as gate
from skybuild.manual_integration import DEFAULT_GATE_COMMAND, DEFAULT_GATE_COMMAND_SHA256
from skybuild.workflow import Place, ResultState, TaskToken, ValidationResult, ValidationStage

_ORIGINAL_VERIFY_TRUSTED_RUNTIME = publisher._verify_trusted_runtime


def git(cwd, *args):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True,
                                   stderr=subprocess.PIPE).strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "verify_skybuild", lambda _checkout: None)
    monkeypatch.setattr(publisher, "verify_skybuild_remote", lambda _checkout, _remote: None)
    monkeypatch.setattr(publisher, "_verify_trusted_runtime", lambda *_args: None)
    trusted_source = tmp_path / "trusted-publisher"
    trusted_source.mkdir()
    git(trusted_source, "init", "-b", "main")
    git(trusted_source, "config", "user.name", "Publisher test")
    git(trusted_source, "config", "user.email", "publisher-test@localhost")
    (trusted_source / "bundle_publisher.py").write_text("# trusted test publisher\n")
    git(trusted_source, "add", ".")
    git(trusted_source, "commit", "-m", "trusted publisher source")
    trusted_commit = git(trusted_source, "rev-parse", "HEAD")
    monkeypatch.setattr(publisher, "__file__", str(trusted_source / "bundle_publisher.py"))
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Publisher test")
    git(repo, "config", "user.email", "publisher-test@localhost")
    (repo / "bundle.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                   capture_output=True, text=True)
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "origin", "main")
    (repo / "bundle.txt").write_text("base\nreviewed candidate\n")
    git(repo, "commit", "-am", "frozen bundle candidate")
    candidate = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    key = tmp_path / "gate.hmac"
    key.write_bytes(b"local bare repository test key 0123456789abcdef")
    key.chmod(0o600)
    return {"repo": repo, "bare": bare, "base": base, "candidate": candidate,
            "tree": tree, "target": "refs/heads/main", "intents": tmp_path / "intents",
            "key": key, "trusted_source": trusted_source,
            "trusted_commit": trusted_commit}


def frozen_bundle(world):
    manifest_sha = hashlib.sha256(b"local publisher test manifest").hexdigest()
    review_sha = hashlib.sha256(b"review").hexdigest()
    return {"bundle_id": "bundle-" + manifest_sha[:24], "manifest_sha256": manifest_sha,
            "target_ref": world["target"], "base_commit": world["base"],
            "candidate_commit": world["candidate"], "candidate_tree": world["tree"],
            "policy_sha256": hashlib.sha256(b"policy").hexdigest(),
            "members": [{"task_id": "task-1", "source_head": world["candidate"],
                         "source_branch": "refs/heads/task-1", "reviewer": "reviewer-1",
                         "review_sha256": review_sha,
                         "review_artifact": f"reviews/code-review.json#sha256={review_sha}",
                         "workflow": {"project_id": "skybuild", "attempt_id": "attempt-1",
                                      "claim_fence": 2, "input_generation": 3,
                                      "definition_revision": 4, "policy_version": "policy-1",
                                      "target_base": world["base"]}}]}


def observe_task(world, project, task_id):
    bundle_member = {"task_id": task_id, "source_head": world["candidate"],
                     "source_branch": "refs/heads/task-1", "reviewer": "reviewer-1",
                     "review_sha256": hashlib.sha256(b"review").hexdigest(),
                     "review_artifact": "reviews/code-review.json#sha256=" + hashlib.sha256(b"review").hexdigest(),
                     "workflow": {"project_id": project, "attempt_id": "attempt-1",
                                  "claim_fence": 2, "input_generation": 3,
                                  "definition_revision": 4, "policy_version": "policy-1",
                                  "target_base": world["base"]}}
    workflow = bundle_member["workflow"]
    review = ValidationResult(
        project_id=project, task_id=task_id, stage=ValidationStage.CODE_REVIEW,
        state=ResultState.PASSED, attempt_id=workflow["attempt_id"],
        source_head=bundle_member["source_head"], target_base=workflow["target_base"],
        input_generation=workflow["input_generation"],
        definition_revision=workflow["definition_revision"],
        policy_version=workflow["policy_version"], claim_fence=workflow["claim_fence"],
        producer="reviewer-1", check_id="independent-code-review",
        artifacts=(bundle_member["review_artifact"],))
    token = TaskToken(
        project_id=project, task_id=task_id, place=Place.INTEGRATING, responsible="author-1",
        source_branch=bundle_member["source_branch"], source_head=bundle_member["source_head"],
        target_base=workflow["target_base"], definition_revision=workflow["definition_revision"],
        input_generation=workflow["input_generation"], policy_version=workflow["policy_version"],
        requirements=(ValidationStage.CODE_REVIEW,), evidence=(review,),
        attempt_id=workflow["attempt_id"], claim_fence=workflow["claim_fence"])
    return {"token": token.to_dict()}


def qualification(world):
    bundle = frozen_bundle(world)
    resources = {"runner_container_id": "2" * 64, "pg_container_id": "3" * 64,
                 "network_id": "4" * 64, "test_image_id": "sha256:" + "5" * 64}
    predicate = {
        "bundle_id": bundle["bundle_id"], "pr_number": 7, "target_ref": world["target"],
        "target_base": world["base"], "candidate_commit": world["candidate"],
        "candidate_tree": world["tree"],
        "candidate_archive_sha256": publisher._candidate_archive_sha256(world["repo"], world["candidate"]),
        "candidate_history_sha256": "9" * 64,
        "gate_argv": DEFAULT_GATE_COMMAND, "gate_command_sha256": DEFAULT_GATE_COMMAND_SHA256,
        "gate_policy_sha256": bundle["policy_sha256"], "runner_identity": "local-test-runner",
        "runner_version": "1", "runner_image_id": "sha256:" + "1" * 64,
        "postgres_image_id": "sha256:" + "7" * 64,
        "firewall_image_id": "sha256:" + "9" * 64,
        "firewall_policy_sha256": "a" * 64,
        "trusted_entrypoint_sha256": "8" * 64,
        "network_probe_sha256": "b" * 64,
        "attestation_signer_sha256": "c" * 64,
        "execution_host": "test-host", "source_mount_readonly": True,
        "scratch_mount_writable": True, "docker_socket_mounted": False,
        "host_home_mounted": False, "host_credentials_mounted": False,
        "credential_access": "synthetic_database_only", "network_mode": "internal",
        "egress_allowed": False, "firewall_defaults_drop": True,
        "firewall_ipv4_default_drop": True, "firewall_ipv6_default_drop": True,
        "firewall_policy_applied": True, "candidate_blocked_until_probe": True,
        "postgres_namespace_egress_blocked": True,
        "readonly_fixture_allowlist": ["/runner/entrypoint.py", "/runner/network_probe.py"],
        "mounts": [
            {"target": "/workspace/source", "mode": "ro", "kind": "bind",
             "source_class": "candidate_archive", "source": "sha256:" +
             publisher._candidate_archive_sha256(world["repo"], world["candidate"])},
            {"target": "/workspace/scratch", "mode": "rw", "kind": "tmpfs",
             "source_class": "scratch", "source": "tmpfs"},
            {"target": "/runner/entrypoint.py", "mode": "ro", "kind": "bind",
             "source_class": "trusted_runner_fixture", "source": "sha256:" + "8" * 64},
            {"target": "/runner/network_probe.py", "mode": "ro", "kind": "bind",
             "source_class": "trusted_runner_fixture", "source": "sha256:" + "b" * 64},
        ],
        "environment_allowlist": ["HOME", "PATH"],
        "postgres_data_mount": {"type": "tmpfs", "source": "tmpfs", "size_bytes": 1024**3},
        "resource_limits": {"cpu_millis": 1000, "memory_bytes": 1024**3,
                            "pids": 128, "timeout_seconds": 300},
        "resources": {**resources, "test_image_id": "sha256:" + "1" * 64,
                       "postgres_image_id": "sha256:" + "7" * 64,
                       "firewall_container_id": "5" * 64, "probe_container_id": "6" * 64,
                       "firewall_image_id": "sha256:" + "9" * 64,
                       "postgres_firewall_container_id": "a" * 64,
                       "postgres_probe_container_id": "b" * 64},
        "network_probe": {"postgres_tcp_allowed": True, "dns_blocked": True,
                          "external_ipv4_blocked": True, "external_ipv6_blocked": True,
                          "host_gateway_listener_blocked": True, "host_listener_port": 54001,
                          "log_sha256": "c" * 64},
        "postgres_network_probe": {"dns_blocked": True, "external_ipv4_blocked": True,
                                   "external_ipv6_blocked": True,
                                   "host_gateway_listener_blocked": True,
                                   "host_listener_port": 54001,
                                   "postgres_unix_socket_ready": True, "log_sha256": "d" * 64},
        "result": {"exit_code": 0, "log_sha256": "6" * 64,
                   "started_at": "2026-10-10T10:00:00Z", "finished_at": "2026-10-10T10:01:00Z",
                   "preflight": {"candidate_archive_readonly": True,
                                 "candidate_copied_to_scratch": True,
                                 "source_package_path": "/scratch/workspace/src/skybuild/__init__.py",
                                 "gate_launch": "trusted_exec",
                                 "host_gateway_probe": "blocked",
                                 "external_direct_ip_probe": "blocked",
                                 "external_dns_probe": "blocked"}},
        "cleanup": {"status": "confirmed", "owned_resources": [
            {"kind": "runner_container", "id": resources["runner_container_id"], "state": "absent"},
            {"kind": "postgres_container", "id": resources["pg_container_id"], "state": "absent"},
            {"kind": "network", "id": resources["network_id"], "state": "absent"},
            {"kind": "firewall_container", "id": "5" * 64, "state": "absent"},
            {"kind": "network_probe_container", "id": "6" * 64, "state": "absent"},
            {"kind": "postgres_firewall_container", "id": "a" * 64, "state": "absent"},
            {"kind": "postgres_network_probe_container", "id": "b" * 64, "state": "absent"},
        ]},
    }
    return gate.sign_attestation(predicate, key_path=world["key"], key_id="local-test")


def pr_view(world, state="OPEN"):
    return {"number": 7, "state": state, "isDraft": False, "headRefName": "bundle-head",
            "headRefOid": world["candidate"], "baseRefOid": world["base"],
            "baseRefName": "main",
            "headRepository": "stonesky-ai/skybuild",
            "baseRepository": "stonesky-ai/skybuild", "author": "bundle-author",
            "checks": "PASS",
            "mergeCommitOid": world["candidate"] if state == "MERGED" else None}


def call(world, observe_pr):
    receipt = qualification(world)
    predicate = receipt["predicate"]
    runner_pins = {key: predicate[key] for key in publisher._RUNNER_PIN_FIELDS}
    return publisher.publish_bundle(
        checkout=world["repo"], intent_dir=world["intents"], remote="origin",
        frozen_bundle=frozen_bundle(world), pr=7,
        qualification=qualification(world), trusted_gate_keys={"local-test": world["key"]},
        expected_gate_key_id="local-test", trusted_source_root=world["trusted_source"],
        trusted_runner_pins=runner_pins, trusted_publisher_commit=world["trusted_commit"],
        observe_pr=observe_pr, observe_task=lambda project, task_id: observe_task(world, project, task_id))


def test_publisher_augmentation_preserves_manual_receipt_shape(world, tmp_path):
    frozen = frozen_bundle(world)
    frozen["members"] = [{key: value for key, value in frozen["members"][0].items()
                          if key not in {"workflow", "review_artifact"}}]
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    review = tmp_path / "source-review.md"
    review.write_bytes(b"review")
    workflow = {"project_id": "skybuild", "attempt_id": "attempt-1", "claim_fence": 2,
                "input_generation": 3, "definition_revision": 4,
                "policy_version": "policy-1", "target_base": world["base"]}
    inputs = {"schema": "skybuild.bundle-input.v1", "checkout": str(world["repo"]),
              "target": {"ref": world["target"], "sha": world["base"]},
              "policy": {"path": str(tmp_path / "policy.md"), "sha256": frozen["policy_sha256"]},
              "members": [{"ref": "refs/heads/task-1", "sha": world["candidate"],
                           "task_id": "task-1", "reviewer": "reviewer-1",
                           "review": {"path": str(review), "sha256": frozen["members"][0]["review_sha256"]},
                           "workflow": workflow}]}
    fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    frozen["manifest_sha256"] = fingerprint
    frozen["bundle_id"] = "bundle-" + fingerprint[:24]
    report = {"schema": "skybuild.bundle-preparation.v1", "fingerprint": fingerprint,
              "inputs": inputs, "ok": True}
    (prepared / "inputs.json").write_text(json.dumps({"fingerprint": fingerprint, "inputs": inputs}))
    (prepared / "report.json").write_text(json.dumps(report))

    augmented = publisher._augment_frozen_workflow(world["repo"], prepared, frozen)
    assert augmented["members"][0]["workflow"] == workflow
    assert augmented["members"][0]["review_artifact"] == (
        str(review) + "#sha256=" + frozen["members"][0]["review_sha256"])
    assert set(frozen["members"][0]) == {
        "task_id", "source_head", "source_branch", "reviewer", "review_sha256"}


def test_github_pr_observer_uses_rest_base_sha_and_current_commit_checks(monkeypatch):
    commit = "a" * 40
    base = "b" * 40
    calls = []
    pull = {"number": 12, "state": "open", "draft": False, "merged": False,
            "head": {"sha": commit, "ref": "bundle-head",
                     "repo": {"full_name": "stonesky-ai/skybuild"}},
            "base": {"sha": base, "ref": "dev-006",
                     "repo": {"full_name": "stonesky-ai/skybuild"}},
            "user": {"login": "bundle-author"}, "merge_commit_sha": None}

    def api(path):
        calls.append(path)
        if path.endswith("/pulls/12"):
            return pull
        if path.endswith("/check-runs?per_page=100"):
            return {"total_count": 0, "check_runs": []}
        if path.endswith("/status"):
            return {"total_count": 0, "statuses": [], "state": "pending"}
        pytest.fail(f"unexpected GitHub endpoint: {path}")

    monkeypatch.setattr(publisher, "_gh_api", api)
    view = publisher.observe_github_pr(12)
    assert view["baseRefOid"] == base
    assert view["headRefOid"] == commit
    assert view["checks"] == "PASS"
    assert calls == ["repos/stonesky-ai/skybuild/pulls/12",
                     f"repos/stonesky-ai/skybuild/commits/{commit}/check-runs?per_page=100",
                     f"repos/stonesky-ai/skybuild/commits/{commit}/status"]


def test_owner_private_trust_config_requires_pinned_skybuild_ca(world, tmp_path):
    token = tmp_path / "observer-token"
    token.write_text("read-only-token\n")
    token.chmod(0o600)
    ca = tmp_path / "skybuild-ca.pem"
    ca.write_bytes(b"pinned CA material\n")
    pins = {key: qualification(world)["predicate"][key]
            for key in publisher._RUNNER_PIN_FIELDS}
    config = {"schema": "skybuild.publisher-trust.v1", "active_key_id": "local-test",
              "publisher_commit": world["trusted_commit"], "runner_pins": pins,
              "keys": {"local-test": str(world["key"])},
              "skybuild_client": {"base_url": "https://skybuild.example/api/v1",
                                  "token_path": str(token), "ca_file": str(ca),
                                  "ca_sha256": hashlib.sha256(ca.read_bytes()).hexdigest()}}
    path = tmp_path / "trusted-publisher.json"
    raw = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    keys, active, commit, observed_pins, client = publisher._read_trust_config(
        path, hashlib.sha256(raw).hexdigest(), world["repo"])
    assert keys["local-test"] == world["key"]
    assert active == "local-test" and commit == world["trusted_commit"]
    assert observed_pins == pins and client["ca_file"] == str(ca)

    config["skybuild_client"]["ca_file"] = None
    config["skybuild_client"]["ca_sha256"] = None
    raw = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(raw)
    with pytest.raises(publisher.PublisherError, match="requires a pinned CA file"):
        publisher._read_trust_config(path, hashlib.sha256(raw).hexdigest(), world["repo"])


def test_trusted_runtime_rejects_candidate_loaded_workflow(world, monkeypatch, tmp_path):
    source_root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(publisher, "__file__", str(source_root / "scripts/bundle_publisher.py"))
    canonical_paths = [str(source_root / "src"), str(source_root / "scripts"), str(source_root)]
    monkeypatch.setattr(sys, "path", canonical_paths + [entry for entry in sys.path
                                                         if entry not in canonical_paths])
    candidate_module = tmp_path / "candidate" / "src/skybuild/workflow.py"
    candidate_module.parent.mkdir(parents=True)
    candidate_module.write_text("# candidate workflow module\n")
    monkeypatch.setitem(sys.modules, "skybuild.workflow",
                        SimpleNamespace(__file__=str(candidate_module)))
    with pytest.raises(publisher.PublisherError, match="skybuild.workflow is outside"):
        _ORIGINAL_VERIFY_TRUSTED_RUNTIME(source_root, world["trusted_commit"])


def test_trusted_runtime_rejects_untrusted_pythonpath(world, monkeypatch, tmp_path):
    source_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(tmp_path), os.environ.get("PYTHONPATH", ""))))
    with pytest.raises(publisher.PublisherError, match="canonical project_python"):
        _ORIGINAL_VERIFY_TRUSTED_RUNTIME(source_root, world["trusted_commit"])


@pytest.mark.parametrize("place", ["done", "hold", "deferred"])
def test_only_integrating_workflow_can_publish(world, place):
    member = frozen_bundle(world)["members"][0]
    response = observe_task(world, "skybuild", member["task_id"])
    response["token"]["place"] = place
    with pytest.raises(publisher.PublisherError, match="Current SkyBuild task differs"):
        publisher._verify_member_workflow(member, lambda _project, _task: response)


def intent(world):
    target = publisher._key("origin", world["target"])
    return json.loads((world["intents"] / f"{target}.json").read_text())


def test_expected_base_cas_publishes_fast_forward_candidate(world):
    def view(_pr):
        current = git(world["repo"], "ls-remote", "origin", world["target"]).split()[0]
        return pr_view(world, "MERGED" if current == world["candidate"] else "OPEN")

    result = call(world, view)
    assert result["state"] == "confirmed"
    assert git(world["repo"], "ls-remote", "origin", world["target"]).split()[0] == world["candidate"]
    assert intent(world)["attempts"] == 1


def test_trusted_publisher_source_requires_clean_pinned_checkout(world):
    publisher._verify_trusted_publisher_source(world["trusted_source"], world["trusted_commit"])
    with pytest.raises(publisher.PublisherError, match="clean root-pinned"):
        publisher._verify_trusted_publisher_source(world["trusted_source"], "9" * 40)
    (world["trusted_source"] / "bundle_publisher.py").write_text("# changed source\n")
    with pytest.raises(publisher.PublisherError, match="clean root-pinned"):
        publisher._verify_trusted_publisher_source(world["trusted_source"], world["trusted_commit"])


def test_changed_base_rejects_before_intent_or_publication(world):
    competitor = world["repo"].parent / "competitor"
    subprocess.run(["git", "clone", "--branch", "main", str(world["bare"]), str(competitor)], check=True,
                   capture_output=True, text=True)
    git(competitor, "config", "user.name", "Racer")
    git(competitor, "config", "user.email", "racer@localhost")
    (competitor / "race.txt").write_text("concurrent writer\n")
    git(competitor, "add", ".")
    git(competitor, "commit", "-m", "concurrent update")
    git(competitor, "push", "origin", "HEAD:main")
    with pytest.raises(publisher.PublisherError, match="changed before publication"):
        call(world, lambda _pr: pr_view(world))
    assert not world["intents"].exists() or not list(world["intents"].glob("*.json"))


@pytest.mark.parametrize("change", [
    {"baseRefOid": "9" * 40},
    {"checks": "FAIL"},
    {"headRepository": "untrusted/other"},
])
def test_mismatched_or_unpassing_bundle_pr_is_rejected(world, change):
    def observe(_pr):
        return pr_view(world) | change

    with pytest.raises(publisher.PublisherError, match="exact frozen candidate"):
        call(world, observe)


def test_lease_rejects_race_after_preflight(world, monkeypatch):
    competitor = world["repo"].parent / "competitor"
    subprocess.run(["git", "clone", "--branch", "main", str(world["bare"]), str(competitor)], check=True,
                   capture_output=True, text=True)
    git(competitor, "config", "user.name", "Racer")
    git(competitor, "config", "user.email", "racer@localhost")
    (competitor / "race.txt").write_text("concurrent writer\n")
    git(competitor, "add", ".")
    git(competitor, "commit", "-m", "concurrent update")
    real_run = subprocess.run
    raced = False

    def run(argv, *args, **kwargs):
        nonlocal raced
        if isinstance(argv, (list, tuple)) and len(argv) > 1 and argv[1] == "push" and not raced:
            raced = True
            subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=competitor,
                           check=True, capture_output=True, text=True)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(publisher.subprocess, "run", run)
    with pytest.raises(publisher.PublisherError, match="outcome is unknown"):
        call(world, lambda _pr: pr_view(world))
    assert raced
    assert intent(world)["state"] == "unknown"
    assert git(world["repo"], "ls-remote", "origin", world["target"]).split()[0] != world["candidate"]


def test_lost_ack_is_recorded_and_second_call_only_reconciles(world, monkeypatch):
    real_run = subprocess.run
    pushes = 0

    def run(argv, *args, **kwargs):
        nonlocal pushes
        result = real_run(argv, *args, **kwargs)
        if isinstance(argv, (list, tuple)) and len(argv) > 1 and argv[1] == "push":
            pushes += 1
            return subprocess.CompletedProcess(argv, 1, "", "simulated lost acknowledgement")
        return result

    monkeypatch.setattr(publisher.subprocess, "run", run)
    with pytest.raises(publisher.PublisherError, match="outcome is unknown"):
        call(world, lambda _pr: pr_view(world))
    assert pushes == 1
    assert intent(world)["state"] == "unknown"
    result = call(world, lambda _pr: pr_view(world))
    assert result["state"] == "unknown"
    assert pushes == 1


def test_non_fast_forward_candidate_is_rejected(world):
    git(world["repo"], "checkout", "--orphan", "other")
    git(world["repo"], "rm", "-rf", ".")
    (world["repo"] / "other.txt").write_text("other branch\n")
    git(world["repo"], "add", ".")
    git(world["repo"], "commit", "-m", "unrelated candidate")
    world["candidate"] = git(world["repo"], "rev-parse", "HEAD")
    world["tree"] = git(world["repo"], "rev-parse", "HEAD^{tree}")
    with pytest.raises(publisher.PublisherError, match="git failed"):
        call(world, lambda _pr: pr_view(world))
    assert not list(world["intents"].glob("*.json"))
