"""Local bare-repository exercises for the bundle CAS and unknown-ack rules."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bundle_publisher as publisher
import trusted_gate_attestation as gate
from skybuild.manual_integration import DEFAULT_GATE_COMMAND, DEFAULT_GATE_COMMAND_SHA256


def git(cwd, *args):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True,
                                   stderr=subprocess.PIPE).strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "verify_skybuild", lambda _checkout: None)
    monkeypatch.setattr(publisher, "verify_skybuild_remote", lambda _checkout, _remote: None)
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
    return {"bundle_id": "bundle-" + manifest_sha[:24], "manifest_sha256": manifest_sha,
            "target_ref": world["target"], "base_commit": world["base"],
            "candidate_commit": world["candidate"], "candidate_tree": world["tree"],
            "policy_sha256": hashlib.sha256(b"policy").hexdigest(),
            "members": [{"task_id": "task-1", "source_head": world["candidate"],
                         "source_branch": "refs/heads/task-1", "reviewer": "reviewer-1",
                         "review_sha256": hashlib.sha256(b"review").hexdigest()}]}


def qualification(world):
    bundle = frozen_bundle(world)
    resources = {"runner_container_id": "2" * 64, "pg_container_id": "3" * 64,
                 "network_id": "4" * 64, "test_image_id": "sha256:" + "5" * 64}
    predicate = {
        "bundle_id": bundle["bundle_id"], "pr_number": 7, "target_ref": world["target"],
        "target_base": world["base"], "candidate_commit": world["candidate"],
        "candidate_tree": world["tree"],
        "candidate_archive_sha256": publisher._candidate_archive_sha256(world["repo"], world["candidate"]),
        "gate_argv": DEFAULT_GATE_COMMAND, "gate_command_sha256": DEFAULT_GATE_COMMAND_SHA256,
        "gate_policy_sha256": bundle["policy_sha256"], "runner_identity": "local-test-runner",
        "runner_version": "1", "runner_image_id": "sha256:" + "1" * 64,
        "postgres_image_id": "sha256:" + "7" * 64,
        "firewall_image_id": "sha256:" + "9" * 64,
        "firewall_policy_sha256": "a" * 64,
        "trusted_entrypoint_sha256": "8" * 64,
        "network_probe_sha256": "b" * 64,
        "execution_host": "test-host", "source_mount_readonly": True,
        "scratch_mount_writable": True, "docker_socket_mounted": False,
        "host_home_mounted": False, "host_credentials_mounted": False,
        "credential_access": "synthetic_database_only", "network_mode": "internal",
        "egress_allowed": False, "firewall_defaults_drop": True,
        "firewall_ipv4_default_drop": True, "firewall_ipv6_default_drop": True,
        "firewall_policy_applied": True, "candidate_blocked_until_probe": True,
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
                       "firewall_image_id": "sha256:" + "9" * 64},
        "network_probe": {"postgres_tcp_allowed": True, "dns_blocked": True,
                          "external_ipv4_blocked": True, "external_ipv6_blocked": True,
                          "host_gateway_listener_blocked": True, "host_listener_port": 54001,
                          "log_sha256": "c" * 64},
        "result": {"exit_code": 0, "log_sha256": "6" * 64,
                   "started_at": "2026-10-10T10:00:00Z", "finished_at": "2026-10-10T10:01:00Z",
                   "preflight": {"candidate_archive_readonly": True,
                                 "candidate_copied_to_scratch": True,
                                 "imported_package_path": "/scratch/workspace/src/skybuild/__init__.py",
                                 "host_gateway_probe": "blocked",
                                 "external_direct_ip_probe": "blocked",
                                 "external_dns_probe": "blocked"}},
        "cleanup": {"status": "confirmed", "owned_resources": [
            {"kind": "runner_container", "id": resources["runner_container_id"], "state": "absent"},
            {"kind": "postgres_container", "id": resources["pg_container_id"], "state": "absent"},
            {"kind": "network", "id": resources["network_id"], "state": "absent"},
            {"kind": "firewall_container", "id": "5" * 64, "state": "absent"},
            {"kind": "network_probe_container", "id": "6" * 64, "state": "absent"},
        ]},
    }
    return gate.sign_attestation(predicate, key_path=world["key"], key_id="local-test")


def pr_view(world, state="OPEN"):
    return {"number": 7, "state": state, "isDraft": False,
            "headRefOid": world["candidate"], "baseRefOid": world["base"],
            "baseRefName": "main",
            "headRepository": "stonesky-ai/skybuild",
            "reviewDecision": "APPROVED", "checks": "PASS",
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
        observe_pr=observe_pr)


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
    {"reviewDecision": "REVIEW_REQUIRED"},
])
def test_unreviewed_or_unpassing_bundle_pr_is_rejected(world, change):
    def observe(_pr):
        return pr_view(world) | change

    with pytest.raises(publisher.PublisherError, match="exact approved candidate"):
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
