#!/usr/bin/env python3
"""Initialize, verify and sync this checkout's local CodeGraph CLI index.

This script checks the CLI only. Each agent must separately verify its own MCP session.
"""
from __future__ import annotations

import argparse
from _repo_guard import verify_skybuild, RepoGuardError
import json
from pathlib import Path
import subprocess


def prepare(checkout: Path, query: str | None = None) -> dict:
    def run(argv):
        return subprocess.run(argv, cwd=checkout, check=True, text=True,
                              capture_output=True, timeout=180).stdout

    ignored = subprocess.run(["git", "check-ignore", "--quiet", "--no-index", ".codegraph/"],
                             cwd=checkout, check=False, capture_output=True).returncode == 0
    tracked = run(["git", "ls-files", "--", ".codegraph"])
    if not ignored or tracked.strip():
        raise RuntimeError(".codegraph/ must be ignored and untracked before indexing")
    initialized = not (checkout / ".codegraph").exists()
    if initialized:
        run(["codegraph", "init", "--yes", str(checkout)])

    def status():
        data = json.loads(run(["codegraph", "status", "--json", str(checkout)]))
        if not data.get("initialized") or Path(data.get("projectPath", "")).resolve() != checkout:
            raise RuntimeError("CodeGraph project path does not match the requested checkout")
        if data.get("worktreeMismatch"):
            raise RuntimeError("CodeGraph reports a worktree mismatch")
        return data

    status()
    run(["codegraph", "sync", str(checkout)])
    data = status()
    result = {"ok": True, "project": str(checkout), "initialized": initialized,
              "ignored": True, "files": data.get("fileCount"), "symbols": data.get("nodeCount"),
              "mcp_verified": False}
    if query:
        result["query"] = json.loads(run(["codegraph", "query", "--path", str(checkout),
                                          "--limit", "5", "--json", query]))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--query", help="Optional symbol query, limited to five results")
    args = parser.parse_args()
    try:
        result = prepare(verify_skybuild(args.checkout.resolve()), args.query)
    except (RepoGuardError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
