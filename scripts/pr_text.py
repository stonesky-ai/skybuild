#!/usr/bin/env python3
"""Publish literal UTF-8 PR text with gh; body writes are read back exactly."""
import argparse
import json
from pathlib import Path
import subprocess
from _repo_guard import RepoGuardError, verify_skybuild


def run(argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, check=True, text=True, capture_output=True).stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["body", "comment"])
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--repo", help="OWNER/REPO; otherwise gh uses this checkout")
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    if args.pr <= 0:
        parser.error("--pr must be positive")
    checkout = args.checkout.resolve()
    verify_skybuild(checkout)
    if args.repo not in (None, "stonesky-ai/skybuild"):
        raise RuntimeError("Expected an explicit SkyBuild checkout with stonesky-ai/skybuild origin")
    # read_bytes avoids newline conversion and validates UTF-8 before publication.
    expected = args.file.read_bytes().decode("utf-8")
    repo = ["--repo", "stonesky-ai/skybuild"]
    action = "edit" if args.action == "body" else "comment"
    run(["gh", "pr", action, str(args.pr), *repo, "--body-file", str(args.file.resolve())], checkout)
    if args.action == "body":
        actual = json.loads(run(["gh", "pr", "view", str(args.pr), *repo, "--json", "body"], checkout))["body"]
        if actual != expected:
            raise RuntimeError("Published PR body does not match the UTF-8 file")
    print(json.dumps({"ok": True, "action": args.action, "pr": args.pr,
                      "verified": args.action == "body"}, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except (OSError, UnicodeError, RuntimeError, RepoGuardError, subprocess.CalledProcessError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, separators=(",", ":")))
        raise SystemExit(1)
