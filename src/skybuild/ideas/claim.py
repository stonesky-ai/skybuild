#!/usr/bin/env python3
"""Claim a row by pushing `seam/<Id>` to the shared remote (PAR-CLAIM, ADR-0112 § 2).

The branch on the shared remote is the claim: no shared file two branches could
conflict on, and anyone can list who holds what (`scripts/claims.py`). A claim:

1. reads the row's brief, `todo/<Id>.md`, AT THE TRUNK SHA NAMED (`--at`), never
   in the working tree: a brief gone from that sha is a row that landed, and a
   brief only in someone's working tree is a row nobody filed. No brief refuses.
2. refuses while any row named on the brief's `depends on:` line is still open.
   Every row-id-shaped token on that line is a dependency, and it is closed when
   its brief is gone from the sha named (the tree decides, never an annotation
   like "(landed)"), or the sha named is the pushed tip of that row's own claimed
   `seam/<dep>` branch or a descendant of it (a stacked claim). A trailing
   parenthetical is prose, never a dependency. Work cut on an unlanded dependency rests on a base the
   batch gate may still reject. A brief with no `depends on:` line refuses: an
   unknown dependency set is not an empty one.
3. refuses when `seam/<Id>` already exists on the remote, and names its holder.
4. refuses a stream's seventh claim whose tip carries no `Lane:` evidence
   (CLAIM-WIP-CAP-AND-EXPIRY; `EVIDENCE_LESS_CAP`, six since
   CLAIM-EVIDENCE-LESS-CAP-6). The stream is the agent's first segment, or
   `owner/<session>` for an owner session, so two sessions the owner drives
   do not share a cap. Evidence is exactly one `Lane:` trailer on the tip,
   in the same shape `scripts/seam_merge.py` accepts; a trailer on an older
   commit does not count, and neither does a trailer that is not that shape.
   The refusal names every evidence-less claim that stream already holds.
5. builds ONE empty commit, `<Id>: claimed`, whose parent is the sha named and
   whose message ends with the `Agent:` trailer, using `git commit-tree`, and
   pushes that sha to `refs/heads/seam/<Id>` with a lease that the branch must
   not exist, so a claim racing another is refused by the remote itself.

Every commit is authored by the same configured user (AGENTS.md § Parallel
development), so `%an` names nobody: the holder is the `Agent:` trailer, which
is why `--agent` is required. The claim never touches the index, HEAD, the
working tree or a local branch, because the owner's checkout is shared by live
sessions; take the branch locally afterwards with the command it prints.

The remote comes from `--remote` or the `SKYKEEP_GIT_REMOTE` setting, never a
literal (ADR-0002); neither set refuses.

    python3 scripts/claim.py <Id> --at <trunk sha> --agent <stream>/<vendor>-<model> \\
        [--remote <name>] [--root <repo>]

Exit: 0 claimed, 3 refused (nothing pushed).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

#: The repository this file lives in (ADR-0002: derived, never hardcoded).
ROOT = Path(__file__).resolve().parent.parent
BRIEFS = "todo"
BRANCH_PREFIX = "seam/"
REMOTE_SETTING = "SKYKEEP_GIT_REMOTE"
AGENT_SETTING = "SKYKEEP_AGENT"

#: A row id as the ledger writes them: upper-case segments joined by hyphens
#: (`PAR-CLAIM`, `BS-P4-11`, `ASK-PTODO-P5-5-A`). On a `depends on:` line this
#: shape is what separates an id from prose, dates (`2026-09-24`) and file
#: names (`synth-basics.pst`).
ROW_ID = re.compile(r"(?<![\w.-])[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+(?![\w-])")
#: `<stream>/<vendor>-<model>`, e.g. `W1/claude-opus-5.5`, `W3/codex-gpt-5`, or,
#: for an owner-driven session with no stream file, `owner/<session>/<vendor>-<model>`
#: (AGENTS.md § Parallel development), e.g. `owner/skykeep-0e/claude-opus-5`. The
#: session segment may hold hyphens. `owner/<vendor>-<model>` is refused: with no
#: session it identifies nobody, which is what the three-segment form exists to fix.
AGENT = re.compile(
    r"(?:owner/[A-Za-z0-9][\w.-]*|(?!owner/)[A-Za-z0-9][\w.]*)"
    r"/[A-Za-z0-9][\w.]*-[A-Za-z0-9][\w.-]*"
)
#: The identity a script-written commit signs with. It is `AGENT`: the trunk's pattern
#: now takes the three-segment owner form, and the identifies-nobody cases in
#: `tests/adversarial/test_claim.py` hold for it, so a second looser pattern would
#: admit `W1/claude` and `owner/claude-opus-5`, which name no model and no session.
SIGNER = AGENT
DEPENDS = re.compile(r"depends on:(?P<rest>.*)")
#: A trailing parenthetical on a `depends on:` line is an annotation, not a
#: dependency: `none (coordinate with WF-X)` names no row.
TRAILING_PAREN = re.compile(r"\s*\([^()]*\)\s*$")
#: A URL carrying `user:password@` (the standing lint's pattern): a credential
#: never rides in a URL, so a remote spelled that way is refused, not used.
CREDENTIALED_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s'\"@:]+:[^/\s'\"@]+@")
def _lane_trailer():
    """`lane_trailer`, loaded from beside this file whatever the caller's sys.path."""
    spec = importlib.util.spec_from_file_location("lane_trailer", Path(__file__).resolve().parent / "lane_trailer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lane_trailer = _lane_trailer()
#: CLAIM-WIP-CAP-AND-EXPIRY. A stream may hold this many claims whose tips carry
#: no Lane evidence; the next is refused. The count is the row's rule, not a
#: setting. The owner raised it from two to six on 2026-10-04
#: (CLAIM-EVIDENCE-LESS-CAP-6) so a stream can hold more `Partial:` seams while
#: their testfast evidence catches up. The age at which such a claim becomes
#: releasable is the setting, and it has no default (ADR-0002); this number is
#: not that setting.
EVIDENCE_LESS_CAP = 6


class Refused(Exception):
    """The claim or listing cannot go ahead. Nothing was pushed."""


def git(root: Path, *argv: str, stdin: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(root), *argv], input=stdin, capture_output=True,
                       text=True, check=False)
    if check and r.returncode:
        raise Refused(f"git {' '.join(argv)} -> {r.returncode}\n{(r.stdout + r.stderr).strip()}")
    return r


