"""Merge verified seam branches onto the trunk, without ever risking the shared index.

This replaces the patch-and-replay step of the old landing model (`git diff` in a
worktree -> a `.patch` file -> `git apply -3` on the main tree), whose recorded
failure mode is that **`git apply -3` applies SOME files and stays silent about the
rest**. A merge has real ancestry, so git reports a conflict instead of skipping a
hunk; this script asserts the file count as well, belt and braces.

THE RULE THAT SHAPES THIS SCRIPT (skykeep-07, 2026-09-15): a conflicting `git merge`
writes unmerged entries into the SHARED index, and that locks every concurrent
session out of `git commit -- <paths>` until somebody resolves or aborts. Recovering
with `git merge --abort` is already too late — the damage lands the moment the merge
runs. So a conflict must be detected WITHOUT touching the index:
`git merge-tree --write-tree` does exactly that, writing only to the object database
and reporting a clean merge as exit 0. Every seam is tested that way first, and a
real merge runs only for a seam that has already proven clean.

Each seam becomes its own `--no-ff` merge commit, deliberately, not a squash:
squashing would stage the seam into the shared index and then need a bare commit,
which this repo forbids, and real merge commits let one bad seam inside a batch back
out with `git revert -m 1 <merge sha>` while its batch-mates stay.

A PUSHED seam (ADR-0112 §§ 3 and 7) is named `<remote>/seam/<Id>`. It is fetched
first, into its remote-tracking ref with a forced refspec, and the sha merged is the
one that fetch brought, read back from the fully qualified ref: a stale local
`seam/<Id>` (or a stale tracking ref) never stands in for what the developer pushed.
Before anything merges, the fetched tip must carry its evidence and its base:

- the TIP commit's `Lane:` trailer, `testfast <passed> passed, <failed> failed, exit 0
  at <sha>` (AGENTS.md § Parallel development, rule 4). None, two, a malformed one, a
  non-zero exit, a failed test or no passing test refuses, and so does a sha this clone
  does not hold, or one that is neither on the branch nor the tip's own tree: that is
  evidence about some other history. A red refusal adds one sentence when the trunk
  already changed the check the tip's message names (`fix_landed`): the wording only,
  the tip is refused either way.
- a base on the trunk: the fork point (`git merge-base <tip> <trunk>`) must be on the
  trunk's FIRST-PARENT history. A branch cut from another unlanded seam, or from a
  branch the trunk only ever merged in, forks at a commit the trunk reaches through a
  merge's second parent or not at all; the refusal names that fork point.

A pushed seam whose `Lane:` sha holds exactly the tree its merge makes is covered by
its evidence (a sha whose tree equals the tip's is the tip: W3 records its verified sha
in an empty evidence commit). Any other one — the trunk moved after the lane ran, or
the tip changed after it — merges and is MARKED for the batch's unit lane, which the
integrator runs once per batch anyway. The remote and the trunk come from `--remote` /
`--trunk` or the `SKYKEEP_GIT_REMOTE` / `SKYKEEP_GIT_TRUNK` settings (ADR-0002; the
same ones `scripts/claim.py` and `scripts/claims.py` read); a pushed seam refuses
without both. A LOCAL branch (`seam/<Id>`, the integrator's own seam agents) merges as
it always has, with no evidence asked of it.

BACKPRESSURE: a batch is N merges, ONE unit lane, ONE distributed gate, ONE ledger
commit (AGENTS.md § Work list), and nothing else holds a seat to the second half. So
before anything merges, the merge commits on HEAD's first-parent history above the
newest ledger commit are counted, and more than the configured number refuses, naming
that commit and the count: the batch owes its gate and its ledger before another seam
joins it. A ledger commit is a first-parent commit whose subject starts
`Batch<N> ledger`; the next one resets the count, and a history with none counts every
merge on it. The number comes from `--max-unledgered-merges` or the
`SKYKEEP_INTEGRATOR_MAX_UNLEDGERED_MERGES` setting and has no default (ADR-0002): unset
or unreadable refuses. `--dry-run` reports the same count and refuses nothing, so a
developer sees it too; `scripts/sessionview_web.py` shows it beside the Seams panel.

Usage, from the repo root:

    python3 scripts/seam_merge.py seam/ROW-A seam/ROW-B ...
    python3 scripts/seam_merge.py origin/seam/ROW-C [--remote origin --trunk <trunk>]
    SKYKEEP_AGENT=<stream>/<vendor>-<model> python3 scripts/seam_merge.py ...   # or --agent; the merge's `Agent:` trailer
    python3 scripts/seam_merge.py --delete-branches seam/ROW-A ...
    python3 scripts/seam_merge.py --dry-run seam/ROW-A origin/seam/ROW-C  # checks only
    python3 scripts/seam_merge.py --merged-tree-check seam/ROW-A seam/ROW-B  # cumulative tree, then one command
    python3 scripts/seam_merge.py --max-unledgered-merges <N> seam/ROW-A  # else the setting

Prints each seam's MERGE SHA — pass those to `scripts/seam_land.py` as slot values,
because under the batch model a ledger entry should cite its own merge, not HEAD.
`seam_land.py --push` deletes a pushed seam's remote branch once its row lands and the
remote trunk holds it.

This script merges and nothing else: gates run afterwards, in the main checkout, and
the ledger/MasterToDo edits land separately as ONE pathspec commit for the batch.

`--merged-tree-check` is the exception that still merges nothing. It builds the
cumulative merge of the named seams, in order, with `git merge-tree --write-tree`
and `git commit-tree` (objects only: no worktree, and HEAD, the index and the
working tree stay as they were). A seam that conflicts only with an earlier seam
is REFUSED, naming both. The clean commit is exported as a shared clone
(`git clone --shared --no-checkout`, then `checkout --detach`) into a temp
directory: the clone keeps `.git`, so the command can ask git, and it does
not register a worktree on this checkout. The command in
`SKYKEEP_MERGED_TREE_CMD` runs there (default `scripts/skykeep.sh testfast`
when the setting is unset; a blank value refuses).
The checkout's project environment is reused (`SKYKEEP_TEST_PYTHON` when the
caller did not already name an interpreter). It prints `MERGED TREE GREEN` or
`MERGED TREE RED`, naming the seams, and exits non-zero on red. Absent the flag,
nothing about this path runs, and `--dry-run` prints what it printed before.

Fail-closed: a staged index, a merge already in progress, an unknown ref, a pushed
seam without its evidence or its base, a predicted conflict, a file-count mismatch,
or an uncommitted file that a seam also changes, all refuse. So does a seam that
takes the integrator's four things (ADR-0112 § 4): `MasterToDo.md`, `Unresolved.md`,
a `- **Status:** accepted` line in `docs/adr/*.md` (a new record written `proposed`
is allowed; one written `accepted`, or a flip from `proposed` to `accepted`, is not),
or the active `revision:` line of `tests/benchmark/plans.yaml` (the commented example
in that file's header is not the active line). The diff is against the merge base
with HEAD, so a trunk the seam merged in does not count as the seam's edit. Every
check on every seam runs before the first merge. Uncommitted files NO seam touches
are reported and allowed — see the note in `preflight`.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple


def _lane_trailer():
    """`lane_trailer`, loaded from beside this file whatever the caller's sys.path."""
    spec = importlib.util.spec_from_file_location("lane_trailer", Path(__file__).resolve().parent / "lane_trailer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lane_trailer = _lane_trailer()
#: Aliases: mergeprep and the doctor read these names off this module.
LANE = lane_trailer.LANE
LANE_FORMAT = lane_trailer.LANE_FORMAT

#: How an evidence message names the check that went red: a pytest node id, or the bare
#: name of a `test_*` function or a `check_*` script; `[case]` is a node id's parameter.
#: A path with no `::` after it names a file, not a check, so nothing inside one matches.
NAMED_CHECK = re.compile(
    r"(?<![\w./-])(?:(?P<file>[\w./-]+\.py)::)?(?P<name>(?:test|check)_\w+)(?:\[(?P<case>\w+)\])?"
)


def _seat():
    """`integrator_seat`, loaded from beside this file whatever the caller's sys.path."""
    spec = importlib.util.spec_from_file_location("integrator_seat", Path(__file__).resolve().parent / "integrator_seat.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_seat = _seat()
refuse, git, ok = _seat.refuse, _seat.git, _seat.ok
#: mergeprep and the doctor load this name off the module.
_claims = _seat.claims


class Seam(NamedTuple):
    name: str  #: as the integrator named it, for the report
    label: str  #: the merge commit's subject names this: `Merge <label>`
    tip: str  #: the commit checked and merged, pinned once
    lane: str | None  #: a pushed seam's `Lane:` commit; None for a local branch


def seam_files(ref):
    """Every path a seam changes, against its merge base with HEAD."""
    base = git("merge-base", "HEAD", ref)
    return {f for f in git("diff", "--name-only", base, ref).splitlines() if f}


#: ADR-0112 § 4. These two files are the integrator's whatever the diff says.
_INTEGRATOR_PATHS = ("MasterToDo.md", "Unresolved.md")
#: The plans file's active revision line. A commented `revision:` in its header is not.
_PLANS = "tests/benchmark/plans.yaml"
#: The bare status token `scripts/check_adr_template.py` already requires: nothing else on the line.
_STATUS_ACCEPTED = "- **Status:** accepted"


def _adr_record(path):
    """A record directly under `docs/adr/`, the glob the rule names."""
    parent, _, name = path.rpartition("/")
    return parent == "docs/adr" and name.endswith(".md")


def _accepted_status_line(line):
    return line.strip() == _STATUS_ACCEPTED


def _active_revision_line(line):
    """The live `revision:` key. A `# revision:` comment is the header example."""
    body = line.lstrip(" \t")
    return body.startswith("revision:") and not body.startswith("#")


def _changed_lines(diff):
    """`(path, line without its +/-)` for every added or removed line of a `-U0` diff."""
    path = None
    for raw in diff.splitlines():
        if raw.startswith("diff --git a/"):
            old, sep, new = raw[len("diff --git a/"):].rpartition(" b/")
            path = (old if new == "/dev/null" else new) if sep else None
            continue
        if path is None or not raw or raw[:1] not in "+-":
            continue
        if raw.startswith(("+++", "---")):
            continue
        yield path, raw[1:]


def integrator_only(seam):
    """Paths this seam takes from the integrator, each naming the rule it broke.

    `seam_files` is the merge-base diff, so a trunk merged into the seam does not
    list the trunk's own ledger edits. Status and revision are read from `git diff
    -U0` of that same range: a file may change without the forbidden line changing.
    """
    files = seam_files(seam.tip)
    problems = [
        f"{path}: the integrator alone edits this file"
        for path in _INTEGRATOR_PATHS if path in files
    ]
    watched = [p for p in sorted(files) if _adr_record(p) or p == _PLANS]
    if not watched:
        return problems
    base = git("merge-base", "HEAD", seam.tip)
    diff = git("diff", "-U0", base, seam.tip, "--", *watched)
    seen = set()
    for path, line in _changed_lines(diff):
        if path in seen:
            continue
        if _adr_record(path) and _accepted_status_line(line):
            seen.add(path)
            problems.append(f"{path}: adds or changes a `- **Status:** accepted` line")
        elif path == _PLANS and _active_revision_line(line):
            seen.add(path)
            problems.append(f"{path}: changes the active `revision:` line")
    return problems


def refuse_integrator_files(seams):
    """Refuse before any merge. The message names the seam, the path and the rule."""
    for seam in seams:
        problems = integrator_only(seam)
        if problems:
            refuse(
                f"{seam.name} is the integrator's work (ADR-0112 § 4):\n  "
                + "\n  ".join(problems)
            )


def working_changes():
    """Dirty paths: tracked modifications plus untracked files, no porcelain parsing."""
    tracked = {f for f in git("diff", "--name-only", "HEAD").splitlines() if f}
    untracked = {
        f for f in git("ls-files", "--others", "--exclude-standard").splitlines() if f
    }
    return tracked | untracked


def preflight(allow_dirty, refs):
    """Everything that must be true before we are allowed near the shared index."""
    if os.path.exists(os.path.join(git("rev-parse", "--git-dir"), "MERGE_HEAD")):
        refuse(
            "a merge is already in progress (.git/MERGE_HEAD exists). Resolve or "
            "`git merge --abort` it first — a partial commit is impossible until then."
        )

    staged = [ln for ln in git("diff", "--cached", "--name-only").splitlines() if ln]
    if staged:
        refuse(
            f"{len(staged)} file(s) are STAGED in the shared index; a merge would refuse "
            f"anyway, and resetting them could destroy a peer's in-flight work. Ask "
            f"whoever staged them.\n  " + "\n  ".join(staged[:10])
        )

    # Refuse only on dirty paths a seam actually TOUCHES (skykeep-07, 2026-09-15,
    # from the first live batch). An all-or-nothing dirty check trips on whatever
    # the user happens to be typing — MasterToDo.md, on this tree, constantly — and
    # the only escape is --allow-dirty, which switches the check off for the
    # overlapping paths too. A blunt guard that gets overridden by habit protects
    # less than a narrow guard that almost never fires. Non-overlapping dirt is
    # reported, not refused: git itself still refuses to overwrite a dirty file.
    dirty = working_changes()
    touched = set().union(*(seam_files(r) for r in refs)) if refs else set()
    overlap = sorted(dirty & touched)
    if overlap and not allow_dirty:
        refuse(
            f"{len(overlap)} uncommitted file(s) are also changed by these seams; "
            f"merging would fight over them. Commit or stash them, or pass "
            f"--allow-dirty deliberately.\n  " + "\n  ".join(overlap[:10])
        )
    bystanders = len(dirty) - len(overlap)
    if bystanders:
        print(f"note: {bystanders} unrelated uncommitted file(s) present, untouched by any seam")


def predicts_clean(ref):
    """Conflict check that never touches the index. Returns (ok, tree or detail)."""
    r = subprocess.run(
        ["git", "merge-tree", "--write-tree", "--name-only", "HEAD", ref],
        text=True,
        capture_output=True,
        check=False,
    )
    if r.returncode == 0:
        return True, r.stdout.split("\n", 1)[0].strip()
    # Non-zero means conflicts; output is the tree sha then the conflicted paths.
    lines = [ln for ln in r.stdout.splitlines()[1:] if ln.strip()]
    return False, "\n  ".join(lines[:10] or [(r.stderr or "").strip()])


# ---------------------------------------------------------------- pushed seams


def pushed_form(arg, remotes):
    """(remote, branch) when `arg` names a branch on one of this clone's remotes, else None.

    `origin/seam/X`, `remotes/origin/seam/X` and `refs/remotes/origin/seam/X` all
    name the pushed branch, so none of them can slip past the evidence checks as a
    plain local ref.
    """
    name = arg.removeprefix("refs/").removeprefix("remotes/")
    remote, sep, branch = name.partition("/")
    return (remote, branch) if sep and remote in remotes else None


def fetch_tip(remote, branch):
    """Fetch `branch` into its remote-tracking ref and return the sha that fetch brought.

    Forced, because a developer rebases their own branch (ADR-0112 § 3); read back
    through the fully qualified ref, because the short name `<remote>/seam/X`
    resolves to a LOCAL branch of that name first. No FETCH_HEAD is written: in a
    shared checkout it belongs to whoever fetched last.
    """
    tracking = f"refs/remotes/{remote}/{branch}"
    r = subprocess.run(
        ["git", "fetch", "--quiet", "--no-write-fetch-head", remote, f"+refs/heads/{branch}:{tracking}"],
        text=True, capture_output=True, check=False,
    )
    if r.returncode:
        refuse(f"cannot fetch {branch} from {remote}: is it pushed?\n{(r.stdout + r.stderr).strip()}")
    return git("rev-parse", "--verify", f"{tracking}^{{commit}}")


def named_checks(message, trunk_ref):
    """{a check as `message` names it: its file on the trunk}, for each one the trunk holds.

    A node id names its own file, unless its `[case]` is a `check_*` script: then the
    script is the check and the test around it only runs it. A bare `check_*` name is
    the one tracked file `<name>.py`, a bare `test_*` name the one file that defines
    that function. A name the trunk does not hold, or holds in two places, is left out:
    a red is attributed only to a check this can point at, never to a likely one.
    """
    tracked = git("ls-tree", "-r", "--name-only", trunk_ref, check=False).splitlines()

    def only(paths):
        return paths[0] if len(paths) == 1 else None

    def script(stem):
        return only([p for p in tracked if p.rpartition("/")[2] == f"{stem}.py"])

    def defining(function):
        found = git(
            "grep", "-l", "-E", "-e", rf"^[[:space:]]*(async[[:space:]]+)?def {function}\(",
            trunk_ref, "--", "*.py", check=False,
        )
        return only([line.removeprefix(f"{trunk_ref}:") for line in found.splitlines()])

    checks = {}
    for m in NAMED_CHECK.finditer(message):
        file, named, case = m["file"], m["name"], m["case"]
        if case and case.startswith("check_"):
            named, path = case, script(case)
        elif file:
            named, path = f"{file}::{named}", file if file in tracked else None
        else:
            path = script(named) if named.startswith("check_") else defining(named)
        if path and path not in checks.values():
            checks[named] = path
    return checks


def fix_landed(tip, trunk):
    """What a red refusal adds when the trunk already fixed the red the tip names, else "".

    Derived from the tree, never from a list of known reds: the trunk's first-parent
    history says where each change LANDED (a seam's fix lands as its merge commit, the
    sha the ledger cites), and `git merge-base --is-ancestor` says whether the seam
    holds that landing. The newest landing of a named check that the tip lacks is
    evidence the lane ran before the fix. A check whose newest landing the seam already
    holds adds nothing, and neither does a fix still on an unlanded branch: the red is
    then the seam's own, or its fix is still to wait for. Every question here is asked
    with `check=False`: this only words a refusal already decided, so a git failure
    costs the sentence and never replaces the refusal.
    """
    ref = f"refs/heads/{trunk}"
    if not ok("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
        return ""
    fixes = []
    for named, path in named_checks(git("log", "-1", "--format=%B", tip, check=False), ref).items():
        landing = git("--literal-pathspecs", "rev-list", "-1", "--first-parent", ref, "--", path, check=False)
        if landing and not ok("merge-base", "--is-ancestor", landing, tip):
            fixes.append(f"`{named}`, which {trunk} last changed at {landing[:12]}")
    if not fixes:
        return ""
    return (
        f". The tip's message names {', and '.join(fixes)}, after this seam was cut: "
        f"your evidence predates the fix on {trunk}. Rebase onto {trunk} and re-cite "
        f"(seam-recite § 5, path (a): a landed fix rules out the xfail of path (b))"
    )


def lane_evidence(name, tip, trunk):
    """The commit the tip's `Lane:` trailer says `testfast` passed at; anything less refuses.

    Only the TIP's trailers count: a branch starts with an empty `<Id>: claimed`
    commit and may carry fix-ups after its lane ran, and neither is evidence.
    """
    raw = git("log", "-1", "--format=" + lane_trailer.TRAILER_FORMAT, tip)
    found = lane_trailer.values(raw)
    problem, _, lane = lane_trailer.verdict(found)
    if problem == "missing":
        refuse(
            f"{name}'s tip {tip[:12]} carries no `Lane:` trailer: push it with the "
            f"`testfast` evidence as its last trailer block, `{LANE_FORMAT}`"
        )
    if problem == "several":
        refuse(f"{name}'s tip {tip[:12]} carries {len(found)} `Lane:` trailers; one says which lane counts")
    if problem == "malformed":
        refuse(f"{name}'s `Lane: {found[0]}` is not `{LANE_FORMAT}`")
    if problem == "red":
        refuse(
            f"{name}'s `Lane:` trailer records {lane['failed']} failed, exit {lane['exit']}: "
            f"a branch is pushed only on a green `testfast`{fix_landed(tip, trunk)}"
        )
    if problem == "empty":
        refuse(f"{name}'s `Lane:` trailer records no passing test: a lane that ran nothing proves nothing")
    ran = git("rev-parse", "--verify", "--quiet", f"{lane['sha']}^{{commit}}", check=False)
    if not ran:
        refuse(
            f"{name}'s `Lane:` sha {lane['sha']} is not a commit this clone holds, so the "
            f"evidence cannot be checked. Was the branch rebased after the lane ran?"
        )
    if tree(ran) != tree(tip) and not ok("merge-base", "--is-ancestor", ran, tip):
        refuse(
            f"{name}'s `Lane:` sha {ran[:12]} is neither on the branch nor the tip's tree: "
            f"the evidence is about another history"
        )
    return ran


def based_on_trunk(name, tip, trunk):
    """Refuse a tip whose fork point is not on the trunk's first-parent history."""
    ref = f"refs/heads/{trunk}"
    if not ok("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
        refuse(f"no local branch {trunk!r} to check {name}'s base against (--trunk / SKYKEEP_GIT_TRUNK)")
    fork = git("merge-base", tip, ref, check=False)
    if not fork:
        refuse(f"{name} shares no history with {trunk}: it was not cut from the trunk")
    if fork not in set(git("rev-list", "--first-parent", ref).splitlines()):
        refuse(
            f"{name} forks from {trunk} at {fork[:12]}, which is not on {trunk}'s first-parent "
            f"history: it was cut from another seam or another branch, not from the trunk. "
            f"Rebase it onto {trunk} and push it again."
        )


def tree(commit):
    return git("rev-parse", f"{commit}^{{tree}}")


def resolve(args):
    """Every named seam, fetched and checked, before anything touches the trunk."""
    claims = _claims()
    prefix, row_id = claims.claim.BRANCH_PREFIX, claims.claim.ROW_ID
    remotes = set(git("remote").splitlines())
    named = [(a, pushed_form(a, remotes)) for a in args.branches]
    remote = trunk = None
    if any(form for _, form in named):
        try:
            remote = claims.claim.remote_name(args.remote)
            trunk = claims.trunk_name(args.trunk)
        except claims.Refused as e:
            refuse(f"{e}; a pushed seam is checked against both")

    seams = []
    for arg, form in named:
        if form is None:
            tip = git("rev-parse", "--verify", "--quiet", f"{arg}^{{commit}}", check=False)
            if not tip:
                refuse(f"no such ref: {arg}")
            seams.append(Seam(arg, arg, tip, None))
            continue
        on, branch = form
        if on != remote:
            refuse(f"{arg} is on remote {on!r}, but the configured remote is {remote!r} (--remote / SKYKEEP_GIT_REMOTE)")
        if not (branch.startswith(prefix) and row_id.fullmatch(branch.removeprefix(prefix))):
            refuse(f"{arg}: a pushed branch is merged only as <remote>/{prefix}<Id>")
        name = f"{remote}/{branch}"
        tip = fetch_tip(remote, branch)
        lane = lane_evidence(name, tip, trunk)
        based_on_trunk(name, tip, trunk)
        seams.append(Seam(name, branch, tip, lane))
    return seams


# ---------------------------------------------------------------- ledger backpressure

#: A batch's ledger commit, known by the start of its subject. Read on the
#: first-parent history only: the same words in a commit body, mid-subject or on a
#: merged seam's own commits are not the trunk's ledger.
LEDGER_SUBJECT = re.compile(r"(?:Batch[0-9]+|Tooling batch) ledger\b")
LEDGER_FORM = "Batch<N> ledger"
MAX_UNLEDGERED_FLAG = "--max-unledgered-merges"
MAX_UNLEDGERED_SETTING = "SKYKEEP_INTEGRATOR_MAX_UNLEDGERED_MERGES"
#: The command `--merged-tree-check` runs in the exported cumulative checkout.
#: Unset means the in-process half; a blank value is not that default (ADR-0002).
MERGED_TREE_SETTING = "SKYKEEP_MERGED_TREE_CMD"
MERGED_TREE_DEFAULT = "scripts/skykeep.sh testfast"
_TREE_OID = re.compile(r"[0-9a-f]{40,64}")
#: The `git` arguments whose output `ledger_debt` reads: HEAD's first-parent history,
#: newest first, one `<sha> NUL <parents> NUL <subject>` line per commit. NUL because git
#: allows it in no message, and because it is not whitespace: a strip cannot eat it.
SPINE_LOG = ("log", "--first-parent", "--format=%H%x00%P%x00%s", "HEAD")


class LedgerDebt(NamedTuple):
    merges: int  #: merge commits on the first-parent history above the ledger commit
    ledger: str | None  #: the newest ledger commit's sha; None when the history holds none
    batch: str  #: that commit's `Batch<N> ledger` words; "" when there is none


def ledger_debt(spine):
    """The merges above the newest ledger commit, from `git SPINE_LOG` output.

    Every merge commit on the first-parent history counts, whoever made it: each
    is work the trunk took that no gate and no ledger has answered for. A line
    that is not a commit raises ValueError rather than be skipped: a count taken
    from a history only partly read would be a guess.
    """
    merges = 0
    for line in spine.split("\n"):
        if not line:
            continue
        fields = line.split("\x00", 2)
        if len(fields) != 3 or not re.fullmatch(r"[0-9a-f]{40,64}", fields[0]):
            raise ValueError(f"not a first-parent history line: {line[:80]!a}")
        sha, parents, subject = fields
        found = LEDGER_SUBJECT.match(subject)
        if found:
            return LedgerDebt(merges, sha, found.group(0))
        if len(parents.split()) > 1:
            merges += 1
    return LedgerDebt(merges, None, "")


def debt_text(debt):
    """The count as the refusal, the dry run and sessionview all word it.

    Plain words only: sessionview prints this in a frame its page embeds, and a
    character HTML escapes would make the page's copy differ from the frame.
    """
    if debt.ledger is None:
        return f"{debt.merges} merge(s) on a history with no ledger commit"
    return f"{debt.merges} merge(s) above the last ledger commit {debt.ledger[:12]} ({debt.batch})"


def merge_limit(arg):
    """(limit, "") from the flag or the setting; (None, why) when neither gives a count.

    No default (ADR-0002): how many merges may wait on one gate is the
    integrator's to set, and a number supplied here would be a constant. A flag
    that is given wins, blank included, so a blank flag is never made good by
    the setting. Only ASCII digits are a count; zero allows no merge above the
    ledger.
    """
    raw = arg if arg is not None else os.environ.get(MAX_UNLEDGERED_SETTING)
    if raw is None or not raw.strip():
        return None, (
            f"no limit: pass {MAX_UNLEDGERED_FLAG} N or set {MAX_UNLEDGERED_SETTING} "
            f"(ADR-0002: how many merges may sit above the last ledger commit is "
            f"configuration, never a literal)"
        )
    text = raw.strip()
    if not re.fullmatch(r"[0-9]{1,9}", text):
        return None, (
            f"{raw!a} is not a count of merges: {MAX_UNLEDGERED_FLAG} / "
            f"{MAX_UNLEDGERED_SETTING} takes a whole number, zero or more, in at most nine digits"
        )
    return int(text), ""


def backpressure(args):
    """Refuse a merge onto a trunk that owes its gate and its ledger; a dry run only reports.

    Read before anything is written: the count is of the trunk as it stands, so a
    refusal leaves the index, the manifest and the trunk untouched.
    """
    try:
        debt = ledger_debt(git(*SPINE_LOG))
    except ValueError as exc:
        refuse(f"cannot count the merges above the last ledger commit: {exc}")
    said = debt_text(debt)
    if debt.ledger is None:
        said += f" (no first-parent subject starts `{LEDGER_FORM}`, so every merge on it counts)"
    limit, why = merge_limit(args.max_unledgered_merges)
    if limit is None:
        if not args.dry_run:
            refuse(f"{why}. Nothing was merged. The trunk carries {said}.")
        print(f"ledger: {said}; {why}: a real merge would be REFUSED")
        return
    over = debt.merges > limit
    allowed = f"more than the {limit} allowed ({MAX_UNLEDGERED_FLAG} / {MAX_UNLEDGERED_SETTING})"
    if args.dry_run:
        if over:
            print(
                f"ledger: {said}; {allowed}: a real merge would be REFUSED until the "
                f"batch is gated and its ledger committed"
            )
        else:
            print(f"ledger: {said}; limit {limit}")
        return
    if over:
        refuse(
            f"{said}: {allowed}. Nothing was merged, and the shared index is untouched.\n"
            f"  The batch owes its gate and its ledger before another seam joins it: run the "
            f"unit + adversarial lane, then the gate batch (scripts/gate_distribute.py), then "
            f"ONE pathspec ledger commit via scripts/seam_land.py whose subject starts "
            f"`{LEDGER_FORM}`. That commit resets this count."
        )


def _manifest_prepare(args, seams):
    """Refuse a merge the live manifest does not name, before the trunk moves."""
    batch = _seat.supplied_batch(args.batch)
    remote = _seat.configured_remote(args.remote)
    if batch is not None and remote is None:
        refuse(
            "the batch id is written on the integrator manifest, and that needs "
            "the remote (--remote / SKYKEEP_GIT_REMOTE)"
        )
    if remote is None:
        return
    if batch is not None:
        for seam in seams:
            try:
                _seat.manifest().row_of(seam.label)
            except _seat.manifest().ManifestError as exc:
                refuse(str(exc))
    try:
        _seat.manifest().prepare(remote, batch, root=Path("."))
    except _seat.manifest().ManifestError as exc:
        refuse(str(exc))


def _record_manifest(args, merged):
    batch = _seat.supplied_batch(args.batch)
    remote = _seat.configured_remote(args.remote)
    if batch is None or remote is None or not merged:
        return
    try:
        _seat.manifest().record_merged(
            remote, batch,
            [{"branch": seam.label, "merge_sha": full} for seam, _sha, full in merged],
            root=Path("."),
        )
    except _seat.manifest().ManifestError as exc:
        refuse(str(exc))


def _merged_tree_command():
    """The command to run in the exported checkout. Unset selects the default; blank refuses."""
    raw = os.environ.get(MERGED_TREE_SETTING)
    if raw is None:
        return MERGED_TREE_DEFAULT
    if raw.strip() == "" or raw != raw.strip():
        refuse(
            f"{MERGED_TREE_SETTING} is empty or padded; the merged-tree command is "
            f"configuration (ADR-0002). Leave it unset for the default "
            f"`{MERGED_TREE_DEFAULT}`, or set it to the command to run."
        )
    return raw


def _project_env():
    """The caller's environment, plus this checkout's interpreter when they named none.

    `scripts/skykeep.sh` treats `SKYKEEP_TEST_PYTHON` as the project environment
    and does not `uv sync` a fresh one into the temp tree. `UV_PROJECT_ENVIRONMENT`
    is left untouched: pointing it at a shared venv can rewrite that venv's
    editable install.
    """
    env = dict(os.environ)
    if env.get("SKYKEEP_TEST_PYTHON") or env.get("UV_PROJECT_ENVIRONMENT"):
        return env
    python = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"
    if python.is_file() and os.access(python, os.X_OK):
        env["SKYKEEP_TEST_PYTHON"] = str(python)
    return env


def _git_steering():
    """`git_steering.py`, loaded from beside this file whatever the caller's sys.path."""
    spec = importlib.util.spec_from_file_location(
        "git_steering", Path(__file__).resolve().parent / "git_steering.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_STEERING = _git_steering()
#: The variables that point git at a repository: `git_steering.STEERING`, the
#: one list every scrubbing script shares, under the name this script gave it.
_REPO_STEERING_VARS = _STEERING.STEERING


def _scrubbed_env():
    """`_project_env` without the variables that point git at a repository.

    One environment for the export's clone, checkout and fetch and for the
    command run in the clone: a leaked GIT_DIR or GIT_INDEX_FILE would make any
    of them act on the source checkout (its HEAD detached, its index rewritten).
    """
    return _STEERING.scrubbed(_project_env())


def _conflicted_paths(stdout):
    """Filenames `git merge-tree --name-only` listed. A leading tree oid is not one."""
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if lines and _TREE_OID.fullmatch(lines[0]):
        lines = lines[1:]
    return lines


def _commit_tree(tree, parents, message):
    """A throwaway commit in the object database. `--no-gpg-sign`: it is never published."""
    argv = ["git", "commit-tree", "--no-gpg-sign", tree]
    for parent in parents:
        argv.extend(["-p", parent])
    made = subprocess.run(argv, input=message, text=True, capture_output=True, check=False)
    sha = made.stdout.strip()
    if made.returncode != 0 or not _TREE_OID.fullmatch(sha):
        refuse(
            f"git commit-tree -> {made.returncode}\n{(made.stdout + made.stderr).strip()}"
        )
    return sha


def _export_commit(commit, dest):
    """Shared clone of this checkout, detached at `commit`.

    `git archive` leaves no `.git`, so a command that runs `git rev-parse` or
    `git ls-files` cannot succeed there, and the default `testfast` never
    reads green. A shared clone keeps the history without copying objects and
    without registering a worktree on the source. `dest` is the empty
    directory the caller deletes afterwards.

    A clone makes no local branch but HEAD's, and `check_reserved_numbers.py`
    reads the trunk by its branch name. So the source's local branches are
    fetched into the clone under their own names, after the detach: no branch
    is checked out there, so none is refused.
    """
    env = _scrubbed_env()
    toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], text=True, capture_output=True,
        env=env, check=False,
    )
    source = toplevel.stdout.strip()
    if toplevel.returncode != 0 or not source:
        refuse(f"git rev-parse --show-toplevel -> {toplevel.returncode}\n{toplevel.stderr.strip()}")
    cloned = subprocess.run(
        ["git", "clone", "--quiet", "--shared", "--no-checkout", source, str(dest)],
        capture_output=True, env=env, check=False,
    )
    if cloned.returncode != 0:
        detail = cloned.stderr.decode("utf-8", "replace").strip()
        refuse(f"git clone --shared -> {cloned.returncode}\n{detail}")
    checked = subprocess.run(
        ["git", "-C", str(dest), "checkout", "--quiet", "--detach", commit],
        capture_output=True, env=env, check=False,
    )
    if checked.returncode != 0:
        detail = checked.stderr.decode("utf-8", "replace").strip()
        refuse(f"git checkout --detach {commit} -> {checked.returncode}\n{detail}")
    fetched = subprocess.run(
        ["git", "-C", str(dest), "fetch", "--quiet", "--no-tags", source,
         "+refs/heads/*:refs/heads/*"],
        capture_output=True, env=env, check=False,
    )
    if fetched.returncode != 0:
        detail = fetched.stderr.decode("utf-8", "replace").strip()
        refuse(f"git fetch of the local branches -> {fetched.returncode}\n{detail}")


def merged_tree_check(seams):
    """Cumulative merge, then one command. Exit 0 on green, 1 on red; a conflict REFUSES.

    Each seam is already known to merge clean onto the trunk alone. A conflict
    here is with an earlier seam, and the refusal names both.
    """
    current = git("rev-parse", "HEAD")
    built = []
    for seam in seams:
        merged = subprocess.run(
            ["git", "merge-tree", "--write-tree", "--name-only", current, seam.tip],
            text=True, capture_output=True, check=False,
        )
        if merged.returncode not in (0, 1):
            detail = (merged.stdout + merged.stderr).strip()
            refuse(f"git merge-tree {seam.name} -> {merged.returncode}\n{detail}")
        if merged.returncode != 0:
            paths = _conflicted_paths(merged.stdout)
            partners = [
                earlier.name for earlier in built
                if set(paths) & seam_files(earlier.tip)
            ]
            if not partners:
                partners = [earlier.name for earlier in built]
            if not partners:
                refuse(
                    f"{seam.name} CONFLICTS with the current trunk — nothing was staged, "
                    f"the shared index is untouched.\n  conflicted:\n  "
                    + "\n  ".join(paths[:10] or ["(merge-tree named no path)"])
                )
            named = ", ".join(partners)
            refuse(
                f"{seam.name} conflicts with {named} once both are merged; "
                f"each merges clean onto the trunk alone.\n  conflicted:\n  "
                + "\n  ".join(paths[:10] or ["(merge-tree named no path)"])
            )
        tree = merged.stdout.split("\n", 1)[0].strip()
        if not _TREE_OID.fullmatch(tree):
            refuse(f"git merge-tree {seam.name} returned no tree oid:\n{merged.stdout.strip()}")
        current = _commit_tree(
            tree, [current, seam.tip], f"merged-tree check: {seam.label}\n",
        )
        built.append(seam)

    command = _merged_tree_command()
    argv = shlex.split(command)
    if not argv:
        refuse(f"{MERGED_TREE_SETTING} produced no command")
    names = ", ".join(seam.name for seam in seams)
    with tempfile.TemporaryDirectory(prefix="skykeep-merged-tree-") as dest:
        _export_commit(current, Path(dest))
        proc = subprocess.run(argv, cwd=dest, env=_scrubbed_env(), check=False)
    if proc.returncode == 0:
        print(f"MERGED TREE GREEN: {names}")
        return 0
    print(f"MERGED TREE RED: {names}")
    return 1


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("branches", nargs="*", metavar="branch")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="check every seam; merge nothing")
    parser.add_argument(
        "--merged-tree-check", action="store_true",
        help="build the cumulative merge in a temp tree and run "
             f"${MERGED_TREE_SETTING} (default: {MERGED_TREE_DEFAULT}); merge nothing",
    )
    parser.add_argument("--delete-branches", action="store_true", help="delete merged LOCAL branches")
    parser.add_argument("--remote", help="the shared remote a pushed seam is on (default: $SKYKEEP_GIT_REMOTE)")
    parser.add_argument("--agent", help="who signs the merge commits (default: $SKYKEEP_AGENT)")
    parser.add_argument("--trunk", help="the trunk a pushed seam must fork from (default: $SKYKEEP_GIT_TRUNK)")
    parser.add_argument(
        "--batch", default=None,
        help="the batch id this merge belongs to (else $SKYKEEP_INTEGRATOR_BATCH); never invented",
    )
    parser.add_argument(
        MAX_UNLEDGERED_FLAG, default=None, metavar="N",
        help=f"refuse when more than N merges already sit above the last `{LEDGER_FORM}` commit "
             f"(else ${MAX_UNLEDGERED_SETTING}); no default",
    )
    args = parser.parse_intermixed_args(argv)
    if not args.branches:
        refuse("no seam branch named. Usage: seam_merge.py [flags] <branch> ...")
    # The cumulative check writes nothing, same as a dry run: no lease, no
    # manifest, and backpressure reports instead of refusing.
    if args.merged_tree_check:
        args.dry_run = True

    claims = _claims()
    try:
        who = claims.claim.agent_identity(args.agent)
    except claims.Refused as exc:
        refuse(f"{exc}; nothing was merged")

    seams = resolve(args)
    # A dry run writes nothing. Developers run it on their own seams
    # (seam-recite), so the integrator's lease does not apply to it.
    if not args.dry_run:
        _seat.fence(args.remote)
    backpressure(args)
    if not args.dry_run:
        _manifest_prepare(args, seams)
    preflight(args.allow_dirty, [s.tip for s in seams])
    refuse_integrator_files(seams)
    start = git("rev-parse", "--short", "HEAD")
    print(f"{'checking' if args.dry_run else 'merging'} {len(seams)} seam(s) onto {start}\n")

    merged, marked = [], []
    for s in seams:
        clean, detail = predicts_clean(s.tip)
        if not clean:
            done = f" {len(merged)} seam(s) already merged and left in place." if merged else ""
            refuse(
                f"{s.name} CONFLICTS with the current trunk — nothing was staged, the shared "
                f"index is untouched.{done}\n  Rebase the seam onto the tip in its own "
                f"worktree and re-run.\n  conflicted:\n  {detail}"
            )
        if args.dry_run:
            note = evidence_note(s, detail, marked)
            print(f"  {s.name}: merges clean{note}")
            continue

        base = git("merge-base", "HEAD", s.tip)
        expected = {f for f in git("diff", "--name-only", base, s.tip).splitlines() if f}
        before = git("rev-parse", "HEAD")
        git("merge", "--no-ff", "-q", "-m", claims.claim.with_agent_trailer(f"Merge {s.label}", who), s.tip)
        full = git("rev-parse", "HEAD")
        sha = git("rev-parse", "--short", "HEAD")
        landed = {f for f in git("diff", "--name-only", before, "HEAD").splitlines() if f}

        missing = expected - landed
        if missing:
            refuse(
                f"{s.name} changed {len(expected)} file(s) but the merge brought {len(landed)}; "
                f"{len(missing)} missing — the silent-partial class. Back out with "
                f"`git revert -m 1 {sha}`.\n  " + "\n  ".join(sorted(missing)[:10])
            )
        merged.append((s, sha, full))
        note = evidence_note(s, tree("HEAD"), marked)
        print(f"  {s.name}: {len(expected)} file(s) -> merge {sha}{note}")

    if marked:
        print(
            f"\nMARKED for the batch's unit lane — {len(marked)} pushed seam(s) whose `Lane:` "
            f"evidence ran on another tree than the merge makes:\n  " + "\n  ".join(marked)
        )
    if args.dry_run:
        if args.merged_tree_check:
            code = merged_tree_check(seams)
            print("\nNothing was changed.")
            return code
        print("\nAll seams merge clean. Nothing was changed.")
        return 0

    _record_manifest(args, merged)
    print(f"\nMERGED {len(merged)} seam(s); trunk {start} -> {git('rev-parse', '--short', 'HEAD')}")
    for s, sha, _full in merged:
        print(f"  {sha}  {s.name}")
    print(
        "\nNEXT: run the unit + adversarial lane, then the gate batch "
        "(scripts/gate_distribute.py), then ONE pathspec ledger commit via "
        "scripts/seam_land.py citing each merge sha above; with --push it pushes the "
        "trunk, then deletes each landed seam's remote branch.\n"
        "A seam that goes red backs out alone: git revert -m 1 <its merge sha>"
    )

    local = [s for s, *_ in merged if s.lane is None]
    if args.delete_branches:
        for s in local:
            r = subprocess.run(["git", "branch", "-d", s.name], text=True, capture_output=True, check=False)
            print(f"  delete {s.name}: {(r.stdout + r.stderr).strip()}")
        if len(local) < len(merged):
            print("  pushed seams keep their remote branches until seam_land.py lands their rows")
    elif local:
        print(f"\nSeam branches kept. Delete after the batch lands: "
              f"git branch -d {' '.join(s.name for s in local)}")
    return 0


def evidence_note(seam, merged_tree, marked):
    """How far a pushed seam's `Lane:` evidence reaches; a miss joins `marked`."""
    if seam.lane is None:
        return ""
    if tree(seam.lane) == merged_tree:
        return f" (Lane: evidence at {seam.lane[:12]} is this tree)"
    marked.append(seam.name)
    return f" (Lane: evidence at {seam.lane[:12]} is another tree: MARKED for the batch's unit lane)"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
