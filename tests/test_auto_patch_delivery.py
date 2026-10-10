"""Policy and real Git behavior for the bounded automatic CPU patch route."""

import hashlib
import json
from pathlib import Path
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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

    def local_git(repo, *args, timeout=30):
        if args[:3] == ("remote", "get-url", "origin") or args[:4] == (
                "remote", "get-url", "--push", "origin"):
            return b"https://github.com/stonesky-ai/skybuild.git\n"
        return original(repo, *args, timeout=timeout)

    monkeypatch.setattr(worker, "_git", local_git)
    head, paths = worker._clone_and_apply(source, destination,
                                          {"base_sha": base, "branch": "task/approved-cpu-note",
                                           "task_id": "SKYBUILD-CPU-NOTE",
                                           "owned_paths": ["README.md"]}, patch)
    assert paths == ["README.md"]
    assert git(destination, "show", "HEAD:README.md") == b"Original\nUseful new note.\n"
    assert git(destination, "rev-list", "--parents", "-n", "1", head).decode().split() == [head, base]


def test_controller_source_mount_excludes_ignored_local_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    (repo / ".gitignore").write_text("local-secret.txt\n")
    (repo / "tracked.md").write_text("public source\n")
    (repo / "local-secret.txt").write_text("host-only secret\n")
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://github.com/stonesky-ai/skybuild.git"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore", "tracked.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()

    mount = controller._prepare_worker_source(repo, tmp_path / "worker-source", base)
    assert (mount / "tracked.md").read_text() == "public source\n"
    assert not (mount / "local-secret.txt").exists()
    assert subprocess.run(["git", "-C", str(mount), "status", "--porcelain"],
                          check=True, capture_output=True).stdout == b""