def remote_name(arg: str | None) -> str:
    """The remote from the argument, else the setting; neither refuses."""
    remote = (arg or os.environ.get(REMOTE_SETTING) or "").strip()
    if not remote:
        raise Refused(
            f"no remote: pass --remote <name> or set {REMOTE_SETTING}"
            " (ADR-0002: the remote is configuration, never a literal)"
        )
    if CREDENTIALED_URL.search(remote):
        raise Refused("the remote carries a credential in its URL; name a configured remote instead")
    return remote


def agent_identity(arg: str | None) -> str:
    """Who signs a commit a script writes: the argument, else the setting; neither refuses."""
    who = (arg or os.environ.get(AGENT_SETTING) or "").strip()
    if not who:
        raise Refused(
            f"no agent identity: pass --agent <stream>/<vendor>-<model> or set {AGENT_SETTING}"
            " (ADR-0002: the identity is configuration, never a literal). An unnamed commit is not written."
        )
    if not SIGNER.fullmatch(who):
        raise Refused(f"agent identity {who!r} is not `<stream>/<vendor>-<model>` or `owner/<session>/<vendor>-<model>`")
    return who


def with_agent_trailer(message: str, who: str) -> str:
    """`message` ending in an `Agent: who` trailer: joined to a closing trailer paragraph, else its own."""
    body = message.rstrip()
    paragraphs = body.split("\n\n")
    last = paragraphs[-1].splitlines()
    if any(line.startswith("Agent:") for line in last):
        return body + "\n"
    trailers = len(paragraphs) > 1 and all(re.match(r"[A-Za-z][\w-]*: \S", line) for line in last)
    return body + ("\n" if trailers else "\n\n") + f"Agent: {who}\n"


def resolve_commit(root: Path, ref: str) -> str:
    r = git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    if r.returncode or not r.stdout.strip():
        raise Refused(f"{ref!r} is not a commit in this clone; fetch the trunk first")
    return r.stdout.strip()


def brief_at(root: Path, sha: str, row_id: str) -> str | None:
    """`todo/<Id>.md` as it stands at `sha`, or None when the sha holds no such brief."""
    r = git(root, "show", f"{sha}:{BRIEFS}/{row_id}.md", check=False)
    return r.stdout if r.returncode == 0 else None


def _without_trailing_parens(line: str) -> str:
    while (stripped := TRAILING_PAREN.sub("", line)) != line:
        line = stripped
    return line


