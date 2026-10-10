#!/usr/bin/env python3
"""Print a bounded, read-only snapshot of one SkyBuild checkout and task."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import time

from skybuild.client import Client
from skybuild.contracts import valid_identifier
from skybuild.fleet_preflight import _token_from_file


COMMAND_TIMEOUT = 5.0
COMMAND_OUTPUT_LIMIT = 16_384
MAX_DIRTY_PATHS = 20
MAX_VALIDATIONS = 10
MAX_TEXT = 2_000


def _clean_command_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in tuple(env):
        if name.startswith("GIT_"):
            env.pop(name)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    return env


def _stop(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def run_bounded(argv: list[str], *, cwd: Path, timeout: float = COMMAND_TIMEOUT,
                output_limit: int = COMMAND_OUTPUT_LIMIT) -> tuple[str, str, int | None]:
    """Run one command with a hard stdout cap, deadline, and no echoed stderr."""
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=_clean_command_env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=(os.name == "posix"),
        )
    except (OSError, ValueError):
        return "unavailable", "", None

    output = bytearray()
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    state = "ok"
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                state = "timeout"
                _stop(process)
                break
            chunk = os.read(process.stdout.fileno(), min(4096, output_limit + 1 - len(output)))
            if not chunk:
                selector.unregister(process.stdout)
                continue
            output.extend(chunk)
            if len(output) > output_limit:
                state = "output_limit"
                _stop(process)
                break
        if process.poll() is None:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                state = "timeout"
                _stop(process)
        return state, bytes(output[:output_limit]).decode("utf-8", errors="replace"), process.poll()
    finally:
        selector.close()
        if process.poll() is None:
            _stop(process)
            process.wait()
        process.stdout.close()


def _git(checkout: Path, *args: str, limit: int = COMMAND_OUTPUT_LIMIT) -> tuple[str, str, int | None]:
    return run_bounded(["git", *args], cwd=checkout, output_limit=limit)


def _verify_checkout(checkout: Path) -> Path | None:
    guard = ("import sys; from pathlib import Path; from _repo_guard import verify_skybuild; "
             "print(verify_skybuild(Path(sys.argv[1])))")
    state, output, code = run_bounded([sys.executable, "-c", guard, str(checkout)], cwd=checkout,
                                      output_limit=2_048)
    if state != "ok" or code != 0:
        return None
    try:
        verified = Path(output.strip()).resolve()
    except (OSError, ValueError):
        return None
    return verified if verified == checkout else None


def checkout_snapshot(checkout: Path) -> dict:
    requested = checkout.expanduser().resolve()
    verified = _verify_checkout(requested)
    if verified is None:
        return {"status": "unavailable", "reason": "checkout_verification_failed"}

    commands = {
        "head": ("rev-parse", "HEAD"),
        "branch": ("branch", "--show-current"),
        "status": ("status", "--porcelain=v1", "--untracked-files=normal"),
    }
    values: dict[str, str] = {}
    for name, args in commands.items():
        state, output, code = _git(verified, *args, limit=32_768 if name == "status" else 2_048)
        if state != "ok" or code != 0:
            return {"status": "unavailable", "reason": f"git_{name}_{state if state != 'ok' else 'failed'}"}
        values[name] = output.strip()

    head = values["head"]
    if len(head) not in {40, 64} or any(char not in "0123456789abcdef" for char in head):
        return {"status": "unavailable", "reason": "git_head_invalid"}

    paths = []
    status_lines = values["status"].splitlines()
    for line in status_lines[:MAX_DIRTY_PATHS]:
        path = line[3:] if len(line) > 3 else ""
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        paths.append(path[:300])
    return {
        "status": "available",
        "path": str(verified),
        "git_root": str(verified),
        "origin": {"repository": "stonesky-ai/skybuild", "fetch_verified": True, "push_verified": True},
        "head": head,
        "branch": values["branch"][:200],
        "dirty": {"present": bool(status_lines), "path_count": len(status_lines),
                  "paths": paths, "truncated": len(status_lines) > MAX_DIRTY_PATHS},
    }


def _text(value: object, *, limit: int = MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit]


def summarize_task(view: object, expected_project_id: str, expected_task_id: str) -> dict:
    if not isinstance(view, dict) or not isinstance(view.get("task"), dict):
        return {"status": "unavailable", "reason": "invalid_response"}
    task = view["task"]
    token = view.get("token")
    if (not isinstance(token, dict) or task.get("task_id") != expected_task_id
            or task.get("project_id") != expected_project_id):
        return {"status": "unavailable", "reason": "task_identity_mismatch"}
    revision = task.get("revision")
    if type(revision) is not int or revision < 1:
        return {"status": "unavailable", "reason": "task_revision_invalid"}
    task_status = _text(task.get("status"), limit=100)
    place = _text(token.get("place"), limit=100)
    if task_status is None or place is None:
        return {"status": "unavailable", "reason": "workflow_state_invalid"}
    validations = []
    raw_validations = task.get("validation", [])
    if isinstance(raw_validations, list):
        for result in raw_validations[:MAX_VALIDATIONS]:
            if isinstance(result, dict):
                stage, state = _text(result.get("stage"), limit=100), _text(result.get("state"), limit=100)
                if stage is not None and state is not None:
                    validations.append({"stage": stage, "state": state})
    actions = view.get("available_actions", [])
    if not isinstance(actions, list):
        actions = []
    actions = [item[:100] for item in actions[:20] if isinstance(item, str)]
    truncated_fields = [name for name, limit in (("next_action", MAX_TEXT),
                                                   ("blocker", MAX_TEXT),
                                                   ("responsible", 200))
                        if isinstance(task.get(name), str) and len(task[name]) > limit]
    return {
        "status": "available",
        "task": {
            "task_id": expected_task_id,
            "project_id": expected_project_id,
            "revision": revision,
            "status": task_status,
            "place": place,
            "next_action": _text(task.get("next_action")),
            "blocker": _text(task.get("blocker")),
            "responsible": _text(task.get("responsible"), limit=200),
            "evidence_freshness": _text(task.get("evidence_freshness"), limit=100),
            "validation": validations,
            "validation_truncated": isinstance(raw_validations, list) and len(raw_validations) > MAX_VALIDATIONS,
            "available_actions": actions,
            "truncated_fields": truncated_fields,
        },
    }


def fetch_task_snapshot(client: Client, project_id: str, task_id: str) -> dict:
    try:
        return summarize_task(client.task_workflow(project_id, task_id), project_id, task_id)
    except Exception:
        return {"status": "unavailable", "reason": "task_request_failed"}


def codegraph_snapshot(checkout: Path, query: str | None) -> dict:
    if not query:
        return {"status": "not_requested"}
    if len(query) > 1_000 or "\x00" in query:
        return {"status": "unavailable", "reason": "query_invalid"}
    checkout = checkout.expanduser().resolve()
    index = checkout / ".codegraph"
    if not index.is_dir() or index.is_symlink():
        return {"status": "unavailable", "reason": "index_missing_or_unsafe"}
    if shutil.which("codegraph") is None:
        return {"status": "unavailable", "reason": "codegraph_cli_missing"}

    state, output, code = run_bounded(["codegraph", "status", "--json", str(checkout)], cwd=checkout)
    if state != "ok" or code != 0:
        return {"status": "unavailable", "reason": f"status_{state if state != 'ok' else 'failed'}"}
    try:
        info = json.loads(output)
        if not isinstance(info, dict):
            return {"status": "unavailable", "reason": "status_invalid"}
        project = Path(info.get("projectPath", "")).resolve()
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return {"status": "unavailable", "reason": "status_invalid"}
    if (info.get("initialized") is not True or project != checkout or info.get("worktreeMismatch")):
        return {"status": "unavailable", "reason": "project_path_mismatch"}

    state, output, code = run_bounded(
        ["codegraph", "explore", "--path", str(checkout), "--max-files", "3", query],
        cwd=checkout, timeout=10.0, output_limit=12_000,
    )
    if state != "ok" or code != 0:
        return {"status": "unavailable", "reason": f"explore_{state if state != 'ok' else 'failed'}"}
    file_count = info.get("fileCount")
    node_count = info.get("nodeCount")
    index_summary = {
        "file_count": file_count if type(file_count) is int and file_count >= 0 else None,
        "node_count": node_count if type(node_count) is int and node_count >= 0 else None,
    }
    return {"status": "available", "project_path": str(project), "index": index_summary,
            "output": output, "truncated": False}


def snapshot(*, checkout: Path, project_id: str, task_id: str, url: str,
             token_file: Path, ca_file: Path | None = None,
             graph_query: str | None = None) -> dict:
    result = {
        "checkout": checkout_snapshot(checkout),
        "task": {"status": "unavailable", "reason": "not_requested"},
        "codegraph": codegraph_snapshot(checkout, graph_query),
    }
    if not valid_identifier(project_id) or not valid_identifier(task_id):
        result["task"] = {"status": "unavailable", "reason": "invalid_identifier"}
        return result
    try:
        token = _token_from_file(token_file)
        with Client(url, token, retries=0, timeout=5, trust_env=False, ca_file=ca_file) as client:
            result["task"] = fetch_task_snapshot(client, project_id, task_id)
    except Exception:
        result["task"] = {"status": "unavailable", "reason": "credentials_or_api_unavailable"}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--graph-query", help="Optional caller-supplied query; requires existing exact-checkout index")
    args = parser.parse_args()
    print(json.dumps(snapshot(checkout=args.checkout, project_id=args.project, task_id=args.task_id,
                              url=args.url, token_file=args.token_file, ca_file=args.ca_file,
                              graph_query=args.graph_query), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