def test_controller_push_reconciles_uncertain_create_only_push_without_retry(tmp_path, monkeypatch):
    def git(repo, *args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout

    source = tmp_path / "checkout"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.name", "Test Author")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "docs/design/assignments").mkdir(parents=True)
    (source / "docs/design/architecture.md").write_text("architecture\n")
    (source / "README.md").write_text("base\n")
    assignment = {"schema": "manual-work-v2", "assignment_id": "ASSIGN-CPU-TEST",
        "task_id": "SKYBUILD-CPU-TEST", "worker": "worker_a", "dispatcher": "pilot_dispatcher",
        "base_sha": "0" * 40, "brief_path": "docs/design/assignments/relay.json",
        "brief_sha256": "0" * 64, "branch": "task/cpu-relay-test", "owned_paths": ["README.md"],
        "checks": ["Patch accepted"], "model_limit": "No candidate execution",
        "task_status": "in-progress", "task_revision": 3}
    brief = {key: value for key, value in assignment.items()
             if key not in {"base_sha", "brief_path", "brief_sha256", "task_status", "task_revision"}}
    brief.update(schema="manual-work-brief-v1", next_action="Independent review")
    brief_bytes = json.dumps(brief, sort_keys=True, separators=(",", ":")).encode()
    (source / assignment["brief_path"]).write_bytes(brief_bytes)
    git(source, "add", "docs", "README.md")
    git(source, "commit", "-qm", "base")
    base = git(source, "rev-parse", "HEAD").decode().strip()
    assignment["base_sha"] = base
    assignment["brief_sha256"] = hashlib.sha256(brief_bytes).hexdigest()
    origin = "https://github.com/stonesky-ai/skybuild.git"
    git(source, "remote", "add", "origin", origin)
    git(source, "remote", "set-url", "--push", "origin", origin)
    (source / "README.md").write_text("base\nreviewed change\n")
    patch_bytes = git(source, "diff", "--", "README.md")
    (source / "README.md").write_text("base\n")

    state = tmp_path / "state"
    assignment_dir, output = state / "assignment", state / "output"
    assignment_dir.mkdir(parents=True, mode=0o700)
    output.mkdir(mode=0o700)
    input_dir = state / "input"
    input_dir.mkdir(mode=0o700)
    patch_file = input_dir / "approved.patch"
    patch_file.write_bytes(patch_bytes)
    patch_file.chmod(0o400)
    digest = hashlib.sha256(patch_bytes).hexdigest()
    assignment_file = assignment_dir / "assignment.json"
    assignment_file.write_text(json.dumps(assignment))
    assignment_file.chmod(0o600)
    preclaim = {"assignment_id": assignment["assignment_id"], "place": "working",
                "attempt_id": "attempt-relay-test", "claim_fence": 9}
    preclaim_file = assignment_dir / "preclaim.json"
    preclaim_file.write_text(json.dumps(preclaim))
    preclaim_file.chmod(0o600)
    workflow = {"token": {"attempt_id": preclaim["attempt_id"], "claim_fence": 9,
                           "input_generation": 2, "definition_revision": 3,
                           "policy_version": "policy-test"}}
    workflow_file = assignment_dir / "assignment.json.workflow.json"
    workflow_file.write_text(json.dumps(workflow))
    workflow_file.chmod(0o600)
    terminal = worker.run(worker="worker_a", checkout=source, assignment_path=assignment_file,
                          preclaim_path=preclaim_file, patch_path=patch_file,
                          patch_sha256=digest, state_dir=output)
    log = state / "worker.log"
    log.write_text(json.dumps(terminal, sort_keys=True) + "\n")
    log.chmod(0o600)
    remote = tmp_path / "remote.git"
    git(None, "init", "--bare", "-q", str(remote))
    plan = SimpleNamespace(assignment_dir=assignment_dir, worker_output_dir=output,
        patch_file=patch_file, patch_digest=digest, external_state_dir=state,
        checkout=source, project_id="skybuild", worker_id="worker_a")
    prepared = SimpleNamespace(plan=plan, task_id=assignment["task_id"],
        assignment_id=assignment["assignment_id"], attempt_id=preclaim["attempt_id"],
        claim_fence=9, operation_id="operation-relay-test")
    pushes = []
    def controller_git(repo, *args, **kwargs):
        rewritten = tuple(str(remote) if arg == origin else arg for arg in args)
        result = subprocess.run(["git", *rewritten], cwd=repo, check=True,
                                capture_output=True, timeout=10)
        if rewritten and rewritten[0] == "push":
            pushes.append(rewritten)
            raise controller.AutoControllerError("simulate lost push acknowledgement")
        return result.stdout
    monkeypatch.setattr(controller, "_controller_git", controller_git)
    intent = controller._publish_worker_output(prepared, {"log": str(log)})
    assert len(pushes) == 1
    assert intent["source_head"] == terminal["head_sha"]
    assert intent["source_branch"] == "refs/heads/" + assignment["branch"]
    assert json.loads((assignment_dir / "push-confirmed.json").read_text())["remote_head"] == terminal["head_sha"]
    assert (assignment_dir / "result-intent.json").is_file()


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


def test_worker_rechecks_exact_permit_and_assignment_before_work(tmp_path, monkeypatch):
    checked = []
    monkeypatch.setattr(permit, "check_source", lambda *_args, **kwargs: checked.append(kwargs))
    assignment = {"task_id": "SKYBUILD-CPU-1", "assignment_id": "ASSIGN-1",
                  "brief_path": "docs/design/assignments/a.json", "brief_sha256": "b" * 64,
                  "branch": "task/cpu-1", "base_sha": "a" * 40, "task_revision": 2}
    expiry = datetime.now(timezone.utc) + timedelta(minutes=10)
    entry = {"task_id": assignment["task_id"], "worker": "worker_1",
             "assignment_id": assignment["assignment_id"], "brief_path": assignment["brief_path"],
             "brief_sha256": assignment["brief_sha256"], "branch": assignment["branch"],
             "base_sha": assignment["base_sha"], "revision": 2, "patch_sha256": "c" * 64,
             "envelope_sha256": permit.envelope_sha256(assignment)}
    approved = {"schema": "skybuild.auto-cpu-patch-permit.v1",
                "profile": "bounded-trusted-cpu-patch-v1", "slots": 2,
                "project_id": "skybuild", "host_id": socket.gethostname(),
                "approved_until": expiry.isoformat(), "source_head": "d" * 40,
                "workers": [entry, {"different": True}]}
    path = tmp_path / "approved.json"
    path.write_text(json.dumps(approved))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    permit.check_worker_permit(path, digest, checkout=tmp_path, assignment=assignment,
                               project="skybuild", worker="worker_1", patch_sha256="c" * 64,
                               approved_until=expiry)
    assert checked == [{"require_job_unit": False}]
    with pytest.raises(permit.PermitError, match="assignment differs"):
        permit.check_worker_permit(path, digest, checkout=tmp_path, assignment=assignment,
                                   project="skybuild", worker="worker_1", patch_sha256="e" * 64,
                                   approved_until=expiry)


def test_worker_has_no_git_or_api_credential_inputs():
    source = Path(worker.__file__).read_text()
    assert "git_token_file" not in source
    assert "send_result" not in source
    assert "renew_assignment" not in source


@pytest.mark.parametrize("revised", [False, True])
def test_exact_one_shot_permit_requires_valid_usage_and_resource_headroom(tmp_path, monkeypatch, revised):
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
        "runtime_seconds": 600, "worker_image_id": "sha256:" + "e" * 64, "workers": [
            {**{key: item[key] for key in ("task_id", "worker", "assignment_id", "brief_path",
                                            "brief_sha256", "branch", "base_sha", "revision", "patch_sha256")},
             "envelope_sha256": permit.envelope_sha256(item["envelope"])} for item in selected]}
    approved_file = tmp_path / "permit.json"
    if revised:
        weekly.write_text(json.dumps({"production_must_drain": True,
            "stop_production_percent": 50, "weekly_used_percent": 56,
            "confirmed_at": now.isoformat(), "valid_until": future}))
        owner = tmp_path / "owner.json"
        owner.write_text(json.dumps({"schema": "skybuild.usage-policy-owner-revision.v1",
            "production_allowed": True, "weekly_production_stop_percent": None}))
        del approved["weekly_usage_sha256"]
        approved["usage"] = {"path": str(weekly),
            "sha256": hashlib.sha256(weekly.read_bytes()).hexdigest(), "valid_until": future,
            "owner_policy": {"path": str(owner), "sha256": hashlib.sha256(owner.read_bytes()).hexdigest()}}
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