def dependencies(brief: str, row_id: str) -> list[str]:
    """Every row id on the brief's `depends on:` line(s), in order, itself excluded."""
    lines = [m["rest"] for m in map(DEPENDS.match, brief.splitlines()) if m]
    if not lines:
        raise Refused(
            f"{BRIEFS}/{row_id}.md has no `depends on:` line; an unknown dependency set"
            " is not an empty one (write `depends on: —` when there is none)"
        )
    ids = [t for line in lines for t in ROW_ID.findall(_without_trailing_parens(line)) if t != row_id]
    return list(dict.fromkeys(ids))


def remote_tip(root: Path, remote: str, ref: str) -> str | None:
    """The sha `ref` has on the remote, or None when the remote has no such ref."""
    out = git(root, "ls-remote", remote, ref).stdout
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if name == ref:
            return sha
    return None


def fetch(root: Path, remote: str, *refs: str) -> None:
    """Bring the refs' objects into this clone and write no ref and no FETCH_HEAD.

    FETCH_HEAD belongs to whoever last fetched in a shared checkout; the
    objects are all a reader needs.
    """
    if refs:
        git(root, "fetch", "--quiet", "--no-write-fetch-head", remote, *refs)


class Holder(NamedTuple):
    name: str
    #: False when no commit on the branch carries an `Agent:` trailer and the
    #: name is the commit author, which in this repository names nobody.
    from_trailer: bool

    def __str__(self) -> str:
        return self.name if self.from_trailer else f"{self.name} (author; no Agent: trailer)"


def stream_of(agent: str) -> str | None:
    """The stream an `Agent:` trailer belongs to, or None when it names nobody.

    `W4/claude-opus-5.5` and `W4/codex-gpt-5` are one stream. An owner session
    is `owner/<session>/<vendor>-<model>`, and the session is the stream:
    two sessions the owner drives do not share a cap.
    """
    if not AGENT.fullmatch(agent):
        return None
    parts = agent.split("/")
    if parts[0] == "owner":
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def tip_has_lane_evidence(root: Path, tip: str) -> bool:
    """True when the tip carries exactly one `Lane:` trailer of the lane shape.

    An older commit's trailer is not evidence: `seam_merge` reads only the tip.
    Two trailers, or one that is not the shape, are not evidence either.
    """
    raw = git(root, "log", "-1", "--format=" + lane_trailer.TRAILER_FORMAT, tip).stdout
    found = lane_trailer.values(raw)
    # The WIP cap counts any well-formed trailer, red or empty included.
    return len(found) == 1 and lane_trailer.LANE.fullmatch(found[0]) is not None


def evidence_less_held(root: Path, remote: str, base: str, stream: str) -> list[str]:
    """`seam/*` branches on `remote` that `stream` holds with no tip Lane evidence.

    Sorted by branch name. A branch whose holder is an author, or another
    stream, is not in the list. Fetching writes no ref and no FETCH_HEAD.
    """
    prefix = f"refs/heads/{BRANCH_PREFIX}"
    out = git(root, "ls-remote", remote, f"{prefix}*").stdout
    tips: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith(prefix):
            tips[ref] = sha
    if not tips:
        return []
    fetch(root, remote, *sorted(tips))
    held: list[str] = []
    for ref, tip in sorted(tips.items()):
        who = holder(root, base, tip)
        if not who.from_trailer or stream_of(who.name) != stream:
            continue
        if tip_has_lane_evidence(root, tip):
            continue
        held.append(ref.removeprefix("refs/heads/"))
    return held


def holder(root: Path, base: str, tip: str) -> Holder:
    """The newest `Agent:` trailer among the branch's own commits (`base..tip`).

    Falls back to the tip's author, marked as such, when none carries one: a
    branch pushed by hand before this tool, say, or one sitting on the trunk.
    """
    r = git(root, "log", "--format=%(trailers:key=Agent,valueonly=true,separator=%x1f)%x1e",
            f"{base}..{tip}", check=False)
    if r.returncode == 0:
        for record in r.stdout.split("\x1e"):
            values = [v.strip() for v in record.split("\x1f") if v.strip()]
            if values:
                return Holder(values[-1], True)
    author = git(root, "log", "-1", "--format=%an", tip, check=False).stdout.strip()
    return Holder(author or "unknown", False)


