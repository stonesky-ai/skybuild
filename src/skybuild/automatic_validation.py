"""Static pre-freeze proof for two exact approved CPU patch submissions.

This reads Git objects and parses Python syntax as data. It never imports a
candidate module or executes candidate tests; signed focused test receipts are
verified separately before recording unit/long validation outcomes.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile

from .store import Store
from .workflow import Place


class AutomaticValidationError(ValueError):
    pass


_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _git(checkout: Path, *args: str, data: bytes | None = None,
         extra_env: dict | None = None) -> bytes:
    import os
    # Keep the owner's approved read-only credential helper for the private
    # origin, as prepare_bundle does. Strip inherited Git override variables;
    # candidate source never supplies this process environment.
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("GIT_")}
    env.update(LC_ALL="C", GIT_NO_REPLACE_OBJECTS="1",
               GIT_TERMINAL_PROMPT="0")
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(checkout),
                             "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                             *args], input=data, env=env, capture_output=True, timeout=30)
    if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
        raise AutomaticValidationError("Bounded static Git observation failed")
    return result.stdout


def _oid(value: str) -> str:
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        raise AutomaticValidationError("Exact Git object ID is required")
    return value


def static_submission(checkout: Path, view: dict, task: dict,
                      *, target_ref: str, patch: bytes,
                      allowed_places: tuple[Place, ...] = (Place.VALIDATING,)) -> dict:
    """Compare the submitted head/tree with one approved patch on exact base."""
    token = Store.workflow_token(view["task"])
    if (view.get("token") != token.to_dict() or token.place not in allowed_places
            or token.pending_action is not None or token.superseded
            or token.task_id != task.get("task_id") or token.responsible != task.get("worker_id")
            or token.source_branch != task.get("task_branch")
            or token.target_base != task.get("base_sha")
            or token.policy_version != task.get("policy_version")
            or token.definition_revision != task.get("definition_revision")):
        raise AutomaticValidationError("Worker submission differs from approved task and live attempt")
    base = _oid(token.target_base)
    head = _oid(token.source_head)
    if (not isinstance(patch, bytes) or not patch
            or hashlib.sha256(patch).hexdigest() != task.get("approved_patch_sha256")
            or not _SHA.fullmatch(task["approved_patch_sha256"])):
        raise AutomaticValidationError("Approved deterministic patch bytes differ")
    remote = _git(checkout, "ls-remote", "--refs", "origin", task["task_branch"]).decode().strip().split()
    if remote != [head, task["task_branch"]]:
        raise AutomaticValidationError("Pushed worker branch differs from submitted head")
    target = _git(checkout, "ls-remote", "--refs", "origin", target_ref).decode().strip().split()
    if target != [base, target_ref]:
        raise AutomaticValidationError("Development base moved; rebase is required")
    _git(checkout, "fetch", "--no-tags", "--no-write-fetch-head", "--refmap=",
         "origin", task["task_branch"])
    if _git(checkout, "ls-remote", "--refs", "origin", task["task_branch"]).decode().strip().split() != remote:
        raise AutomaticValidationError("Worker branch moved while its exact object was fetched")
    parents = _git(checkout, "show", "-s", "--format=%P", head).decode().strip().split()
    if parents != [base]:
        raise AutomaticValidationError("Worker head must be one commit on the approved base")
    raw_paths = _git(checkout, "diff-tree", "--no-commit-id", "--no-renames", "-r",
                     "--name-only", "-z", base, head)
    paths = [part.decode("utf-8") for part in raw_paths.split(b"\0") if part]
    if (not paths or len(paths) > 32 or not isinstance(task.get("owned_paths"), list)
            or any(path not in task["owned_paths"] or path.startswith(".git/")
                   or ".." in Path(path).parts for path in paths)):
        raise AutomaticValidationError("Worker changed paths outside approved ownership")
    modes = {}
    for entry in _git(checkout, "ls-tree", "-r", "-z", head).split(b"\0"):
        if not entry:
            continue
        meta, _, name = entry.partition(b"\t")
        decoded = name.decode("utf-8")
        if decoded in paths:
            modes[decoded] = meta.split(b" ", 1)[0]
    if set(modes) != set(paths) or any(mode != b"100644" for mode in modes.values()):
        raise AutomaticValidationError("Worker output contains missing, executable, or link paths")
    base_tree = _git(checkout, "rev-parse", base + "^{tree}").decode().strip()
    submitted_tree = _git(checkout, "rev-parse", head + "^{tree}").decode().strip()
    with tempfile.TemporaryDirectory(prefix="skybuild-static-validation-") as temporary:
        scratch = Path(temporary)
        objects = scratch / "objects"
        objects.mkdir()
        env = {"GIT_INDEX_FILE": str(scratch / "index"),
               "GIT_OBJECT_DIRECTORY": str(objects),
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(checkout / ".git/objects")}
        _git(checkout, "read-tree", base_tree, extra_env=env)
        _git(checkout, "apply", "--cached", "--whitespace=nowarn", "-", data=patch,
             extra_env=env)
        approved_tree = _git(checkout, "write-tree", extra_env=env).decode().strip()
    if submitted_tree != approved_tree:
        raise AutomaticValidationError("Worker tree differs from approved patch application")
    for path in paths:
        if path.endswith(".py"):
            source = _git(checkout, "show", head + ":" + path)
            try:
                ast.parse(source, filename=path)
            except SyntaxError as error:
                raise AutomaticValidationError("Submitted Python syntax is invalid") from error
    return {"schema": "skybuild.static-worker-validation.v1", "task_id": token.task_id,
            "attempt_id": token.attempt_id, "claim_fence": token.claim_fence,
            "input_generation": token.input_generation,
            "definition_revision": token.definition_revision,
            "policy_version": token.policy_version, "worker_id": token.responsible,
            "source_branch": token.source_branch, "source_head": head,
            "base_sha": base, "approved_patch_sha256": task["approved_patch_sha256"],
            "approved_tree": approved_tree, "changed_paths": paths,
            "python_syntax": "passed", "target_ref": target_ref,
            "target_head": base}