@pytest.fixture
def revised_usage(tmp_path):
    now = datetime.now(timezone.utc)
    future = (now + timedelta(minutes=10)).isoformat()
    observation = {"production_must_drain": True, "stop_production_percent": 50,
        "weekly_used_percent": 56, "confirmed_at": now.isoformat(), "valid_until": future}
    owner = {"schema": "skybuild.usage-policy-owner-revision.v1",
        "production_allowed": True, "weekly_production_stop_percent": None}
    usage_path, owner_path = tmp_path / "usage.json", tmp_path / "owner.json"
    usage_path.write_text(json.dumps(observation))
    owner_path.write_text(json.dumps(owner))
    approved = {"approved_until": future, "usage": {"path": str(usage_path),
        "sha256": hashlib.sha256(usage_path.read_bytes()).hexdigest(), "valid_until": future,
        "owner_policy": {"path": str(owner_path), "sha256": hashlib.sha256(owner_path.read_bytes()).hexdigest()}}}
    return usage_path, owner_path, observation, owner, approved


@pytest.mark.parametrize("change", ["digest", "schema", "stopped", "threshold", "missing_threshold", "symlink"])
def test_revised_usage_rechecks_pinned_owner_authority(revised_usage, change):
    usage_path, owner_path, _, owner, approved = revised_usage
    assert permit.check_weekly_usage(usage_path, approved)["weekly_used_percent"] == 56
    if change == "schema":
        owner["schema"] = "unknown"
    elif change == "stopped":
        owner["production_allowed"] = False
    elif change == "threshold":
        owner["weekly_production_stop_percent"] = 50
    elif change == "missing_threshold":
        del owner["weekly_production_stop_percent"]
    if change == "symlink":
        target = owner_path.with_suffix(".target")
        owner_path.rename(target)
        owner_path.symlink_to(target)
    else:
        owner_path.write_text(json.dumps(owner) + "\n")
        if change != "digest":
            approved["usage"]["owner_policy"]["sha256"] = hashlib.sha256(owner_path.read_bytes()).hexdigest()
    with pytest.raises(permit.PermitError):
        permit.check_weekly_usage(usage_path, approved)


@pytest.mark.parametrize("change", ["expired", "future", "window", "expiry_pin", "digest",
                                     "path", "boolean", "nan", "over_100", "no_revision"])
def test_revised_usage_preserves_freshness_and_observation_binding(revised_usage, change):
    usage_path, _, observation, _, approved = revised_usage
    now = datetime.now(timezone.utc)
    if change == "expired":
        observation["valid_until"] = (now - timedelta(seconds=1)).isoformat()
        approved["usage"]["valid_until"] = observation["valid_until"]
    elif change == "future":
        observation["confirmed_at"] = (now + timedelta(minutes=1)).isoformat()
    elif change == "window":
        approved["approved_until"] = (now + timedelta(minutes=11)).isoformat()
    elif change == "expiry_pin":
        approved["usage"]["valid_until"] = (now + timedelta(minutes=12)).isoformat()
    elif change == "path":
        approved["usage"]["path"] += ".other"
    elif change in {"boolean", "nan", "over_100"}:
        observation["weekly_used_percent"] = {"boolean": True, "nan": float("nan"), "over_100": 101}[change]
    elif change == "no_revision":
        del approved["usage"]["owner_policy"]
    usage_path.write_text(json.dumps(observation) + "\n")
    if change != "digest":
        approved["usage"]["sha256"] = hashlib.sha256(usage_path.read_bytes()).hexdigest()
    with pytest.raises(permit.PermitError):
        permit.check_weekly_usage(usage_path, approved)