def claim(root: Path, row_id: str, at: str, agent: str, remote: str) -> str:
    """Push the claim commit; its sha, or Refused."""
    if not ROW_ID.fullmatch(row_id):
        raise Refused(f"{row_id!r} is not a row id (upper-case segments joined by hyphens)")
    if not AGENT.fullmatch(agent):
        raise Refused(f"--agent {agent!r} is not `<stream>/<vendor>-<model>` (e.g. W1/claude-opus-5.5) "
                      "or, for an owner session with no stream, `owner/<session>/<vendor>-<model>` "
                      "(e.g. owner/skykeep-0e/claude-opus-5)")
    sha = resolve_commit(root, at)

    brief = brief_at(root, sha, row_id)
    if brief is None:
        raise Refused(
            f"{BRIEFS}/{row_id}.md does not exist at {sha[:12]}: the row was never filed"
            " there, or it has landed"
        )
    open_deps = [d for d in dependencies(brief, row_id)
                 if brief_at(root, sha, d) is not None and not stacked_on(root, remote, sha, d)]
    if open_deps:
        raise Refused(
            f"{row_id} depends on open row(s) {', '.join(open_deps)}: each still has a brief"
            f" at {sha[:12]}. Claim it once they land."
        )

    ref = f"refs/heads/{BRANCH_PREFIX}{row_id}"
    existing = remote_tip(root, remote, ref)
    if existing:
        raise Refused(already_claimed(root, remote, ref, existing, sha))

    stream = stream_of(agent)
    if stream is None:
        raise Refused(f"agent identity {agent!r} names no stream")
    held = evidence_less_held(root, remote, sha, stream)
    if len(held) >= EVIDENCE_LESS_CAP:
        raise Refused(
            f"{stream} already holds {len(held)} evidence-less claims with no "
            f"Lane: evidence ({', '.join(held)}); the cap is {EVIDENCE_LESS_CAP} per "
            "stream, so another is refused. Nothing was pushed."
        )

    tree = git(root, "rev-parse", f"{sha}^{{tree}}").stdout.strip()
    message = f"{row_id}: claimed\n\nAgent: {agent}\n"
    commit = git(root, "commit-tree", tree, "-p", sha, stdin=message).stdout.strip()
    # The lease with an empty expected value means "the branch must not exist",
    # and the remote checks it atomically: of two racing claims, one is refused.
    push = git(root, "push", "--porcelain", f"--force-with-lease={ref}:", remote,
               f"{commit}:{ref}", check=False)
    if push.returncode:
        existing = remote_tip(root, remote, ref)
        if existing:
            raise Refused(already_claimed(root, remote, ref, existing, sha))
        raise Refused(f"the push of {ref} failed:\n{(push.stdout + push.stderr).strip()}")
    return commit


def stacked_on(root: Path, remote: str, sha: str, dep: str) -> bool:
    """True when `sha` sits on the claimed tip of `seam/<dep>` (a stacked claim).

    The dependency's brief is still present at `sha` because its row is in
    flight, not landed. That is legitimate only when the row is claimed on the
    remote and its pushed tip is `sha` or an ancestor of it: the base then
    carries the dependency's work. An unclaimed dependency, or a base that does
    not contain the claimed tip, is still an open dependency.
    """
    ref = f"refs/heads/{BRANCH_PREFIX}{dep}"
    tip = remote_tip(root, remote, ref)
    if not tip:
        return False
    try:
        fetch(root, remote, ref)
    except Refused:
        return False
    return git(root, "merge-base", "--is-ancestor", tip, sha, check=False).returncode == 0


def already_claimed(root: Path, remote: str, ref: str, tip: str, base: str) -> str:
    branch = ref.removeprefix("refs/heads/")
    try:
        fetch(root, remote, ref)
        who = str(holder(root, base, tip))
    except Refused:
        who = f"unknown (its tip {tip[:12]} could not be fetched)"
    return f"{branch} already exists on {remote}: the row is held by {who}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("row_id", metavar="Id")
    parser.add_argument("--at", required=True, help="the trunk sha the integrator names")
    parser.add_argument("--agent", required=True, help="<stream>/<vendor>-<model> or owner/<session>/<vendor>-<model>, the holder")
    parser.add_argument("--remote", help=f"the shared remote (default: ${REMOTE_SETTING})")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        remote = remote_name(args.remote)
        commit = claim(args.root, args.row_id, args.at, args.agent, remote)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    branch = f"{BRANCH_PREFIX}{args.row_id}"
    print(f"claim: {branch} pushed to {remote} at {commit[:12]} as {args.agent}")
    print(f"next: git switch -c {branch} {commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
