#!/usr/bin/env python3
"""List who holds which row: every `seam/*` branch on the shared remote (PAR-CLAIM).

A pushed `seam/<Id>` is the claim (ADR-0112 § 2, `scripts/claim.py`), so the
remote's branches are the one list of who holds what. For each it prints:

- **holder**: the newest `Agent:` trailer among the branch's own commits (those
  not on the trunk). Every commit is authored by the same configured user, so
  the author names nobody; when no commit carries a trailer the author is shown,
  marked as such.
- **pushed**: the tip's committer date. Git keeps no push time; the tip is the
  last thing pushed, and a claim commit is made the moment it is pushed.
- **evidence**: `releasable` when the tip has no `Lane:` evidence and the tip is
  older than `SKYKEEP_CLAIM_EVIDENCE_MAX_AGE_HOURS`; `-` otherwise. A tip that
  carries Lane evidence is never releasable, however old. The age is a whole
  number of hours from `--evidence-max-age` or that setting, and it has no
  default (ADR-0002): unset, blank, or not a whole number, the listing refuses
  rather than guessing. The owner's plan suggests 24.
- **ahead**: the branch's commits not on the trunk. 1 is a bare claim.

After that table it prints a second section, labelled `unpushed`, for each local
`refs/heads/seam/*` branch the remote does not have: the same holder, ahead and
last-commit date, plus the worktree that has it checked out. Such a branch holds
no claim (the push is the claim) but is work an agent left behind. The section is
absent when there is none, so the output is then unchanged.

The remote and the trunk branch come from `--remote` / `--trunk` or the
`SKYKEEP_GIT_REMOTE` / `SKYKEEP_GIT_TRUNK` settings, never a literal (ADR-0002).
Reading fetches the branches' objects only: no ref, no FETCH_HEAD, and never the
index, HEAD or the working tree.

    python3 scripts/claims.py [--remote <name>] [--trunk <branch>]
        [--evidence-max-age <hours>] [--root <repo>]

Exit: 0 listed (an empty list included), 3 refused.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

TRUNK_SETTING = "SKYKEEP_GIT_TRUNK"
AGE_SETTING = "SKYKEEP_CLAIM_EVIDENCE_MAX_AGE_HOURS"


def _claim():
    """`claim`, loaded from beside this file whatever the caller's sys.path."""
    spec = importlib.util.spec_from_file_location("claim", Path(__file__).resolve().parent / "claim.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


claim = _claim()
Refused = claim.Refused


def evidence_max_age(arg: str | None) -> timedelta:
    """How old an evidence-less claim must be before the list marks it releasable.

    `--evidence-max-age` wins; else `SKYKEEP_CLAIM_EVIDENCE_MAX_AGE_HOURS`.
    Neither set, blank, or not a positive whole number of hours refuses.
    There is no default (ADR-0002): guessing 24 would hide a claim the owner
    has not said is old.
    """
    raw = arg if arg is not None else (os.environ.get(AGE_SETTING) or "")
    raw = raw.strip()
    if not raw:
        raise Refused(
            f"no evidence age: pass --evidence-max-age or set {AGE_SETTING} "
            "(ADR-0002: how old an evidence-less claim must be before it is "
            "releasable is configuration, never a literal). An unset age is not guessed."
        )
    if re.fullmatch(r"[1-9][0-9]*", raw) is None:
        raise Refused(
            f"{AGE_SETTING} {raw!r} is not a whole number of hours; an age is not guessed"
        )
    return timedelta(hours=int(raw))


def trunk_name(arg: str | None) -> str:
    trunk = (arg or os.environ.get(TRUNK_SETTING) or "").strip()
    if not trunk:
        raise Refused(
            f"no trunk: pass --trunk <branch> or set {TRUNK_SETTING}"
            " (ADR-0002: the trunk is configuration, never a literal)"
        )
    return trunk


def claims(root: Path, remote: str, trunk: str) -> tuple[str, list[tuple[str, str, str, int]]]:
    """(the trunk's sha, [(branch, holder, pushed, ahead)] sorted by branch)."""
    base, rows, _names, _tips = _claims_and_names(root, remote, trunk)
    return base, rows


def _claims_and_names(root: Path, remote: str, trunk: str):
    """`claims`, plus every `seam/*` branch name the remote has."""
    trunk_ref = f"refs/heads/{trunk}"
    seam_refs = f"refs/heads/{claim.BRANCH_PREFIX}"
    out = claim.git(root, "ls-remote", remote, trunk_ref, f"{seam_refs}*").stdout
    tips: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == trunk_ref or ref.startswith(seam_refs):
            tips[ref] = sha
    base = tips.pop(trunk_ref, None)
    if base is None:
        raise Refused(f"{remote} has no branch {trunk!r}; commits ahead of it cannot be counted")
    claim.fetch(root, remote, trunk_ref, *sorted(tips))
    rows = []
    by_branch: dict[str, str] = {}
    for ref, tip in sorted(tips.items()):
        who = claim.holder(root, base, tip)
        pushed = claim.git(root, "log", "-1", "--format=%ci", tip).stdout.strip()
        ahead = int(claim.git(root, "rev-list", "--count", f"{base}..{tip}").stdout.strip())
        branch = ref.removeprefix("refs/heads/")
        rows.append((branch, str(who), pushed, ahead))
        by_branch[branch] = tip
    return base, rows, {ref.removeprefix("refs/heads/") for ref in tips}, by_branch


def worktrees(root: Path) -> dict[str, str]:
    """{local branch name: path of the worktree that has it checked out}."""
    out = claim.git(root, "worktree", "list", "--porcelain").stdout
    found: dict[str, str] = {}
    path = ""
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line.removeprefix("worktree ")
        elif line.startswith("branch refs/heads/"):
            found.setdefault(line.removeprefix("branch refs/heads/"), path)
    return found


def unpushed(root: Path, base: str, on_remote: set[str]) -> list[tuple[str, str, str, int, str]]:
    """[(branch, holder, last commit, ahead, worktree path or "")] for local seam
    branches the remote lacks, sorted by branch. Read-only."""
    prefix = f"refs/heads/{claim.BRANCH_PREFIX}"
    out = claim.git(root, "for-each-ref", "--format=%(refname)\t%(objectname)", prefix).stdout
    trees = worktrees(root)
    rows = []
    for line in out.splitlines():
        ref, _, tip = line.partition("\t")
        name = ref.removeprefix("refs/heads/")
        if not ref.startswith(prefix) or name in on_remote:
            continue
        who = claim.holder(root, base, tip)
        when = claim.git(root, "log", "-1", "--format=%ci", tip).stdout.strip()
        ahead = int(claim.git(root, "rev-list", "--count", f"{base}..{tip}").stdout.strip())
        rows.append((name, str(who), when, ahead, trees.get(name, "")))
    return sorted(rows)


def evidence_mark(root: Path, tip: str, pushed: str, age: timedelta, now: datetime) -> str:
    """`releasable` only for an evidence-less tip strictly older than `age`."""
    when = datetime.strptime(pushed, "%Y-%m-%d %H:%M:%S %z")
    if claim.tip_has_lane_evidence(root, tip):
        return "-"
    if now - when > age:
        return "releasable"
    return "-"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--remote", help=f"the shared remote (default: ${claim.REMOTE_SETTING})")
    parser.add_argument("--trunk", help=f"the trunk branch on it (default: ${TRUNK_SETTING})")
    parser.add_argument("--evidence-max-age", default=None,
                        help=f"whole hours before an evidence-less claim is releasable (default: ${AGE_SETTING})")
    parser.add_argument("--root", type=Path, default=claim.ROOT)
    args = parser.parse_args(argv)
    try:
        remote = claim.remote_name(args.remote)
        trunk = trunk_name(args.trunk)
        # The trunk is resolved before the age, so a missing trunk still says
        # so when the age is also unset. The age is resolved before anything
        # is printed: an unset age refuses rather than guessing a mark.
        base, rows, names, tips = _claims_and_names(args.root, remote, trunk)
        age = evidence_max_age(args.evidence_max_age)
        now = datetime.now(UTC)
        local = unpushed(args.root, base, names)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    print(f"claims on {remote} (trunk {trunk} at {base[:12]}): {len(rows)} branch(es)")
    if rows:
        table = [("branch", "holder", "pushed (tip committed)", "evidence", "ahead")]
        table += [
            (b, h, p, evidence_mark(args.root, tips[b], p, age, now), str(n))
            for b, h, p, n in rows
        ]
        widths = [max(len(r[i]) for r in table) for i in range(4)]
        for r in table:
            print("  ".join(c.ljust(w) for c, w in zip(r[:4], widths, strict=True)) + "  " + r[4])
    if local:
        print(f"unpushed local seam branches (not on {remote}; no claim): {len(local)} branch(es)")
        table = [("branch", "holder", "last commit", "ahead", "worktree")]
        table += [(b, h, w, str(n), t or "-") for b, h, w, n, t in local]
        widths = [max(len(r[i]) for r in table) for i in range(3)]
        for r in table:
            print("  ".join(c.ljust(w) for c, w in zip(r[:3], widths, strict=True)) + "  " + r[3] + "  " + r[4])
    return 0


if __name__ == "__main__":
    sys.exit(main())
