"""Policy and real Git behavior for the bounded automatic CPU patch route."""

import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from skybuild import auto_patch_controller as controller
from skybuild import auto_patch_worker as worker
from skybuild import auto_patch_permit as permit


def task(task_id, assignment_id, digest, *, priority=1, approved=True):
    return {"task_id": task_id, "status": "ready", "revision": 2, "priority": priority,
            "acceptance_criteria": ["Apply the deterministic approved patch; verify exact bytes."],
            "metadata": {"_skybuild_cpu_patch": {
                "schema": "skybuild.cpu-patch.v1", "sha256": digest,
                "assignment_id": assignment_id if approved else "another-assignment"},
                "_skybuild_workflow": {"petri": {"schema_version": 1, "token": {"place": "ready",
                    "pending_action": None, "superseded": False}}}}}


class Client:
    def __init__(self, tasks):
        self.tasks = tasks

    def whoami(self):
        return {"principal_id": "pilot_dispatcher", "is_admin": False,
                "grants": {"skybuild": ["tasks:read", "cord:send", "cord:handle", "cord:read"]}}

    def get_task(self, project, task_id):
        assert project == "skybuild"
        return self.tasks[task_id]


def test_selector_chooses_two_distinct_ready_tasks_and_rejects_unapproved(tmp_path, monkeypatch):
    patch = tmp_path / "approved.patch"
    patch.write_bytes(b"bounded patch\n")
    patch.chmod(0o600)
    digest = hashlib.sha256(patch.read_bytes()).hexdigest()
    candidates = []
    tasks = {}
    for index, (who, path, priority, approved) in enumerate([
            ("worker_a", "docs/a.md", 2, True),
            ("worker_b", "docs/b.md", 1, True),
            ("worker_c", "docs/c.md", 0, False)], 1):
        name = f"SKYBUILD-CPU-{index}"
        assignment_id = f"CPU-{index}"
        brief_path = f"docs/design/assignments/cpu-{index}.json"
        candidates.append({"worker": who, "brief_path": brief_path,
                           "patch": str(patch), "patch_sha256": digest,
                           "token_file": str(tmp_path / f"token-{index}")})
        tasks[name] = task(name, assignment_id, digest, priority=priority, approved=approved)

    def envelope(_repo, brief_path, *, worker, dispatcher, base_ref):
        index = int(Path(brief_path).stem.split("-")[-1])
        assert dispatcher == "pilot_dispatcher" and base_ref == "refs/heads/dev-006"
        return {"task_id": f"SKYBUILD-CPU-{index}", "assignment_id": f"CPU-{index}",
                "branch": f"task/cpu-{index}", "base_sha": "a" * 40,
                "schema": "manual-work-v1", "brief_sha256": "b" * 64,
                "owned_paths": [f"docs/{'abc'[index - 1]}.md"]}

    monkeypatch.setattr(controller, "build_envelope", envelope)
    selected = controller.select(Client(tasks), tmp_path, candidates, project="skybuild",
                                 dispatcher="pilot_dispatcher", base_ref="refs/heads/dev-006")
    assert [(item["task_id"], item["worker"]) for item in selected] == [
        ("SKYBUILD-CPU-2", "worker_b"), ("SKYBUILD-CPU-1", "worker_a")]


