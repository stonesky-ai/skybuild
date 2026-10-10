"""Exact pre-freeze isolated validation for the two approved worker heads.

The trusted source process creates data-only Git checkouts. Candidate Python is
executed only by the reviewed isolated gate runner in its container profile.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess


class FocusedError(ValueError):
    pass


def _git(root: Path, *arguments: str, timeout: int = 120) -> str:
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(root),
                             "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                             *arguments], env=env, capture_output=True, timeout=timeout)
    if result.returncode or len(result.stdout) > 65536:
        raise FocusedError("Exact candidate Git preparation failed")
    return result.stdout.decode("utf-8").strip()


def candidate_checkout(source: Path, state: Path, head: str) -> Path:
    """Prepare one clean detached copy of an already observed exact Git head."""
    if not isinstance(head, str) or re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise FocusedError("Candidate checkout needs an exact commit")
    root = state / "candidate-checkouts"
    root.mkdir(mode=0o700, exist_ok=True)
    if (root.is_symlink() or root.stat().st_uid != os.geteuid()
            or stat.S_IMODE(root.stat().st_mode) != 0o700):
        raise FocusedError("Candidate checkout parent is not private")
    checkout = root / head
    if checkout.is_symlink():
        raise FocusedError("Candidate checkout path is a symlink")
    if not checkout.exists():
        checkout.mkdir(mode=0o700)
        _git(checkout, "init", "-q")
        _git(checkout, "remote", "add", "origin", "https://github.com/stonesky-ai/skybuild.git")
        _git(checkout, "fetch", "--no-tags", "--no-recurse-submodules", str(source), head)
        _git(checkout, "checkout", "-q", "--detach", head)
    if (checkout.stat().st_uid != os.geteuid()
            or stat.S_IMODE(checkout.stat().st_mode) != 0o700
            or _git(checkout, "rev-parse", "--show-toplevel") != str(checkout)
            or _git(checkout, "remote", "get-url", "origin") != "https://github.com/stonesky-ai/skybuild.git"
            or _git(checkout, "rev-parse", "HEAD") != head
            or _git(checkout, "status", "--porcelain", "--untracked-files=all")):
        raise FocusedError("Candidate checkout is not the clean approved Git head")
    return checkout


def signed_input(payload: dict, signer: dict, key: bytes, domain: str) -> dict:
    if len(key) < 32:
        raise FocusedError("Focused integration signing key is too short")
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           allow_nan=False, separators=(",", ":")).encode()
    return {"schema": domain, "key_id": signer["key_id"],
            "principal": signer["principal"], "payload": payload,
            "signature": hmac.new(key, domain.encode() + b"\0" + canonical,
                                  hashlib.sha256).hexdigest()}


def focused_payload(policy: dict, task: dict, token, *, stage: str,
                    permit_sha256: str, conductor_intent_sha256: str,
                    source_tree: str, archive_sha256: str,
                    history_sha256: str) -> dict:
    if (stage not in {"unit", "long"} or token.task_id != task["task_id"]
            or token.responsible != task["worker_id"]
            or token.source_branch != task["task_branch"]
            or token.target_base != policy["base_sha"]
            or token.definition_revision != task["definition_revision"]
            or token.policy_version != task["policy_version"]
            or not token.source_head):
        raise FocusedError("Focused validation differs from exact submitted worker attempt")
    return {"schema": "skybuild.focused-validation-input.v1",
            "policy_sha256": permit_sha256, "project_id": policy["project_id"],
            "target_ref": policy["target_ref"], "base_sha": policy["base_sha"],
            "task_id": task["task_id"], "assignment_id": task["assignment_id"],
            "worker_id": task["worker_id"], "brief_sha256": task["brief_sha256"],
            "head_sha": token.source_head,
            "workflow": {"attempt_id": token.attempt_id,
                         "claim_fence": token.claim_fence,
                         "input_generation": token.input_generation,
                         "definition_revision": token.definition_revision,
                         "policy_version": token.policy_version,
                         "source_sha": token.source_head,
                         "base_sha": token.target_base},
            "candidate_tree": source_tree,
            "candidate_archive_sha256": archive_sha256,
            "candidate_history_sha256": history_sha256,
            "conductor_intent_sha256": conductor_intent_sha256,
            "submitted_at": datetime.now(timezone.utc).isoformat(), "stage": stage}