def test_worker_applies_patch_and_checks_real_committed_bytes(tmp_path, monkeypatch):
    bare, source, destination = tmp_path / "origin.git", tmp_path / "source", tmp_path / "attempt" / "source"

    def git(repo, *args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout

    git(None, "init", "--bare", str(bare))
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.name", "Test Author")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "README.md").write_text("Original\n")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "base")
    base = git(source, "rev-parse", "HEAD").decode().strip()
    git(source, "remote", "add", "origin", str(bare))
    git(source, "push", "origin", "HEAD:refs/heads/dev-006")
    (source / "README.md").write_text("Original\nUseful new note.\n")
    patch = git(source, "diff", "--", "README.md")
    git(source, "checkout", "--", "README.md")
    patch_file = tmp_path / "approved.patch"
    patch_file.write_bytes(patch)
    with pytest.raises(worker.PatchWorkerError, match="outside exact assigned files"):
        worker._patch_paths(source, base, patch_file, ["OTHER.md"])
    destination.parent.mkdir()
    original = worker._git

    def local_git(repo, *args, timeout=30, askpass=None, git_token_file=None):
        if args[:3] == ("remote", "get-url", "origin") or args[:4] == (
                "remote", "get-url", "--push", "origin"):
            return b"https://github.com/stonesky-ai/skybuild.git\n"
        if args[:2] == ("clone", "--no-checkout"):
            args = ("clone", "--no-checkout", str(bare), args[-1])
        return original(repo, *args, timeout=timeout, askpass=askpass,
                        git_token_file=git_token_file)

    monkeypatch.setattr(worker, "_git", local_git)
    head, paths = worker._clone_and_apply(source, destination,
                                          {"base_sha": base, "branch": "task/approved-cpu-note",
                                           "task_id": "SKYBUILD-CPU-NOTE",
                                           "owned_paths": ["README.md"]}, patch,
                                          askpass=tmp_path / "unused-askpass",
                                          git_token_file=tmp_path / "unused-token")
    assert paths == ["README.md"]
    assert git(destination, "show", "HEAD:README.md") == b"Original\nUseful new note.\n"
    assert git(destination, "rev-list", "--parents", "-n", "1", head).decode().split() == [head, base]


def test_worker_refuses_changed_patch_and_unapproved_task(tmp_path):
    patch = tmp_path / "approved.patch"
    patch.write_bytes(b"changed bytes")
    patch.chmod(0o600)
    with pytest.raises(worker.PatchWorkerError, match="changed"):
        worker._patch_bytes(patch, hashlib.sha256(b"approved bytes").hexdigest())
    with pytest.raises(worker.PatchWorkerError, match="no matching"):
        worker._approved_task(task("SKYBUILD-CPU-1", "CPU-1", "a" * 64, approved=False),
                              {"task_id": "SKYBUILD-CPU-1", "task_revision": 2,
                               "assignment_id": "CPU-1"}, "a" * 64)


def test_selector_rejects_ready_task_without_claimable_petri_version():
    candidate = task("SKYBUILD-CPU-1", "CPU-1", "a" * 64)
    del candidate["metadata"]["_skybuild_workflow"]["petri"]["schema_version"]
    with pytest.raises(worker.PatchWorkerError, match="Petri task place"):
        worker._approved_task(candidate, {"task_id": "SKYBUILD-CPU-1", "task_revision": 2,
                                          "assignment_id": "CPU-1"}, "a" * 64)


def test_source_guard_rejects_dirty_checkout_at_approved_head(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    (checkout / "tracked.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "-C", str(checkout), "add", "tracked.py"], check=True)
    subprocess.run(["git", "-C", str(checkout), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
    head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    (checkout / "tracked.py").write_text("VALUE = 2\n")
    with pytest.raises(permit.PermitError, match="dirty"):
        permit.check_source(checkout, {"source_head": head})


def test_private_git_askpass_uses_token_file_without_embedding_secret(tmp_path):
    token = tmp_path / "token"
    token.write_text("example-secret\n")
    token.chmod(0o600)
    helper = worker._askpass(tmp_path, token)
    assert "example-secret" not in helper.read_text()
    environment = {"SKYBUILD_GIT_TOKEN_FILE": str(token), "PATH": os.environ["PATH"]}
    username = subprocess.run([str(helper), "Username for HTTPS"], env=environment,
                              check=True, capture_output=True).stdout
    password = subprocess.run([str(helper), "Password for HTTPS"], env=environment,
                              check=True, capture_output=True).stdout
    assert username == b"x-access-token\n"
    assert password == b"example-secret\n"


def test_exact_one_shot_permit_requires_valid_usage_and_resource_headroom(tmp_path, monkeypatch):
    monkeypatch.setattr(permit, "check_source", lambda *_args: None)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "base"], check=True)
    head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    now = datetime.now(timezone.utc)
    future = (now + timedelta(minutes=10)).isoformat()
    weekly = tmp_path / "weekly.json"
    weekly.write_text(json.dumps({"production_must_drain": False,
        "stop_production_percent": 50, "weekly_used_percent": 42,
        "confirmed_at": now.isoformat(), "valid_until": future}))
    host = tmp_path / "host.json"
    host.write_text(json.dumps({"sampled_at": now.isoformat(), "status": "ok",
        "reserve_bytes": 8 * 1024**3, "available_bytes": 12 * 1024**3,
        "capacity": {"status": "ok", "max_new_jobs": 2}}))
    selected = [{"task_id": "TASK-1", "worker": "worker_a", "assignment_id": "A-1",
                 "brief_path": "docs/design/assignments/a.json", "branch": "task/a",
                 "patch_sha256": "a" * 64, "base_sha": head, "brief_sha256": "c" * 64,
                 "revision": 2, "envelope": {"assignment_id": "A-1", "base_sha": head}},
                {"task_id": "TASK-2", "worker": "worker_b", "assignment_id": "A-2",
                 "brief_path": "docs/design/assignments/b.json", "branch": "task/b",
                 "patch_sha256": "b" * 64, "base_sha": head, "brief_sha256": "d" * 64,
                 "revision": 2, "envelope": {"assignment_id": "A-2", "base_sha": head}}]
    approved = {"schema": "skybuild.auto-cpu-patch-permit.v1",
        "profile": "bounded-trusted-cpu-patch-v1", "project_id": "skybuild",
        "host_id": socket.gethostname(), "source_head": head, "base_ref": "refs/heads/dev-006",
        "slots": 2, "approved_until": future,
        "weekly_usage_sha256": hashlib.sha256(weekly.read_bytes()).hexdigest(),
        "hostwatch_reserve_bytes": 8 * 1024**3,
        "memory_high_bytes": 1024**3, "memory_max_bytes": 2 * 1024**3,
        "runtime_seconds": 600, "workers": [
            {**{key: item[key] for key in ("task_id", "worker", "assignment_id", "brief_path",
                                            "brief_sha256", "branch", "base_sha", "revision", "patch_sha256")},
             "envelope_sha256": permit.envelope_sha256(item["envelope"])} for item in selected]}
    approved_file = tmp_path / "permit.json"
    approved_file.write_text(json.dumps(approved))
    digest = hashlib.sha256(approved_file.read_bytes()).hexdigest()
    assert permit.load(approved_file, digest, checkout=checkout, selected=selected,
                       project="skybuild", base_ref="refs/heads/dev-006",
                       hostwatch=host, usage=weekly) == approved
    selected[0]["base_sha"] = "f" * 40
    with pytest.raises(permit.PermitError, match="Selected task"):
        permit.load(approved_file, digest, checkout=checkout, selected=selected,
                    project="skybuild", base_ref="refs/heads/dev-006",
                    hostwatch=host, usage=weekly)
    selected[0]["base_sha"] = head
    selected[0]["brief_sha256"] = "e" * 64
    with pytest.raises(permit.PermitError, match="Selected task"):
        permit.load(approved_file, digest, checkout=checkout, selected=selected,
                    project="skybuild", base_ref="refs/heads/dev-006",
                    hostwatch=host, usage=weekly)
    selected[0]["brief_sha256"] = "c" * 64
    approved["workers"][1]["patch_sha256"] = "c" * 64
    approved_file.write_text(json.dumps(approved))
    with pytest.raises(permit.PermitError, match="bytes changed"):
        permit.load(approved_file, digest, checkout=checkout, selected=selected,
                    project="skybuild", base_ref="refs/heads/dev-006",
                    hostwatch=host, usage=weekly)
