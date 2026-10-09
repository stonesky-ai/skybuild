#!/usr/bin/env python3
"""Run the whole gate lane across N disposable stacks, and report it ONCE.

`scripts/gate_distribute.py` decides which suite goes where; `scripts/gate_lane.sh`
is one lane; this is the thing an operator actually runs:

    scripts/skykeep.sh testgates              # every gate suite, lanes chosen for you
    scripts/gate_run.py --lanes 2 --first-lane 35 tests/gates/test_gate_hl.py ...
    scripts/gate_run.py --dry-run             # print the lane commands, run nothing

WHY IT EXISTS. The batched landing model (ADR-0090) gates a whole batch of seams
once, over several lanes at a time. Until now that meant a human reading
`gate_distribute.py`'s output, pasting N shell commands into N terminals,
remembering the systemd scope that keeps a lane from OOM-killing the editor, and
then reading N logs and forming one verdict by eye. Every step of that has gone
wrong at least once in this repo's history, and the two that cost the most were
the two a script does not get wrong:

  * **a timeout read as a pass.** `timeout(1)` exits 124 and pytest prints
    nothing conclusive, so a suite that never finished looks, in a scrollback,
    like a suite with no failures. It is neither passed nor failed: the criterion
    is UNMEASURED, and this reports it as its own category and exits non-zero.
  * **a lane that died before its suites ran.** A suite with no `SUITE` line at
    all is not absent from the verdict — it is UNMEASURED too, and named.

And a third, measured on batch24's gate (2026-09-30): **a suite that answered
with no verdict, counted as a failure.** Gate RT exited rc=4 before pytest
collected a case and Gate HW reported `1 passed, 21 errors`; the summary called
both failed and said `0 UNMEASURED`, so "110 passed of 116" read as 116 suites
measured. The fourth is the reverse: a collection error, a broken conftest or a
product failure inside a fixture called UNMEASURED, which sends triage to the
environment for the tree's own red. So UNMEASURED needs the environment's
signature, and a suite that only skipped is SKIPPED, not a verdict
(`suite_outcome`).

AN OOM EVICTION IS RE-MEASURED, ON EVIDENCE ONLY. A lane runs in a scope with
`MemoryMax` and `MemorySwapMax=0`, so a lane that loses a memory race is not
throttled: the OOM killer takes its pytest and the suite reads rc=137. Every
137 is UNMEASURED (`suite_outcome`: a kill is no verdict on any case), but an
UNMEASURED that is never measured again is a criterion nobody tested, landing
as a red run on whichever batch it happened in. Not every 137 is an eviction,
though: anything that sends signal 9 produces one. So the lane, while its
scope still exists, heads a 137 line with `oom-kill=<killer>` from evidence
(`oom_evidence`): its own scope's `memory.events` (`oom_kill` counts a victim
whichever killer chose it, `oom` only when the scope reached its own limit)
and the kernel journal's `oom-kill:` line naming the scope as the victim's.
A `cgroup` or `host` eviction, on this box or in another box's evidence, is
re-run alone on this box, once, in a pass of its own (the re-run pass in
`_run`): this box's own once its lanes and its alone phase are done — while
the other boxes are still on their slices, in split mode — and another box's
once its evidence is read, after every other phase. Bounded, so a suite that
genuinely leaks cannot spin. An `unproven` kill
stays UNMEASURED and is never re-run: something sent signal 9, and a green
re-run must not excuse it.

A RED THE WAVE MADE IS RE-RUN ALONE BEFORE IT IS BELIEVED. Batch23 (two lanes)
and batch24 (three) each ended with six non-clean suites of which four passed
when the integrator re-ran them alone by hand — and the four moved between
runs, so they said something about the packed wave, not about the seams being
gated. That manual pass cost the same time every batch, could be forgotten,
and stood between a flake and a good seam backed out for it. So the re-run
pass does it, once per suite, for every suite whose verdict a re-run can cure
(`rerun_reason`, read on top of `suite_outcome`, which alone decides the
label): one that FAILED, and one UNMEASURED because the lane posture guard
refused it before collection (rc=4), because its cases only errored on the
environment's signature, or because the OOM killer took it on the lane's
evidence. Never a timeout (a
re-run is another full cap), an unproven kill (above), any other status pytest
ends no run with, or a suite no lane reported. The summary then has four
counts rather than three: passed; FAILED, red in the wave and red again alone;
UNMEASURED; and DID NOT REPRODUCE, red in the wave and green alone, which names
the suite and its wave lane and is not counted as a failure. A suite that was
UNMEASURED in the wave and green alone is simply passed: nothing was red to
reproduce. A wave red whose re-run measured nothing stays FAILED: the only
measurement there is said red. The counts add up to the suites scheduled, and
a run whose counts do not is never green. Past `--rerun-limit` the wave itself
is broken (an endpoint gone, a seam that breaks the data layer), re-running
each red alone would turn a gate of minutes into one of hours, and every wave
verdict stands.

FAIL CLOSED BEFORE ANYTHING STARTS (ADR-0002). The endpoint, the credential
file, the three model roles and the lane state directory are all required and
all read from the environment; nothing here has a default that reaches a
network, a model or a key, and the credential is presented as a bearer header
(`SKYKEEP_MODEL_AUTH=bearer`), never in a URL. The refusal happens before the
first container starts, and it happens on `--dry-run` too: a dry run whose
configuration would refuse a real run is a dry run that tells you nothing.

TWO AXES OF PARALLELISM, AND THE SCHEDULING RULES BETWEEN THEM. A lane is
a whole deployment; `-n` is several pytest workers inside ONE lane against ONE
deployment. Across the box the lanes are independent, but inside a lane the
workers share a vault, and three kinds of suite cannot take that; a fourth
cannot share the ENDPOINT, and a fifth cannot share the HOST'S BRIDGE with a
browser:

  * **serial** — `@pytest.mark.serial`. The suite asserts a vault-wide fact or
    reshapes the vault, so it runs WHOLE on its lane and never under `-n`. The
    set is read out of the markers in the tree, not written down here, and
    `tests/gates/conftest.py` refuses a second time from inside pytest.
  * **exclusive** — `gate_distribute.NEEDS_THE_BOX_ALONE`. A suite named there
    gets a phase with no other lane RUNNING, not merely none mid-suite (the
    alone phase, below). The set is empty today, and then the phase is skipped
    and no lane is brought down: Gate OB, its one member, drove the real
    `skykeep.sh start/stop/reinstall` verbs against deployments whose ports
    lanes 33 and 34 held, and those ports now sit clear of every lane decade.
  * **tail** — `gate_distribute.POISONS_ITS_STACK`. Gate RS restores the vault;
    Gate DX has the lane install the real demonstration in front of it, which
    wipes the vault. Each runs at the tail of its lane, RS last of all.
  * **vision** — `gate_distribute.NEEDS_THE_VISION_MODEL`. Gate HW reads real
    handwriting with the vision model, and the endpoint keeps one generating
    model in memory. It gets a wave with no other lane mid-suite (the vision
    wave, below), and no other pass names the vision model at all.
  * **clamd** — `discover_clamd_suites`, `tests/gates/clamd_lane/`. Gate CL
    dials the REAL malware engine, and every standard lane holds clamd at zero
    replicas. It gets a wave of its own on the first lane, prepared with
    `SKYKEEP_CLAMD_REPLICAS=1` and put back afterwards (the clamd wave, below).
    Every full run carries it and no setting removes it.
  * **restarts** — `gate_distribute.RESTARTS_CONTAINERS`. Gate SITE, AD, JB,
    DX, OB, RD and WW stop, start, restart or create containers, or have their
    lane do it in front of them. Each such change moves a port on the host's
    docker bridge, and every Chromium on the box drops its in-flight requests
    with `net::ERR_NETWORK_CHANGED`. They get a wave with no browser suite in
    it (the restart wave, below).

A BARRIER BEFORE THE FIRST SUITE. Every lane's stack is brought up and healthy
before any suite anywhere starts, and no lane is torn down while another is
running. Measured 2026-09-23: browser suites on a lane that was already up saw
`net::ERR_NETWORK_CHANGED` and locator timeouts while a NEIGHBOURING lane was
still creating containers — docker moves host bridges when it does that, and
Chromium drops in-flight requests. Those reds said nothing about the product.

THE BARRIER IS ALSO THE ONLY PLACE A LANE IS RESET. A running lane is reused,
and on 2026-09-25 that reuse was the red: lane 32 had ended its previous run
with Gate RS, so its vault still held the restore marker, its recreated
webserver refused to serve and crash-looped 26 times under `unless-stopped`,
and every restart moved the docker bridge under the OTHER lanes' browser
suites; lane 31's two-day-old vault read Gate 0's `hr` as a duplicate and
SG2's stored days as stale. So `gate_lane.sh`'s barrier pass reads each
running vault first (`vault_probe_sql`, judged by `vault_verdict`) and brings
a lane whose vault holds the restore marker or the demonstration installation,
or is older than `SKYKEEP_LANE_MAX_VAULT_AGE_HOURS`, down with its volumes —
its own project only, name read back first. A suite pass never resets: a
stack coming down while another lane runs browser suites moves the bridge
exactly as one coming up does. The exclusive phase gets a barrier pass of its
own for the same reason, because its lane may have just finished RS or DX.

THE RESTART WAVE WAITS FOR EVERY LANE TOO. The barrier keeps stacks from coming
up beside a browser suite; it did nothing about a suite that restarts a
container mid-wave. In batch19 (2026-09-29) lane 31's Gate SITE ran `docker
restart` on its `site` container in the second lane 32's first QT browser case
signed in, and the page read "Could not reach the vault" — the eighth such red
(GATE-SIGNIN-UNREACHABLE-INTERMITTENT), every one with a container another
lane's suite stopped, started or created inside it. So those suites are in no
lane's list of the suite wave. Once every lane has finished, they run as a wave
of their own, packed over the lanes (none of them drives a browser), each lane
behind a barrier pass of its own under its own label. It runs BEFORE the vision
wave and the alone phase, and the alone phase brings down every lane either
wave brought up. No retry and no longer timeout was added to sign-in for this:
the red was the schedule's.

THE VISION WAVE WAITS FOR EVERY LANE. On batch24's gate (2026-09-30, three
lanes) Gate HW returned `1 passed, 21 errors in 302.79s`, every error a
ReadTimeout from the vision model, and Gate RT returned rc=4, the posture guard
refusing it because the runtime did not answer for the vision model its lane
named. Alone, HW passed 22 of 22 in 164s and RT 9 of 9 in 50s. Nothing was
wrong with either suite, the model or the timeout: lanes on two models were
evicting each other on an endpoint that holds one. So the vision suites are in
no lane's list. Once every lane's suite pass has exited, the first lane gets a
barrier pass of its own (it may have just run Gate RS or Gate DX at its tail)
and then the vision suites, one pytest after another, under a label of their
own so the suite wave's log is left for the packer. It needs the endpoint and
not the box: no lane is brought down and `LANE_ALONE` is not set, because a
lane that refused beside every idle stack would trade one UNMEASURED for
another. It runs BEFORE the alone phase, which does bring lanes down —
unless, in split mode, the alone phase has already run beside the other
boxes' slices (below). What
this run cannot see is a lane another run or another box started on the same
endpoint — that stays the announced window § Lane traps describes.

THE ALONE PHASE EMPTIES THE BOX FIRST. On 2026-09-23 Gate OB ran after every
lane had finished its suites, but with lanes 32-34 still UP: its first
`skykeep.sh start` refused naming skykeep-p33 and skykeep-p34, whose ports its
own deployment needs, and the suite read red for the schedule. (Its other
fourteen reds were nginx 502s from lane 31's webserver, recreated in front of
the restore marker Gate RS had just left — which the alone phase's own barrier
pass now resets.) So once every lane has finished, this run brings its OTHER
lanes down with their volumes (`LANE_TEARDOWN`), and the alone phase's lane
refuses to start while any other gate lane is still running (`LANE_ALONE`) —
a lane this run did not start included, which it names and leaves alone. The
suite is then UNMEASURED, never red. What can ever be brought down is exactly
`skykeep-p<N>` for a lane number this run brought up, other than the alone
phase's own, each name read back from compose first. It runs by default in a
full run: a gate that runs only when asked is a smaller gate nobody decided to
shrink, and leaving it out stays a matter of naming the suites.

IN SPLIT MODE THE ALONE PHASE DOES NOT WAIT FOR THE OTHER BOXES. Alone means
alone on THIS box: the other boxes are other hosts, with their own docker and
their own ports, and nothing they run can hold a port Gate OB needs. On
Batch42's gate (2026-10-02, three boxes) this box's lanes finished at 1569 s,
the slowest slice about 430 s later, and only then did Gate OB start — 330 s,
serial, on a box that had sat idle for six minutes. So once every lane of this
box has finished, the alone phase runs at once, with the same teardown and the
same barrier, while the other boxes are still on their slices; the wait for
their evidence comes after it. The vision wave still waits for every box: it
loads the vision model over the think model they are mid-suite on. Gate OB is
no vision suite, so its lane names the models the other boxes' lanes name and
never the vision model, and swaps nothing on the endpoint. Under an endpoint ceiling the phase overlaps
only when this box's share holds an endpoint lane, which its finished lanes
have just released; with none, it waits for every box as before.

HOW MANY LANES. Each lane is a whole deployment plus a host-side pytest, and
this box has OOM-killed its editor mid-wave before. With no `--lanes`, the
count comes from MemAvailable — `(available - 4 GiB reserve) / memory-per-lane`,
clamped to 1..6 — and every lane is launched inside its own
`systemd-run --user --scope` with `MemoryMax` set and swap off, under
`choom -n 900` so the kernel picks a lane rather than the editor. A machine that
cannot afford one lane gets one lane and a warning, not a refusal: the answer to
"not enough memory for four" is to run it slowly, not to skip the gates.

`memory-per-lane` defaults to `MEASURED_LANE_MEMORY`, and that name is meant
literally: it is what a lane was measured to hold, plus stated slack, and the
comment on the constant says what the measurement covered and what it did not.
ONE FLAG DOES TWO JOBS, and they are not the same job. As the DIVISOR it stands
for the whole lane — the scope and the containers. As `MemoryMax` it caps the
scope only: the host-side pytest and its browser. The lane's containers run
under docker's own cgroups, outside the scope, and nothing here caps them; they
are roughly half of a lane. So the cap does not bound a lane, and the lane
count is the only thing that keeps N lanes inside the machine. Every lane log
ends with a `LANE-MEMORY` line (`gate_lane.sh`) carrying what that lane held,
so the figure can be checked against the last run instead of re-derived.
"""

from __future__ import annotations

import argparse
import getpass
import glob
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent


#: The lane's pytest basetemps live under `<tmp>/<this><user>`, a SIBLING of
#: pytest's own `<tmp>/pytest-of-<user>`. Inside pytest's root, every other
#: pytest session of the same user that ends runs pytest's numbered-directory
#: cleanup over it: a lane directory's non-numeric suffix parses as -1, it has
#: no `.lock` (pytest writes none under --basetemp), and it is removed while the
#: lane still writes into it — measured on wonko 2026-10-01, the same 10 of
#: `tests/bdd`'s cases failing with FileNotFoundError while a peer's unit lane
#: ran. The name must therefore never start with `pytest-of-`. `gate_lane.sh`
#: asks `--lane-pytest-root` for the path, so the lane and this preflight's
#: prune cannot name two different roots.
LANE_PYTEST_ROOT_PREFIX = "skykeep-lane-pytest-"


def lane_pytest_root(base: Path | None = None) -> Path:
    """Where a gate lane makes its pytest basetemps; `base` defaults to the temp dir."""
    base = Path(tempfile.gettempdir()) if base is None else Path(base)
    return base / f"{LANE_PYTEST_ROOT_PREFIX}{getpass.getuser()}"


def pytest_temp_roots(base: Path | None = None) -> tuple[Path, ...]:
    """Roots the preflight prunes: the lane's own, then pytest's, which still
    holds directories that lanes made before their basetemp moved out of it."""
    base = Path(tempfile.gettempdir()) if base is None else Path(base)
    return lane_pytest_root(base), base / f"pytest-of-{getpass.getuser()}"


def _basetemp_args(argv: list[str]) -> list[str]:
    """The `--basetemp` values on one command line, in either spelling."""
    found = []
    for i, arg in enumerate(argv):
        if arg.startswith("--basetemp="):
            found.append(arg.split("=", 1)[1])
        elif arg == "--basetemp" and i + 1 < len(argv):
            found.append(argv[i + 1])
    return found


def _live_pytest_paths(proc_root: Path = Path("/proc")) -> set[str]:
    """Process cwd, open files and `--basetemp` arguments; a stale lock alone
    cannot prove abandonment. The argument matters most: a lane's pytest
    between two tests holds no file open in its basetemp and has its cwd in the
    repository, and pytest writes no `.lock` under `--basetemp`, so its command
    line is the only standing evidence that the directory is in use."""
    paths: set[str] = set()
    for process in proc_root.iterdir():
        if not process.name.isdecimal():
            continue
        for link in (process / "cwd", *(process / "fd").glob("*")):
            try:
                target = os.readlink(link)
            except OSError:
                continue  # a process can exit while its descriptors are read
            if target.startswith("/"):
                paths.add(target.removesuffix(" (deleted)"))
        try:
            argv = (process / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            continue
        for value in _basetemp_args(argv):
            if value.startswith("/"):
                paths.add(os.path.normpath(value))
    return paths


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _tree_bytes(path: Path) -> int:
    total = 0
    for parent, dirs, files in os.walk(path, followlinks=False):
        for name in (*dirs, *files):
            try:
                total += (Path(parent) / name).lstat().st_size
            except FileNotFoundError:
                pass
    return total


def reclaim_pytest_temp_dirs(
    root: Path, *, min_age_seconds: int, now: float | None = None
) -> tuple[int, int]:
    """Remove only old, abandoned pytest-N directories belonging to this user."""
    if root.is_symlink() or not root.is_dir():
        return 0, 0
    now = time.time() if now is None else now
    live_paths = _live_pytest_paths()
    removed = freed = 0
    for path in root.iterdir():
        if not re.fullmatch(r"pytest-[A-Za-z0-9]+", path.name) or path.is_symlink() or not path.is_dir():
            continue
        try:
            if now - path.stat().st_mtime < min_age_seconds:
                continue
            prefix = str(path) + os.sep
            if any(p == str(path) or p.startswith(prefix) for p in live_paths):
                continue
            lock = path / ".lock"
            if lock.is_symlink():
                continue
            if lock.exists():
                try:
                    pid = int(lock.read_text().strip())
                except (OSError, ValueError):
                    continue  # unknown lock state is held, never guessed stale
                if _pid_alive(pid):
                    continue
            size = _tree_bytes(path)
            shutil.rmtree(path)
        except OSError:
            continue  # a concurrent run or unreadable tree stays untouched
        removed += 1
        freed += size
    return removed, freed


def _pytest_tmp_age_seconds(env: dict[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    raw = env.get("SKYKEEP_PYTEST_TMP_MIN_AGE_SECONDS", "18000")
    if not raw.isdecimal() or int(raw) < 1:
        raise Refused("SKYKEEP_PYTEST_TMP_MIN_AGE_SECONDS must be a positive whole number")
    return int(raw)
LANE_SCRIPT = ROOT / "scripts" / "gate_lane.sh"
LOG_DIR = ROOT / "test-logs"

#: `timeout(1)`'s exit code, and the whole reason this script reports three
#: outcomes rather than two.
TIMEOUT_RC = 124

#: SIGKILL's exit status (128 + 9). The OOM killer sends it — the lane's own
#: scope's when it reaches `MemoryMax`, the host's when the box runs out — and
#: so does anything else that sends signal 9. Every 137 is UNMEASURED
#: (`suite_outcome`); only the lane's evidence makes one an eviction to re-run.
SIGKILL_RC = 137

#: pytest's `ExitCode.USAGE_ERROR`. The lane posture guard (`tests/conftest.py`,
#: ADR-0048) exits with it from `pytest_sessionstart`, before collection, so a
#: suite that reads it ran no test at all: UNMEASURED, and re-run alone once.
#: A `tests/conftest.py` that does not import exits with it too, and that is
#: the tree, not the environment: only the suite's own output tells them apart
#: (`TREE_CONFTEST_BROKEN`).
USAGE_ERROR_RC = 4

#: The lane posture guard's other exit (`COULD_NOT_ASK_EXIT_CODE` in
#: `tests/conftest.py`): the model runtime gave no answer at all — its name
#: did not resolve, nothing listened, it timed out — so the guard could not
#: ask, where rc 4 is "asked and failed". No test ran and nothing was learned
#: about the deployment: UNMEASURED, never a failure, and re-run alone once.
#: Batch34's first gate lost 13 suites to a resolver outage of ten minutes.
COULD_NOT_ASK_RC = 75

#: How many suites the re-run pass re-runs at most. Batch23 and batch24 each
#: left six non-clean; past this many the wave is broken rather than flaky,
#: and re-running each alone would cost hours, not minutes.
DEFAULT_RERUN_LIMIT = 8

#: The lane's verdict on a SIGKILLed suite. `gate_lane.sh` writes it FIRST in
#: the summary, ahead of the pytest tail, so it is read only at the head: a
#: suite's own output further along the line can never forge one.
OOM_TOKEN = re.compile(r"oom-kill=(cgroup|host|unproven)\b")

#: The kernel's own files, read by a lane about ITSELF: which cgroup it runs
#: in, and that cgroup's memory events. Kernel interfaces, like /proc/meminfo
#: below, not configuration.
PROC_SELF_CGROUP = Path("/proc/self/cgroup")
CGROUP_ROOT = Path("/sys/fs/cgroup")

#: How long a lane waits on the kernel journal before calling a kill unproven.
JOURNAL_TIMEOUT_SECONDS = 30

#: The lane-number window, matching `gate_lane.sh` and `gate_distribute.py`:
#: lane N publishes on 88<N-30>x, so 31..39 is what the port plan allows.
LANE_FIRST = 31
LANE_LAST = 39

#: Held back from every lane so the machine still has room for the editor, the
#: shell and the page cache. Measured the hard way: the 2026-09-14 wave took the
#: last of memory and the OOM killer took VS Code, orphaning 18 seams.
HOST_RESERVE_BYTES = 4 * 1024**3

MAX_AUTO_LANES = 6

#: What ONE lane holds, measured, plus slack: the lane count's divisor and the
#: default for `--memory-per-lane`. It replaces `3G`, which was the scope's cap
#: and had never been measured against anything.
#:
#: MEASURED 2026-10-01 on a 32 GiB box with no GPU: remote model endpoint, no
#: clamd container (CLAMD-LANE-ZERO), two lanes side by side, one pytest
#: process per lane, 14 minutes of suites. Each lane's own `LANE-MEMORY` line,
#: with a 3-second sampler running beside it:
#:
#:   lane 31 (CV2, ND, PA-browser, DC): scope peak 542 MiB + containers' peaks
#:     summed 601 MiB = 1143 MiB. Highest same-instant total sampled: 951 MiB.
#:   lane 32 (FO, MD-formats, Gate 0):  scope peak 549 MiB + containers' peaks
#:     summed 508 MiB = 1057 MiB. Highest same-instant total sampled: 922 MiB.
#:
#: The seven containers together held 411-428 MiB at their highest sampled
#: moment (the webserver alone peaked at 236-319 MiB), so the two halves of a
#: lane are roughly equal.
#:
#: SLACK: 2048 - 1143 = 905 MiB, 79 % over the highest lane figure recorded and
#: 2.15x the highest same-instant total. It is that wide because the run was
#: NOT a complete gate run, and the figure has to stand for one. Covered: four
#: browser suites (FO, ND, PA-browser, DC), two ingest-heavy suites (CV2, the
#: demonstration corpus; MD-formats, every accepted format), and Gate 0. NOT
#: covered, and what the slack is for: the other 96 gate suites and
#: `tests/bdd`, among them the longest browser suite (Gate DK); Gate DX's
#: demonstration stage; Gate JB's second webserver; Gate RS and Gate OB; a
#: vault grown over a full packed lane (about 75 minutes on that box); and
#: `-n` workers inside a lane, which multiply the scope half. Not covered and
#: NOT in the slack: a lane with a local model or a clamd container, which
#: holds a gigabyte or more on top — pass `--memory-per-lane`. The memory
#: `compose up --build` takes is docker's, outside both halves, and is no part
#: of this figure.
#:
#: The first complete gate run after this lands writes the complete figure into
#: its lane logs. Read `lane_mib` there before trusting or tightening this.
MEASURED_LANE_MEMORY = "2G"

#: What ONE extra pytest-xdist worker holds on top of the lane that runs it, and
#: the default for `SKYKEEP_GATE_WORKER_MEMORY` (same size syntax as
#: `--memory-per-lane`). MEASURED on wonko (Precision 5550, no GPU) at Batch35:
#: about 0.7 GiB per worker, against about 1.8 GiB for a warm lane that runs
#: one pytest process. It is a per-WORKER figure and is never the lane figure:
#: `wave_capacity` divides by it only what is left after the lanes themselves.
MEASURED_WORKER_MEMORY = "0.7G"
WORKER_MEMORY_SETTING = "SKYKEEP_GATE_WORKER_MEMORY"

SUITE_LINE = re.compile(r"^SUITE (\S+) rc=(\d+)(.*)$")

#: The tree a lane measured, written once after its last suite:
#: `TREE skykeep-p31 start=1a2b3c4/0 collect=1a2b3c4/0`, each side `<HEAD>/<dirt>`.
TREE_LINE = re.compile(r"^TREE \S+ start=(\S+) collect=(\S+)\s*$")

#: The one line the lane's vault probe prints: is the restore marker raised
#: (`t`/`f`), how many of the demonstration accounts exist, and the vault's age
#: in seconds (`-1` when its migration ledger is empty, so no age is known).
VAULT_FACTS = re.compile(r"^([tf])\|([0-9]+)\|(-?[0-9]+)$")

#: The lane setting that bounds how old a reused vault may be. Its default is
#: `gate_lane.sh`'s, stated in that script's header with the others; the runner
#: only refuses a value the lane would refuse, so a dry run says so first.
MAX_VAULT_AGE_SETTING = "SKYKEEP_LANE_MAX_VAULT_AGE_HOURS"

#: What a failed `compose up` prints when the thing that failed was a FETCH:
#: an index, a mirror or a registry that did not answer, or answered with
#: nothing. `gate_lane.sh` tries such a bring-up again and no other, so every
#: line here must be one a wrong tree cannot produce. The first is the
#: 2026-10-01 shape: pip told the index offers NO version of a package at all —
#: a wrong pin lists the versions that do exist, and is not this. The BuildKit
#: deadline is the 2026-10-05 shape (Batch59, aragog lane 39): the daemon gave
#: up on a build an I/O-loaded box was still pulling for; only that exact
#: `Internal:` form, never a `failed to solve:` that names a process exit code.
FETCH_FAILURE = re.compile(
    r"\(from versions: none\)"
    r"|failed to solve: Internal: context deadline exceeded"
    r"|ReadTimeoutError|ConnectTimeoutError|NewConnectionError|IncompleteRead"
    r"|Read timed out|Max retries exceeded with url"
    r"|Temporary failure (in name resolution|resolving)|Could not resolve host"
    r"|Connection (reset by peer|timed out)|Network is unreachable"
    r"|Failed to (fetch|download) |Hash Sum mismatch|error sending request for url"
    r"|TLS handshake timeout|i/o timeout|net/http: request canceled"
    r"|failed to resolve source metadata|toomanyrequests"
    r"|HTTP error (429|5[0-9][0-9])"
    r"|\b(429 Too Many Requests|502 Bad Gateway|503 Service Unavailable|504 Gateway Time-?out)\b",
    re.IGNORECASE,
)

#: pip announcing that it is ABOUT to fetch again. The fetch may yet succeed,
#: and a build that then fails on its own account must not read as the index.
FETCH_WILL_BE_RETRIED = re.compile(r"\bWARNING\b.*\bRetrying\b", re.IGNORECASE)

#: How much of the line that showed a fetch failure is repeated to a person.
FETCH_EVIDENCE_CHARS = 200

#: The line `gate_lane.sh`'s `compose_up` leaves in its log when a bring-up is
#: refused: what kind of failure the last attempt was, which attempt that was,
#: how many the lane was allowed, and compose's exit status.
BRINGUP_LINE = re.compile(
    r"^BRINGUP REFUSED kind=(fetch|build|unjudged) attempts=([0-9]+) of=([0-9]+) rc=([0-9]+)$"
)

_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


class Refused(Exception):
    """Configuration is missing or wrong. Nothing has been started."""


class ComposeProject(NamedTuple):
    name: str
    working_dir: Path
    memory_bytes: int
    unit: str
    unit_state: str


def _output(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode:
        raise Refused(f"cannot inspect host state with {argv[0]}: {result.stderr.strip()}")
    return result.stdout


def owner_unit(name: str, runner: Callable[[list[str]], str] = _output) -> tuple[str, Path | None]:
    """Return user service state and its configured checkout, never infer ownership."""
    values = {}
    for line in runner(["systemctl", "--user", "show", name, "-p", "ActiveState", "-p", "WorkingDirectory"]).splitlines():
        key, _, value = line.partition("=")
        values[key] = value
    directory = values.get("WorkingDirectory", "")
    return values.get("ActiveState", "unknown"), Path(directory) if directory else None


def _owner_for(working_dir: Path, runner: Callable[[list[str]], str] = _output) -> tuple[str, str]:
    units = runner(["systemctl", "--user", "list-units", "--all", "--type=service", "--no-legend", "--plain"])
    for line in units.splitlines():
        name = line.split(maxsplit=1)[0] if line.split() else ""
        if not name.endswith(".service"):
            continue
        try:
            state, directory = owner_unit(name, runner)
        except Refused:
            continue
        if directory == working_dir:
            return name, state
    return "unknown", "unknown"


def compose_projects(runner: Callable[[list[str]], str] = _output) -> list[ComposeProject]:
    """Read every running Compose project and its checkout, memory and loop state."""
    ids = runner(["docker", "ps", "--filter", "label=com.docker.compose.project", "--format", "{{.ID}}"]).split()
    if not ids:
        return []
    containers = json.loads(runner(["docker", "inspect", *ids]))
    stats = {}
    for line in runner(["docker", "stats", "--no-stream", "--format", "{{.ID}}|{{.MemUsage}}"]).splitlines():
        identifier, _, usage = line.partition("|")
        try:
            stats[identifier] = parse_memory(usage.partition("/")[0].strip(), "docker memory")
        except Refused:
            raise Refused(f"cannot read memory for container {identifier}") from None
    grouped: dict[str, dict] = {}
    for container in containers:
        labels = container.get("Config", {}).get("Labels") or {}
        name = labels.get("com.docker.compose.project", "")
        directory = labels.get("com.docker.compose.project.working_dir", "")
        if not name or not directory:
            raise Refused("running Compose container lacks project or working_dir label")
        identifier = container["Id"]
        matching = [size for key, size in stats.items() if identifier.startswith(key) or key.startswith(identifier)]
        if len(matching) != 1:
            raise Refused(f"cannot attribute running container memory: {identifier[:12]}")
        entry = grouped.setdefault(name, {"working_dir": Path(directory), "memory_bytes": 0})
        if entry["working_dir"] != Path(directory):
            raise Refused(f"Compose project {name} spans multiple checkout directories")
        entry["memory_bytes"] += matching[0]
    projects = []
    for name, entry in sorted(grouped.items()):
        unit, state = _owner_for(entry["working_dir"], runner)
        projects.append(ComposeProject(name, entry["working_dir"], entry["memory_bytes"], unit, state))
    return projects


def memory_blockers(runner: Callable[[list[str]], str] = _output) -> str:
    projects = compose_projects(runner)
    if not projects:
        return "no running Compose projects found"
    return "; ".join(
        f"{p.name} working_dir={p.working_dir} memory={p.memory_bytes / 1024**2:.0f} MiB "
        f"owner_unit={p.unit} active={p.unit_state}"
        for p in projects
    )


def checkout_processes(checkout: Path) -> list[tuple[int, str, str]]:
    """Any process in this checkout, including pytest and gate runners."""
    found = []
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            cwd = Path(os.readlink(process / "cwd"))
            argv = (process / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except (OSError, PermissionError):
            continue
        if cwd == checkout or checkout in cwd.parents or str(checkout) in argv:
            found.append((int(process.name), argv, str(cwd)))
    return found


def remove_compose_project(project: ComposeProject) -> None:
    if not re.fullmatch(r"skykeep-p[0-9]+", project.name):
        raise Refused(f"{project.name} is not a disposable lane project")
    compose = project.working_dir / "docker-compose.yml"
    if not compose.is_file():
        raise Refused(f"{compose} is missing; cannot identify the deployment")
    command = ["docker", "compose", "-p", project.name, "-f", str(compose)]
    configured = json.loads(_output([*command, "config", "--format", "json"]))
    if configured.get("name") != project.name:
        raise Refused(f"Compose config names {configured.get('name')}, not {project.name}")
    _output([*command, "down"])


def reclaim_idle_project(name: str, unit: str, idle_seconds: int, *, poll_seconds: float) -> None:
    """Manually reclaim one lane only after continuous observation of all proofs."""
    if idle_seconds < 1 or poll_seconds <= 0 or poll_seconds > idle_seconds:
        raise Refused("idle and poll periods must be positive, with poll no longer than idle")
    deadline = time.monotonic() + idle_seconds
    while True:
        projects = {project.name: project for project in compose_projects()}
        project = projects.get(name)
        if project is None:
            raise Refused(f"{name} is not a running Compose project")
        if project.unit != unit:
            raise Refused(f"{name} owner unit is {project.unit}, not {unit}")
        state, directory = owner_unit(unit)
        if state != "inactive" or directory != project.working_dir:
            raise Refused(f"{unit} is {state} or does not own {project.working_dir}")
        live = checkout_processes(project.working_dir)
        if live:
            raise Refused(f"{project.working_dir} still has process {live[0][0]} {live[0][1]}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_seconds, remaining))
    print(
        f"RECLAIM {name}: {unit} inactive; {project.working_dir} has no checkout process; "
        f"no pytest or gate process observed for {idle_seconds} seconds"
    )
    remove_compose_project(project)


def _load_distribute():
    """Import `gate_distribute` by path, so this works from any directory."""
    spec = importlib.util.spec_from_file_location(
        "gate_distribute", ROOT / "scripts" / "gate_distribute.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# The gate over several boxes (GATE-CROSS-BOX-SPLIT).
#
# `gate_distribute.split_plan` deals the suites out over this box's lanes and
# every configured box's. What follows is the other two thirds: the SLICE a box
# is sent, and the judging of the EVIDENCE it sends back.
#
# FAIL CLOSED, PER BOX (the owner's rule). A box whose evidence is absent, or
# not complete when the evidence is last read, has measured nothing: every
# suite of its slice is UNMEASURED and the run is red, however green the other
# boxes are. The deadline is hard for WAITING, not for reading: every file is
# read once more after it, because the integrator's own lanes often outlast it,
# and a complete record that is there then is accepted, never refused for
# arriving between the deadline and that read. Evidence
# is also another machine's word about a tree, so it is refused WHOLE unless it
# names this slice, this box and this candidate, and speaks only of suites the
# slice sent — a record that reports a suite it was never sent ran some other
# slice. A refused record becomes a result with no SUITE lines, which
# `summarise` already calls UNMEASURED: the red comes from code that existed.
#
# NO TRANSPORT HERE. This script writes each slice to a file and reads each
# box's evidence from a file it is pointed at (`--remote-evidence`); moving
# them over the refs the configuration names is the sessions' part, one writer
# per ref (`tell-wonko` and `wonko-checkin` say how).
# --------------------------------------------------------------------------

#: How long a slice may take, counted from the moment the slices are written.
#: Required once a box is configured, and it has no default: how long to wait
#: for another machine is a decision, not a constant (ADR-0002).
REMOTE_TIMEOUT_SETTING = "SKYKEEP_GATE_REMOTE_TIMEOUT_SECONDS"

#: The format of a slice and of the evidence answering it. An evidence record
#: in any other format is refused, not half-read.
SLICE_SCHEMA = 1

#: How often an evidence file that is not there yet, or not complete yet, is
#: looked at again while the deadline has not passed.
EVIDENCE_POLL_SECONDS = 15.0

#: The states a box's record of one slice never leaves. `complete` carries the
#: verdicts; `refused` is the box saying it ran nothing (the candidate would not
#: fetch, or its endpoint is not the slice's), and is red like an absent record.
EVIDENCE_FINAL_STATES = ("complete", "refused")

#: An evidence file is another machine's output: no more than this is read,
#: and no more than `EVIDENCE_SUMMARY_CHARS` of a suite's summary is repeated.
EVIDENCE_MAX_BYTES = 4 * 1024**2
EVIDENCE_SUMMARY_CHARS = 120

#: One host-name rule for both scripts: `gate_distribute` owns it.
_HOST = _load_distribute()._HOST

#: The one tracked file `skykeep.sh testgates` itself rewrites before this
#: script runs. `check_dependency_advisories.py --gate` must ask the advisory
#: service live, and it writes a live answer back with a new `audited_at` even
#: when nothing else in the audit changed — so every cross-box gate started
#: from a clean checkout refused its own tree as dirty (Batch42, 2026-10-02,
#: got past it only with `git update-index --assume-unchanged`). The lint keeps
#: writing: its stamp is what dates the audit on file for the freshness reuse
#: and the stale ceiling, and the gate's live demand is untouched. Instead the
#: clean-tree check excuses exactly this residue (`_advisory_snapshot_drift`).
#: Repo-relative, as every box sees it; `tests/adversarial/test_gate_run.py`
#: pins it to the lint's own `SNAPSHOT`.
ADVISORY_SNAPSHOT = "security/dependency-audit-snapshot.json"

#: The one key in that file a re-audit with an unchanged answer changes.
ADVISORY_SNAPSHOT_STAMP = "audited_at"


def _advisory_snapshot_drift(root: Path) -> str | None:
    """Why the snapshot's change is more than a new stamp, or None when it is not.

    The other boxes check out HEAD, so they read HEAD's snapshot. A working copy
    that differs from it in `audited_at` alone holds the same audit — the same
    packages, components and findings — dated later, and the local lanes measure
    nothing the boxes cannot. Any other difference is a different audit (a new
    finding, a moved pin), and that is refused exactly as any tracked change is.
    Fail closed: an unreadable copy on either side is drift.
    """
    refused = (
        f"{ADVISORY_SNAPSHOT} differs from HEAD's in more than its "
        f"{ADVISORY_SNAPSHOT_STAMP} stamp, so this tree holds a different "
        "dependency audit from the one another box would check out; commit "
        "it, or restore it, before a cross-box gate"
    )
    try:
        committed = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", f"HEAD:{ADVISORY_SNAPSHOT}"],
            capture_output=True, text=True, check=False,
        )
        live = (root / ADVISORY_SNAPSHOT).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return refused
    if committed.returncode != 0:
        return refused
    try:
        before, after = json.loads(committed.stdout), json.loads(live)
    except json.JSONDecodeError:
        return refused
    if not isinstance(before, dict) or not isinstance(after, dict):
        return refused
    if ADVISORY_SNAPSHOT_STAMP not in before or ADVISORY_SNAPSHOT_STAMP not in after:
        return refused
    before.pop(ADVISORY_SNAPSHOT_STAMP)
    after.pop(ADVISORY_SNAPSHOT_STAMP)
    return None if before == after else refused


def candidate_sha(root: Path | None = None) -> str:
    """The commit every box is asked to measure: HEAD, with no tracked change.

    A slice names a sha, and a box can only check out a sha. With a tracked
    change in this tree the local lanes would measure something the other
    boxes cannot, under one verdict — so that is refused, not noted.

    One change is not a change of what is measured: the advisory lint's own
    restamp of `ADVISORY_SNAPSHOT`, unstaged, with nothing else dirty beside it
    (`_advisory_snapshot_drift` says why that much and no more).
    """
    root = ROOT if root is None else Path(root)
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True, text=True, check=False,
        )
        # `-z`: one NUL-terminated `XY path` entry per change, never quoted,
        # so the one excused entry is compared whole rather than parsed.
        dirty = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), "status", "--porcelain", "-z",
             "--untracked-files=no"],
            capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        raise Refused(f"cannot ask git which commit is the candidate: {exc}")
    sha = head.stdout.strip()
    if head.returncode != 0 or dirty.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise Refused(f"cannot read the candidate commit of {root}")
    entries = [entry for entry in dirty.stdout.split("\0") if entry]
    if entries and entries != [f" M {ADVISORY_SNAPSHOT}"]:
        raise Refused(
            "the tree has uncommitted tracked changes, so no sha names what this "
            "box would measure and another box cannot measure the same thing"
        )
    if entries:
        drift = _advisory_snapshot_drift(root)
        if drift:
            raise Refused(drift)
    return sha


def lane_dirty_count(root: Path | None = None) -> int:
    """The `dirty=` figure of a lane's START line: changes, untracked files included.

    `scripts/skykeep.sh testgates` re-audits the dependencies before it runs, on
    every box that answers a slice as much as on the integrator's, and the lint
    restamps `ADVISORY_SNAPSHOT`. A box runner refuses a START line above 0 as a
    tree nobody named, so that residue must not be counted: it is excused here on
    exactly the terms `candidate_sha` excuses it (unstaged, the stamp the only
    difference from HEAD's copy). Every other entry still counts, and a tree git
    cannot read counts as dirty, never as clean.
    """
    root = ROOT if root is None else Path(root)
    try:
        status = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), "status", "--porcelain", "-z"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return 1
    if status.returncode != 0:
        return 1
    entries = [entry for entry in status.stdout.split("\0") if entry]
    excused = f" M {ADVISORY_SNAPSHOT}"
    if excused in entries and _advisory_snapshot_drift(root) is None:
        entries.remove(excused)
    return len(entries)


def remote_timeout_seconds(env=None) -> int:
    env = os.environ if env is None else env
    raw = (env.get(REMOTE_TIMEOUT_SETTING) or "").strip()
    if not raw.isdecimal() or int(raw) < 1:
        raise Refused(
            f"{REMOTE_TIMEOUT_SETTING} must be a positive whole number of seconds — "
            "a slice with no deadline can never be called timed out"
        )
    return int(raw)


#: The ref the candidate is published on, so every box can fetch the sha its
#: slice names. Required once a box is configured, with no default (ADR-0002);
#: the box runner reads the same setting from its own configuration.
CANDIDATE_REF_SETTING = "SKYKEEP_GATE_CANDIDATE_REF"


def candidate_ref(boxes: list[dict], env=None) -> str:
    """The candidate ref, or `Refused` naming the setting: unset, not a ref
    under `refs/`, or one of a box's own refs (a ref has one writer)."""
    env = os.environ if env is None else env
    ref = (env.get(CANDIDATE_REF_SETTING) or "").strip()
    if not ref:
        raise Refused(
            f"{CANDIDATE_REF_SETTING} is not set — a box measures only a candidate it can fetch, "
            "and the ref it is published on has no default (ADR-0002)"
        )
    if not _load_distribute()._GIT_REF.fullmatch(ref) or ".." in ref:
        raise Refused(f"{CANDIDATE_REF_SETTING} gives {ref!r}, which is not a ref under refs/")
    for box in boxes:
        if ref in (box["slice_ref"], box["status_ref"]):
            raise Refused(
                f"{CANDIDATE_REF_SETTING} {ref} is also box {box['name']}'s slice or status ref: "
                "each ref has one writer and one job"
            )
    return ref


def slice_expiry(timeout: int, now: datetime | None = None) -> str:
    """When a slice cut now stops being worth running, in UTC: the deadline
    this run waits to, written into the slice so the box can see it too."""
    now = datetime.now(UTC) if now is None else now
    return (now + timedelta(seconds=timeout)).strftime("%Y-%m-%dT%H:%M:%SZ")


def box_slice(box: dict, candidate: str, slice_id: str, path_of=None, *, endpoint: str,
              candidate_ref: str | None = None, expires_utc: str | None = None) -> dict:
    """What one box is sent: its share of the suites, the suites it runs alone,
    the commit to run them on, the model endpoint to run them against, and the
    two refs it rides.

    `box` is one entry of `split_plan`'s `boxes`; `path_of` turns the planner's
    basenames back into the suite paths the lane script takes; `endpoint` is
    the HOST this run's own lanes use (`gate_distribute.endpoint_marker()`).

    `lanes` is how THIS planner packed the box's share, and it is advisory: the
    box runs the share through its own `testgates`, which re-plans it over the
    lanes it has, with its own barrier, tails and memory floor. What binds is
    the SET of suites, `exclusive`, the candidate and the endpoint: evidence is
    judged against those, never against the order.
    """
    path_of = path_of or (lambda suite: suite)
    if not isinstance(endpoint, str) or not _HOST.fullmatch(endpoint):
        raise Refused(
            f"a slice names the model endpoint its suites run against, and {endpoint!r} "
            "is not a host name"
        )
    return {
        "schema": SLICE_SCHEMA,
        "slice_id": slice_id,
        "box": box["name"],
        "candidate": candidate,
        "endpoint": endpoint,
        "lanes": [[path_of(s) for s in lane] for lane in box["ordered"] if lane],
        "exclusive": [path_of(s) for s in box["exclusive"]],
        # What the box runs them on (GATE-CROSS-BOX-CAPACITY): the lanes it
        # declared, and how many of them may run an endpoint suite at once —
        # its share of the cap over every box, which its own re-plan keeps to
        # through `--endpoint-lanes`. None when the plan was made uncapped.
        "first_lane": box.get("first_lane"),
        "lane_count": box.get("lanes"),
        "endpoint_lanes": box.get("endpoint_lanes"),
        "slice_ref": box["slice_ref"],
        "status_ref": box["status_ref"],
        # Where the candidate is fetched from, and when this run stops waiting
        # (GATE-CROSS-BOX-SPLIT review MED3): a slice past `expires_utc` is
        # one nobody reads the answer to.
        "candidate_ref": candidate_ref,
        "expires_utc": expires_utc,
    }


def slice_suites(sent: dict) -> list[str]:
    """Every suite a slice sends, lanes first, then the ones it runs alone."""
    return [s for lane in sent["lanes"] for s in lane] + list(sent["exclusive"])


def _evidence_record(text: str | None) -> dict | None:
    try:
        record = json.loads(text or "")
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def evidence_final(text: str | None, slice_id: str) -> bool:
    """Whether a box has said its last word on THIS slice: complete, or a
    state it never leaves (`EVIDENCE_FINAL_STATES`).

    The wait ends on either. Only `complete` can carry a verdict; a box that
    published `refused` ran nothing, and holding the run to the deadline on
    its account would spend the deadline and learn nothing more.
    """
    record = _evidence_record(text)
    return (
        record is not None
        and record.get("state") in EVIDENCE_FINAL_STATES
        and record.get("slice_id") == slice_id
    )


def _said(value, limit: int = EVIDENCE_SUMMARY_CHARS) -> str:
    """Another machine's text, fit to repeat: one line, printable, bounded."""
    text = value if isinstance(value, str) else repr(value)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(text.split())[:limit]


def _a_count(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < float("inf")


def judge_box_evidence(sent: dict, text: str | None, *, timed_out: bool = False) -> dict:
    """One box's evidence as a lane result `summarise` reads, fail closed.

    `text` is the evidence file's content, or None when there is none.
    `timed_out` says the deadline passed without a complete record. Refused
    evidence yields a result with NO suites, so every suite the slice sent is
    UNMEASURED and `unmeasured_why` says which rule refused it. Accepted
    evidence yields one `(suite, rc, summary)` per verdict; a sent suite it
    leaves out stays UNMEASURED, and rc 124 is UNMEASURED there as here.

    `measured` and `endpoint` are for `gate_distribute.record_box_durations`,
    and are empty for refused evidence: a record this run would not believe
    must not teach the packer either.
    """
    name = sent["box"]
    sent_suites = slice_suites(sent)

    def result(suites, why, record=None, measured=()):
        wall = (record or {}).get("wall_seconds")
        endpoint = (record or {}).get("endpoint")
        return {
            "lane": f"{name} (remote box)",
            "planned": sent_suites,
            "exit": 0 if suites else 1,
            "wall": float(wall) if _a_count(wall) else 0.0,
            "log": sent["status_ref"],
            "suites": suites,
            "unmeasured_why": why,
            "endpoint": endpoint if isinstance(endpoint, str) and _HOST.fullmatch(endpoint) else None,
            "measured": list(measured),
        }

    def refused(reason):
        return result([], f"box {name}'s evidence refused: {reason}")

    record = _evidence_record(text)
    if timed_out and (record is None or record.get("state") != "complete"):
        if text is None:
            return result([], f"box {name}'s slice timed out: no evidence by the deadline")
        state = _said(record.get("state"), 40) if record else "unreadable"
        return result(
            [], f"box {name}'s slice timed out: its evidence was still {state} at the deadline"
        )
    if text is None:
        return result([], f"no evidence from box {name}")
    if record is None:
        return refused("malformed, not one JSON record")
    if record.get("schema") != SLICE_SCHEMA:
        return refused(f"schema {_said(record.get('schema'), 40)}, this run reads {SLICE_SCHEMA}")
    if record.get("box") != name:
        return refused(f"it is box {_said(record.get('box'), 40)}'s")
    if record.get("slice_id") != sent["slice_id"]:
        return refused(f"it answers slice {_said(record.get('slice_id'), 80)}, not {sent['slice_id']}")
    if record.get("candidate") != sent["candidate"]:
        return refused(
            f"it measured candidate {_said(record.get('candidate'), 64)}, not {sent['candidate']}"
        )
    if record.get("state") != "complete":
        reason = record.get("reason")
        return refused(
            f"its state is {_said(record.get('state'), 40)}, not complete"
            + (f" ({_said(reason)})" if reason else "")
        )
    # A verdict is a measurement of a tree AGAINST AN ENDPOINT. A box that ran
    # its slice on another endpoint — another host, or a model on the box,
    # which `AGENTS.md` forbids — measured something this run did not ask
    # about, and its green is not this run's. A record that names no endpoint
    # cannot show it is not one of those, so it is refused the same way.
    endpoint = record.get("endpoint")
    if not isinstance(endpoint, str) or endpoint != sent.get("endpoint"):
        return refused(
            f"it was measured on endpoint {_said(endpoint, 80) if endpoint else '<none named>'}, "
            f"not {sent.get('endpoint')}"
        )
    # What the box ran its slice ON (GATE-CROSS-BOX-CAPACITY): how many lanes,
    # and how many of them ran endpoint suites, each copied from the slice
    # into `testgates --lanes/--endpoint-lanes` and then into the record. A
    # figure typed by hand drifts; one past the slice's ran a load the plan
    # never gave that box, and a record that states none cannot show it kept
    # to the slice. A slice that states no figure sets nothing to exceed.
    for key, least in (("lane_count", 1), ("endpoint_lanes", 0)):
        bound = sent.get(key)
        if bound is None:
            continue
        said = record.get(key)
        if not _a_whole(said) or said < least:
            return refused(f"it does not state its {key} as a whole number of at least {least}")
        if said > bound:
            return refused(f"it ran with {key} {said}, past the slice's {bound}")

    # The box's own summary of the slice is typed: a wall time that is not a
    # count is a hand-built record, not what the box runner writes.
    if record.get("wall_seconds") is not None and not _a_count(record["wall_seconds"]):
        return refused("malformed, its wall_seconds is not a count")
    verdicts = record.get("suites")
    if not isinstance(verdicts, list):
        return refused("malformed, its suites are not a list")
    seen: list[str] = []
    for verdict in verdicts:
        ok = (
            isinstance(verdict, dict)
            and isinstance(verdict.get("suite"), str)
            and isinstance(verdict.get("rc"), int)
            and _a_count(verdict["rc"])
            and (verdict.get("seconds") is None or _a_count(verdict["seconds"]))
        )
        if not ok:
            return refused("malformed, a verdict is not {suite, rc, seconds}")
        seen.append(verdict["suite"])
    twice = sorted({s for s in seen if seen.count(s) > 1})
    if twice:
        return refused(f"it reports {', '.join(_said(s) for s in twice)} twice")
    stray = sorted(set(seen) - set(sent_suites))
    if stray:
        return refused(
            f"it reports {', '.join(_said(s) for s in stray)}, which this slice never sent"
        )

    suites, measured = [], []
    for verdict in verdicts:
        seconds = verdict.get("seconds")
        took = f" in {float(seconds):.2f}s" if seconds is not None else ""
        said = _said(verdict.get("summary", "")) if verdict.get("summary") else ""
        suites.append((verdict["suite"], verdict["rc"], f"{said}{took} (box {name})".strip()))
        measured.append((verdict["suite"], verdict["rc"], seconds))
    return result(suites, f"box {name}'s evidence holds no verdict for it", record, measured)


def wait_for_evidence(slices: list[dict], paths: dict, deadline: float, *, refresh=None,
                      clock=None, sleep=None) -> dict:
    """`{box: (text or None, timed_out)}` once every slice's evidence is
    final or `deadline` (a `clock()` value, `time.monotonic()` by default)
    has passed.

    Every file is read at least once, so a run whose own lanes outlasted the
    deadline still reads what arrived. A box is `timed_out` when its record is
    not this slice's final one at the last read — absent, unreadable, still
    running, or left over from another run alike. A final record that is not
    `complete` ends that box's wait early and is refused by
    `judge_box_evidence`, never believed.

    `refresh(pending)`, when given, is called before every read with the
    boxes whose record is not final yet: an automatic split run copies each
    one's status ref into its file there (`gate_remote.evidence_refresher`).
    """
    clock = time.monotonic if clock is None else clock
    sleep = time.sleep if sleep is None else sleep
    wanted = {sent["box"]: sent["slice_id"] for sent in slices}
    paths = {name: paths[name] for name in wanted}
    texts: dict = {}
    while True:
        if refresh is not None:
            refresh([name for name in paths if not evidence_final(texts.get(name), wanted[name])])
        for name, path in paths.items():
            if evidence_final(texts.get(name), wanted[name]):
                continue
            try:
                with open(path, errors="replace") as handle:
                    read = handle.read(EVIDENCE_MAX_BYTES + 1)
                texts[name] = read if len(read) <= EVIDENCE_MAX_BYTES else ""
            except OSError:
                texts[name] = None
        pending = {name for name in paths if not evidence_final(texts[name], wanted[name])}
        left = deadline - clock()
        if not pending or left <= 0:
            return {name: (texts[name], name in pending) for name in paths}
        sleep(min(EVIDENCE_POLL_SECONDS, left))


def remote_setup(args, boxes: list[dict], endpoint: str | None = None) -> dict | None:
    """What a split run needs beyond the plan, or None when no box is configured.

    Raises `Refused` for a flag naming a box the configuration does not, for a
    missing deadline, for a candidate no sha names, and for an `endpoint` (this
    run's model host, which every slice names and every record must repeat)
    that is not a host name.
    """
    names = {box["name"] for box in boxes}
    paths = {}
    for item in args.remote_evidence:
        name, equals, path = item.partition("=")
        if not equals or not path:
            raise Refused(f"--remote-evidence {item!r} is not BOX=FILE")
        if name not in names:
            raise Refused(f"--remote-evidence names {name}, which is not a configured box")
        paths[name] = Path(path)
    for name in args.idle_box:
        if name not in names:
            raise Refused(f"--idle-box names {name}, which is not a configured box")
    if not boxes:
        return None
    if not isinstance(endpoint, str) or not _HOST.fullmatch(endpoint):
        raise Refused(
            "SKYKEEP_GATE_MODEL_BASE_URL names no host, so no slice can say which "
            "endpoint its verdicts must come from"
        )
    return {
        "timeout": remote_timeout_seconds(),
        "candidate_ref": candidate_ref(boxes),
        "candidate": candidate_sha(),
        "paths": paths,
        "endpoint": endpoint,
    }


# --------------------------------------------------------------------------
# The integrating box's side of the wire, automatic (GATE-INTEGRATOR-SLICE-PUBLISH).
#
# With a box configured and neither `--box-declaration` nor `--remote-evidence`
# given, this run does what a person on this box used to: it publishes the
# candidate (refusing one whose push would publish anything but this batch's
# merges of pushed seams), asks every box for its declaration and waits for it,
# publishes each slice, copies each box's evidence in while the barrier
# waits, and deletes the refs it published afterwards. `scripts/gate_remote.py`
# is that transport; what is published and judged stays here.
# --------------------------------------------------------------------------

_REMOTE = None


def _load_remote():
    """`gate_remote`, imported by path once, so its exception class is one class."""
    global _REMOTE
    if _REMOTE is None:
        spec = importlib.util.spec_from_file_location("gate_remote", ROOT / "scripts" / "gate_remote.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _REMOTE = module
    return _REMOTE


def open_transport(boxes: list[dict], candidate_ref_name: str):
    """The transport of an automatic split run, configured from the environment."""
    return _load_remote().load(
        os.environ, ROOT, candidate_ref=candidate_ref_name,
        max_declaration_wait=DECLARATION_MAX_AGE_SECONDS, here=_load_distribute().this_box(),
    )


def declaration_verdict(name: str, text: str | None, now: float) -> str | None:
    """`judge_declaration` as the transport asks it: None, or why not."""
    try:
        judge_declaration(name, text, now)
    except Refused as exc:
        return str(exc)
    return None


# --------------------------------------------------------------------------
# What each box can hold (GATE-CROSS-BOX-CAPACITY).
#
# A box's lane count is MEASURED on that box at slice start and declared, never
# assumed from a figure somebody wrote down: the box runs `--declare-box`, which
# reads its own MemAvailable, cores and CPU reservation and names the lane range
# it is free to use, and publishes that record. The plan then applies to each
# box the rule it applies here — the lane window, MemAvailable less the reserve
# over one lane's measured footprint, the usable cores, the smallest winning —
# with no MAX_AUTO_LANES clamp: a box with the memory runs the whole window. A
# listed box with no usable declaration gets no slice, and the run refuses
# rather than measuring a gate short of a box it was told to use.
# --------------------------------------------------------------------------

DECLARATION_SCHEMA = 1

#: How old a box's declaration may be when the plan reads it. MemAvailable on
#: a box that also runs sessions and seams moves in minutes; a declaration older
#: than this describes a box that is no longer there.
DECLARATION_MAX_AGE_SECONDS = 900

#: How far ahead of this box's clock a declaration's time may be: the boxes'
#: clocks are not one clock. Further ahead than this is not a measurement.
DECLARATION_CLOCK_SKEW_SECONDS = 120

DECLARATION_MAX_BYTES = 64 * 1024

#: A gate lane's compose project, and nothing else: the live vault is
#: `skykeep` and testlong's stack `skykeep-testlong`, neither of them a lane.
LANE_PROJECT = re.compile(r"skykeep-p([0-9]+)")


def _docker(argv: list[str], runner: Callable[[list[str]], str] = _output) -> str:
    """A `docker` command on this box's daemon: directly, or through the docker
    group when this session is outside it, as `gate_lane.sh` falls back to.
    Refused when neither answers: a question docker did not answer has no
    answer, and least of all "nothing is there"."""
    try:
        return runner(argv)
    except (Refused, OSError) as direct:
        try:
            return runner(["sg", "docker", "-c", shlex.join(argv)])
        except (Refused, OSError) as grouped:
            raise Refused(
                f"cannot reach the docker daemon, directly ({direct}) or through the "
                f"docker group ({grouped})"
            ) from None


def lane_stacks(runner: Callable[[list[str]], str] = _output) -> dict[int, set[str]]:
    """Every gate-lane stack on this box's docker daemon, running or stopped:
    `{lane: {the checkout its containers were built from}}`, with "" for a
    container that does not say.

    Stopped ones count. A lane's barrier finds a stack whose containers are
    down and whose volumes outlived them, calls that a vault it cannot read,
    and brings it down with its volumes, so a stopped stack is a vault too.
    """
    listing = _docker(
        [
            "docker", "ps", "--all", "--filter", "label=com.docker.compose.project", "--format",
            '{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.project.working_dir"}}',
        ],
        runner,
    )
    found: dict[int, set[str]] = {}
    for line in listing.splitlines():
        project, _tab, built = line.partition("\t")
        match = LANE_PROJECT.fullmatch(project.strip())
        if match:
            found.setdefault(int(match.group(1)), set()).add(built.strip())
    return found


def parse_lane_list(text: str) -> list[int]:
    """Lanes a comma-separated list names, ascending, or `Refused` naming the fault.

    `36,37,39` is those three lanes and not the gap. A token that is not a lane
    number, a lane outside the window, or a duplicate is named in the refusal.
    """
    parts = [part.strip() for part in text.split(",")]
    if not parts or any(not part for part in parts):
        raise Refused(f"{text!r} is not a list of lane numbers")
    lanes: list[int] = []
    for part in parts:
        if not part.isdecimal():
            raise Refused(f"{part!r} is not a lane number")
        lane = int(part)
        if not LANE_FIRST <= lane <= LANE_LAST:
            raise Refused(f"lane {lane} is not inside {LANE_FIRST}..{LANE_LAST}")
        if lane in lanes:
            raise Refused(f"names {lane} twice")
        lanes.append(lane)
    lanes.sort()
    return lanes


def refuse_taken_lanes(
    first_lane: int,
    lanes: int,
    *,
    stacks: Callable[[], dict[int, set[str]]] | None = None,
    mine: Path | None = None,
    lane_numbers: list[int] | None = None,
) -> None:
    """Refuse a lane range any lane of which holds a stack, naming each one
    and the checkout that built it.

    Lanes 31..39 are a register a box's sessions share: each stream file gives
    its session lanes, and a peer's stack may be up on any of them. A lane's
    barrier brings a stack it cannot vouch for down WITH ITS VOLUMES, so a run
    that took a peer's lane would destroy that peer's live vault. The one
    stack let through is one every container of which `mine` built: a run's
    own lanes, left up by its own checkout's last run, which it reuses as every
    single-box run always has. `stacks` is `lane_stacks` unless a caller (a
    test) says otherwise.
    """
    found = (lane_stacks if stacks is None else stacks)()
    own = os.path.realpath(mine) if mine is not None else None
    numbered = (
        list(lane_numbers) if lane_numbers is not None
        else list(range(first_lane, first_lane + lanes))
    )
    held = []
    for lane in numbered:
        built = found.get(lane)
        if not built:
            continue
        if own is not None and all(b and os.path.realpath(b) == own for b in built):
            continue
        named = ", ".join(sorted(b or "<no checkout named>" for b in built))
        held.append(f"skykeep-p{lane} (built from {named})")
    if held:
        shown = (
            f"{first_lane}..{first_lane + lanes - 1}" if lane_numbers is None
            else ",".join(str(lane) for lane in numbered)
        )
        raise Refused(
            f"lanes {shown} are not free: {'; '.join(held)}. "
            "A lane's barrier brings a stack it cannot vouch for down with its volumes, so "
            "name lanes no stack sits on (the ones this box's stream file gives this "
            "session), or bring that stack down yourself once its owner is done with it"
        )


def declare_box(
    name: str,
    first_lane: int,
    lanes: int,
    now: float | None = None,
    *,
    stacks: Callable[[], dict[int, set[str]]] | None = None,
    chosen: list[int] | None = None,
) -> dict:
    """This box's capacity, measured now, for the integrator's plan.

    The lanes it names must be free: no lane stack on any of them, whoever
    built it (`refuse_taken_lanes` with no `mine`). `chosen`, when given, is
    those lanes and not the integers between the first and the last — a gap
    another stream holds is not declared. The slice runs in a checkout of its
    own at the candidate, which cannot reuse a stack another checkout built,
    and whose barrier would bring it down.
    """
    if chosen is None:
        if lanes < 1 or not LANE_FIRST <= first_lane <= LANE_LAST or first_lane + lanes - 1 > LANE_LAST:
            raise Refused(
                f"a box declares lanes inside {LANE_FIRST}..{LANE_LAST} (asked {lanes} from {first_lane})"
            )
        refuse_taken_lanes(first_lane, lanes, stacks=stacks)
        lane_numbers = list(range(first_lane, first_lane + lanes))
        named: list[int] | None = None
    else:
        lane_numbers = parse_lane_list(",".join(str(lane) for lane in chosen))
        if (first_lane, lanes) != (lane_numbers[0], len(lane_numbers)):
            raise Refused(
                f"asked {lanes} from {first_lane}, not the lane list "
                f"{','.join(str(lane) for lane in lane_numbers)}"
            )
        refuse_taken_lanes(
            lane_numbers[0], len(lane_numbers), stacks=stacks, lane_numbers=lane_numbers,
        )
        named = lane_numbers
    available = mem_available_bytes()
    if available is None:
        raise Refused("MemAvailable is unknown, so this box has nothing true to declare")
    cores = machine_cores()
    if not cores:
        raise Refused("CPU count is unknown, so this box has nothing true to declare")
    record = {
        "schema": DECLARATION_SCHEMA,
        "box": name,
        "measured_at": int(time.time() if now is None else now),
        "mem_available_bytes": available,
        "cores": cores,
        "reserved_cpus": reserved_cpus(cores),
        "first_lane": lane_numbers[0],
        "last_lane": lane_numbers[-1],
    }
    if named is not None:
        record["lanes"] = named
    return record


def _a_whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def declared_lane_numbers(record: dict) -> list[int]:
    """The lanes one declaration gives this box.

    No `lanes` key: the inclusive range, as every declaration wrote before a
    box could name a gap. A `lanes` list is those lanes and no others — the
    integers between the first and the last are not implied. A duplicate or a
    lane outside the window is refused and named.
    """
    listed = record.get("lanes")
    if listed is None:
        first, last = record["first_lane"], record["last_lane"]
        if not _a_whole(first) or not _a_whole(last) or first > last:
            return []
        return list(range(first, last + 1))
    if not isinstance(listed, list) or not listed:
        raise Refused("lanes is not a list of lane numbers")
    lanes: list[int] = []
    for lane in listed:
        if not _a_whole(lane):
            raise Refused(f"lanes names {_said(lane, 20)}, which is not a lane number")
        if not LANE_FIRST <= lane <= LANE_LAST:
            raise Refused(f"lane {lane} is not inside {LANE_FIRST}..{LANE_LAST}")
        if lane in lanes:
            raise Refused(f"lanes names {lane} twice")
        lanes.append(lane)
    if lanes != sorted(lanes):
        raise Refused(
            f"lanes {','.join(str(lane) for lane in lanes)} are not in ascending order"
        )
    if lanes[0] != record["first_lane"] or lanes[-1] != record["last_lane"]:
        raise Refused(
            f"lanes {','.join(str(lane) for lane in lanes)} do not run from "
            f"{record['first_lane']} to {record['last_lane']}"
        )
    return lanes


def judge_declaration(name: str, text: str | None, now: float | None = None) -> dict:
    """A box's declaration, or `Refused` naming what is wrong with it."""
    if text is None:
        raise Refused("no declaration")
    try:
        record = json.loads(text)
    except ValueError:
        record = None
    if not isinstance(record, dict):
        raise Refused("malformed, not one JSON record")
    if record.get("schema") != DECLARATION_SCHEMA:
        raise Refused(f"schema {_said(record.get('schema'), 40)}, this run reads {DECLARATION_SCHEMA}")
    if record.get("box") != name:
        raise Refused(f"it is box {_said(record.get('box'), 40)}'s")
    for key in ("measured_at", "mem_available_bytes", "cores", "reserved_cpus", "first_lane", "last_lane"):
        if not _a_whole(record.get(key)) or record[key] < 0:
            raise Refused(f"malformed, {key} is not a whole number")
    age = (time.time() if now is None else now) - record["measured_at"]
    if age > DECLARATION_MAX_AGE_SECONDS:
        raise Refused(
            f"measured {int(age)}s ago, older than {DECLARATION_MAX_AGE_SECONDS}s: re-declare at slice start"
        )
    if age < -DECLARATION_CLOCK_SKEW_SECONDS:
        raise Refused(f"measured {int(-age)}s in this box's future")
    if record["cores"] < 1 or record["reserved_cpus"] >= record["cores"]:
        raise Refused("its cores leave none usable")
    first, last = record["first_lane"], record["last_lane"]
    if not LANE_FIRST <= first <= last <= LANE_LAST:
        raise Refused(f"lanes {first}..{last} are not inside {LANE_FIRST}..{LANE_LAST}")
    declared_lane_numbers(record)
    return record


def box_capacity(record: dict, lane_bytes: int, per_worker_bytes: int, ceiling: int | None = None) -> dict:
    """The per-box rule over one declaration: `{lanes, first_lane, workers, why}`.

        lanes = min(free lanes, (MemAvailable - reserve) // lane, cores - reserved)

    A declaration with no `lanes` list counts the inclusive range. One that
    names its lanes counts that list, so a gap is not a free lane. `ceiling`
    (an operator's `name:lanes`) sits below that; the workers are
    `wave_capacity`'s rule (`worker_budget`) for those lanes on that box.
    Refused when the box affords no lane.
    """
    numbers = declared_lane_numbers(record)
    window = len(numbers)
    if record.get("lanes") is None:
        shown = f"{record['first_lane']}..{record['last_lane']}"
        start = record["first_lane"]
    else:
        shown = ",".join(str(number) for number in numbers)
        start = numbers[0]
    by_memory = (record["mem_available_bytes"] - HOST_RESERVE_BYTES) // lane_bytes
    usable = record["cores"] - record["reserved_cpus"]
    lanes = min(window, by_memory, usable, *(() if ceiling is None else (ceiling,)))
    why = (
        f"MemAvailable {record['mem_available_bytes'] / 1024**3:.1f} GiB - "
        f"{HOST_RESERVE_BYTES / 1024**3:.0f} GiB reserve / {lane_bytes / 1024**3:.1f} GiB per lane "
        f"= {by_memory}; {record['cores']} cores - {record['reserved_cpus']} reserved = {usable}; "
        f"lanes {shown} free = {window}"
        + (f"; ceiling {ceiling}" if ceiling is not None else "")
        + f"; so {max(lanes, 0)}"
    )
    if lanes < 1:
        raise Refused(f"it affords no lane ({why})")
    workers = worker_budget(record["mem_available_bytes"], usable, lanes, per_worker_bytes, lane_bytes)
    return {"lanes": lanes, "first_lane": start, "workers": workers, "why": why}


def declared_boxes(args, boxes: list[dict], lane_bytes: int, per_worker_bytes: int,
                   now: float | None = None, texts: dict | None = None) -> list[dict]:
    """Every listed box, its lane count replaced by what it declared.

    Refuses, naming every box at once, when any listed box has no usable
    declaration: a box the operator listed is a box this run must measure on,
    and a plan quietly made without it would look whole. `texts` is what an
    automatic split run fetched off each status ref, in place of the files
    `--box-declaration` names, and is judged exactly as they are.
    """
    names = {box["name"] for box in boxes}
    paths = {}
    for item in args.box_declaration:
        name, equals, path = item.partition("=")
        if not equals or not path:
            raise Refused(f"--box-declaration {item!r} is not BOX=FILE")
        if name not in names:
            raise Refused(f"--box-declaration names {name}, which is not a configured box")
        if name in paths:
            raise Refused(
                f"--box-declaration names {name} twice: a box declares once per run, and "
                "which of two figures is true is not this run's to guess"
            )
        paths[name] = Path(path)
    if texts is not None and paths:
        raise Refused(
            "--box-declaration is the hand-run flow; an automatic split run fetches each declaration"
        )

    def declared(name: str) -> str | None:
        if texts is not None:
            return texts.get(name)
        path = paths.get(name)
        if path is None:
            raise Refused("no --box-declaration")
        try:
            with open(path, errors="replace") as handle:
                text = handle.read(DECLARATION_MAX_BYTES + 1)
        except OSError:
            return None
        if len(text) > DECLARATION_MAX_BYTES:
            raise Refused("larger than any declaration")
        return text

    unusable = []
    for box in boxes:
        try:
            record = judge_declaration(box["name"], declared(box["name"]), now)
            capacity = box_capacity(record, lane_bytes, per_worker_bytes, box.get("ceiling"))
        except Refused as exc:
            unusable.append(f"{box['name']} ({exc})")
            continue
        box.update(
            lanes=capacity["lanes"],
            first_lane=capacity["first_lane"],
            workers=capacity["workers"],
            capacity_why=capacity["why"],
        )
    if unusable:
        raise Refused(
            "no usable capacity declaration from " + "; ".join(unusable) + " — a listed box "
            "declares its MemAvailable, cores and free lanes at slice start "
            "(gate_run.py --declare-box), and one that has not gets no slice: declare it, "
            "or leave it out of SKYKEEP_GATE_REMOTE_BOXES for this run"
        )
    return boxes


#: The lanes this box's stream file gives this session, as every box names its
#: own (the box runner reads the same two settings on its box). With both set,
#: a split run given no `--lanes` declares THIS box the way it asks every other
#: box to, and runs on what that declaration affords.
BOX_FIRST_LANE_SETTING = "SKYKEEP_GATE_BOX_FIRST_LANE"
BOX_LAST_LANE_SETTING = "SKYKEEP_GATE_BOX_LAST_LANE"

SPLIT_NEEDS_LANES = (
    "a split run takes this box's lanes from --lanes (with --first-lane), the free range this "
    "session's stream file gives it, as every listed box names its own: lanes counted from "
    "MemAvailable alone may be a peer's. Or leave both flags out and set "
    f"{BOX_FIRST_LANE_SETTING} and {BOX_LAST_LANE_SETTING} to that range: this box then "
    "declares its own lanes over it, as every other box does"
)


def local_lanes(first_given: int | None, lane_bytes: int, per_worker_bytes: int, env=None,
                stacks: Callable[[], dict[int, set[str]]] | None = None) -> tuple[int, int, str]:
    """`(lanes, first lane, why)` for THIS box in a split run with no `--lanes`.

    The range is the two settings, and the count is what the per-box rule
    (`box_capacity`) gives this box's own declaration over the free lanes from
    the first one up: the rule every other box's share is cut by. A lane
    another checkout's stack sits on ends the range below it, and the first
    lane held that way refuses the run, naming the stack.
    """
    env = os.environ if env is None else env
    raw = [(env.get(name) or "").strip() for name in (BOX_FIRST_LANE_SETTING, BOX_LAST_LANE_SETTING)]
    if first_given is not None or not all(raw):
        raise Refused(SPLIT_NEEDS_LANES)
    if not all(value.isdecimal() for value in raw):
        raise Refused(f"{BOX_FIRST_LANE_SETTING} and {BOX_LAST_LANE_SETTING} must be lane numbers")
    first, last = (int(value) for value in raw)
    if not LANE_FIRST <= first <= last <= LANE_LAST:
        raise Refused(
            f"{BOX_FIRST_LANE_SETTING}..{BOX_LAST_LANE_SETTING} gives lanes {first}..{last}, "
            f"not a range inside {LANE_FIRST}..{LANE_LAST}"
        )
    found = (lane_stacks if stacks is None else stacks)()
    own = os.path.realpath(ROOT)
    free = []
    for lane in range(first, last + 1):
        built = found.get(lane)
        if built and not all(b and os.path.realpath(b) == own for b in built):
            break
        free.append(lane)
    if not free:
        refuse_taken_lanes(first, 1, stacks=lambda: found, mine=ROOT)
        raise Refused(f"lane {first} is not free")
    available, cores = mem_available_bytes(), machine_cores()
    if available is None or not cores:
        raise Refused("MemAvailable or the CPU count is unknown, so this box has nothing true to declare")
    record = {
        "schema": DECLARATION_SCHEMA, "box": _load_distribute().this_box(), "measured_at": int(time.time()),
        "mem_available_bytes": available, "cores": cores, "reserved_cpus": reserved_cpus(cores),
        "first_lane": first, "last_lane": free[-1],
    }
    capacity = box_capacity(record, lane_bytes, per_worker_bytes)
    held = (
        f" (lane {free[-1] + 1} holds another checkout's stack, so the range ends at {free[-1]})"
        if free[-1] < last else ""
    )
    return capacity["lanes"], first, f"declared by this box over lanes {first}..{last}{held}: {capacity['why']}"


def parse_memory(text: str, flag: str = "--memory-per-lane") -> int:
    """`3G` / `512M` / `2.5G` / a bare byte count, in bytes."""
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([KMGT]?)i?[Bb]?\s*", text or "")
    if not m:
        raise Refused(f"{flag} '{text}' is not a size like 3G or 512M")
    value = float(m.group(1)) * _UNITS[m.group(2).upper()]
    if value <= 0:
        raise Refused(f"{flag} must be greater than zero")
    return int(value)


def mem_available_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def accepted_reds(path, today) -> dict[str, tuple[date, str]]:
    """The suites the owner accepts as red until a date: `{suite: (expiry, citation)}`.

    `path` names a JSON list of `{"suite", "expiry", "citation"}` objects (the
    `SKYKEEP_GATE_ACCEPTED_REDS` setting; empty or None means none). An entry
    with no suite, an unknown suite, no citation, no or malformed expiry, or
    naming `test_gate_bm25.py` raises `Refused`. An entry expired before
    `today` is ignored with one line, so the suite blocks again with no edit.
    `today` is a parameter: the parser never reads the clock.
    """
    if not path or not str(path).strip():
        return {}
    try:
        entries = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise Refused(f"SKYKEEP_GATE_ACCEPTED_REDS {path} is unreadable: {exc}") from exc
    if not isinstance(entries, list):
        raise Refused(f"SKYKEEP_GATE_ACCEPTED_REDS {path} must hold a JSON list of entries")
    known = set(discover_suites())
    accepted: dict[str, tuple[date, str]] = {}
    for n, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise Refused(f"accepted-reds entry {n} is not an object")
        fields = {k: str(entry.get(k) or "").strip() for k in ("suite", "expiry", "citation")}
        for key, value in fields.items():
            if not value:
                raise Refused(f"accepted-reds entry {n} has no {key}")
        suite, citation = fields["suite"], fields["citation"]
        if Path(suite).name == "test_gate_bm25.py":
            raise Refused(f"accepted-reds entry {n}: {suite} (bm25) can never be an accepted red")
        if suite not in known:
            raise Refused(f"accepted-reds entry {n} names an unknown suite {suite}")
        try:
            expiry = date.fromisoformat(fields["expiry"])
        except ValueError as exc:
            raise Refused(
                f"accepted-reds entry {n} ({suite}) has a malformed expiry {fields['expiry']!r}"
            ) from exc
        if expiry < today:
            print(f"accepted red for {suite} expired {expiry}; it blocks again")
            continue
        accepted[suite] = (expiry, citation)
    return accepted


def check_config(env=None) -> dict[str, str]:
    """Every setting a lane needs, or a refusal naming the first one missing.

    Raises `Refused`; returns the resolved values so the caller does not read
    the environment a second time and disagree with the check.
    """
    env = os.environ if env is None else env

    url = (env.get("SKYKEEP_GATE_MODEL_BASE_URL") or "").strip()
    if not url:
        raise Refused(
            "SKYKEEP_GATE_MODEL_BASE_URL is not set — a gate lane has no default "
            "model endpoint (ADR-0002)"
        )
    if "://" not in url:
        raise Refused(f"SKYKEEP_GATE_MODEL_BASE_URL is not a URL: {url!r}")
    if "@" in url.split("://", 1)[1].split("/", 1)[0]:
        raise Refused(
            "SKYKEEP_GATE_MODEL_BASE_URL carries credentials — the bearer goes in "
            "a header, never in a URL"
        )

    secret = (env.get("SKYKEEP_GATE_MODEL_SECRET_FILE") or "").strip()
    if not secret:
        raise Refused(
            "SKYKEEP_GATE_MODEL_SECRET_FILE is not set — a gate lane has no "
            "default credential path (ADR-0002)"
        )
    secret_path = Path(secret)
    if not secret_path.is_file():
        raise Refused(f"SKYKEEP_GATE_MODEL_SECRET_FILE {secret} does not exist")
    mode = stat.S_IMODE(secret_path.stat().st_mode)
    if mode != 0o600:
        raise Refused(
            f"SKYKEEP_GATE_MODEL_SECRET_FILE {secret} is mode 0{mode:o}, must be "
            "0600 — a credential other users can read is not a credential"
        )
    try:
        secret_path.read_bytes()
    except OSError as exc:
        raise Refused(f"SKYKEEP_GATE_MODEL_SECRET_FILE {secret} is unreadable: {exc}")

    # MODEL-ONE-SOURCE (owner order 2026-10-03): the models are named once, in
    # env.example; a lane export may only repeat the name (see gate_lane.sh).
    declared = declared_models()
    for name, key in (
        ("SKYKEEP_GATE_EMBED_MODEL", "SKYKEEP_EMBED_MODEL"),
        ("SKYKEEP_GATE_THINK_MODEL", "SKYKEEP_THINK_MODEL"),
    ):
        if not declared.get(key):
            raise Refused(f"env.example declares no {key} (MODEL-ONE-SOURCE)")
        got = (env.get(name) or "").strip()
        if got and got != declared[key]:
            raise Refused(
                f"{name}={got}, but env.example declares {declared[key]}: one"
                " model per role, named once (MODEL-ONE-SOURCE)"
            )

    # The ingest role is optional (ADR-0031), so EMPTY is a legitimate value —
    # but ABSENT is not. Requiring it to be exported, even empty, is what keeps
    # "this lane runs without an ingest model" a decision on the record rather
    # than a forgotten export, which is how a speed-up once supplied the very
    # absence Gate MO exists to test for.
    if "SKYKEEP_GATE_INGEST_MODEL" not in env:
        raise Refused(
            "SKYKEEP_GATE_INGEST_MODEL is not set — export it EMPTY to run "
            "without an ingest model, so the choice is on the record"
        )

    # The endpoint's shape (see gate_lane.sh, THE ENDPOINT'S SHAPE TRAVELS WITH
    # THE ENDPOINT). Both are optional there; a value the lane would refuse at
    # its barrier is refused here, before any lane starts.
    for name, shape, said in LANE_ENDPOINT_SHAPE_SETTINGS:
        got = env.get(name) or ""
        if got and not re.fullmatch(shape, got):
            raise Refused(f"{name}={got!r} is not {said}")

    state = (env.get("SKYKEEP_LANE_STATE_DIR") or "").strip()
    if not state:
        raise Refused(
            "SKYKEEP_LANE_STATE_DIR is not set — a lane will not invent a place "
            "to write key material and logs"
        )
    if not Path(state).is_dir():
        raise Refused(f"SKYKEEP_LANE_STATE_DIR {state} is not a directory")

    # Optional, with its default in `gate_lane.sh` (which reads it with `:-`, so
    # EMPTY means the default there too); but a value the lane would refuse at
    # its barrier is refused here, before any lane starts.
    age = env.get(MAX_VAULT_AGE_SETTING) or ""
    if age and not re.fullmatch(r"[0-9]+", age):
        raise Refused(
            f"{MAX_VAULT_AGE_SETTING} must be a whole number of hours, got {age!r}"
        )

    return {
        "url": url,
        "secret_file": secret,
        "embed": declared["SKYKEEP_EMBED_MODEL"],
        "think": declared["SKYKEEP_THINK_MODEL"],
        "ingest": env["SKYKEEP_GATE_INGEST_MODEL"],
        "state_dir": state,
    }


#: The optional lane settings that say what SHAPE the model endpoint is, each
#: with the one-word pattern `gate_lane.sh` holds it to and the words for a
#: refusal. One word, because the lane puts each into a word-split list.
LANE_ENDPOINT_SHAPE_SETTINGS = (
    (
        "SKYKEEP_GATE_MODEL_PROVIDER",
        r"[a-z0-9-]+",
        "a provider name: lower-case letters, digits and hyphens only",
    ),
    (
        "SKYKEEP_GATE_OPENAI_THINK_FIELD",
        r"[a-z0-9_]+",
        "a field name: lower-case letters, digits and underscores only",
    ),
    (
        "SKYKEEP_GATE_MODEL_THINK",
        r"true|false",
        "true or false",
    ),
)


def declared_models(template: Path | None = None) -> dict[str, str]:
    """The model names env.example declares (MODEL-ONE-SOURCE), last
    assignment wins, exactly as `gate_lane.sh`'s `sed` reads them."""
    path = template or Path(__file__).resolve().parents[1] / "env.example"
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        for key in ("SKYKEEP_EMBED_MODEL", "SKYKEEP_THINK_MODEL"):
            if line.startswith(f"{key}="):
                found[key] = line[len(key) + 1 :]
    return found


def default_lanes(per_lane_bytes: int) -> tuple[int, str]:
    """Lane count from MemAvailable, with the sentence that explains it.

    `per_lane_bytes` is what one WHOLE lane holds, scope and containers: by
    default `MEASURED_LANE_MEMORY`. The scope's `MemoryMax` is the same number
    and covers only the scope, so this division is what keeps the containers
    of N lanes inside the machine; no cap does.
    """
    avail = mem_available_bytes()
    if avail is None:
        return 1, "cannot read MemAvailable — running ONE lane rather than guessing"
    usable = avail - HOST_RESERVE_BYTES
    n = int(usable // per_lane_bytes)
    clamped = max(1, min(MAX_AUTO_LANES, n))
    why = (
        f"MemAvailable {avail / 1024**3:.1f} GiB - {HOST_RESERVE_BYTES / 1024**3:.0f} GiB "
        f"reserve / {per_lane_bytes / 1024**3:.1f} GiB per lane = {n}, clamped to {clamped}"
    )
    if n < 1:
        raise Refused(
            why + " — this box cannot afford one lane; running Compose projects: "
            + memory_blockers()
        )
    return clamped, why


def worker_shares(lanes: list[int], budget: int) -> dict[int, int]:
    """Give each live lane a worker, then distribute remaining capacity evenly."""
    if not lanes or budget < len(lanes):
        raise Refused("total worker budget must cover every active lane")
    whole, extra = divmod(budget, len(lanes))
    return {lane: whole + (index < extra) for index, lane in enumerate(lanes)}


def worker_memory_bytes(env: dict[str, str] | None = None) -> int:
    """What one extra xdist worker holds: `SKYKEEP_GATE_WORKER_MEMORY` or the measured default."""
    env = os.environ if env is None else env
    raw = env.get(WORKER_MEMORY_SETTING, "") or MEASURED_WORKER_MEMORY
    return parse_memory(raw, WORKER_MEMORY_SETTING)


def machine_cores() -> int | None:
    """The cores this process may run on."""
    return len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()


def reserved_cpus(cores: int, env: dict[str, str] | None = None) -> int:
    """`SKYKEEP_GATE_RESERVED_CPUS`, or a quarter of the cores (at least one)."""
    env = os.environ if env is None else env
    raw_reserve = env.get("SKYKEEP_GATE_RESERVED_CPUS", "")
    if raw_reserve:
        if not raw_reserve.isdecimal():
            raise Refused("SKYKEEP_GATE_RESERVED_CPUS must be a whole number")
        return int(raw_reserve)
    return max(1, cores // 4)


def worker_budget(
    available: int, usable_cores: int, n_lanes: int, per_worker_bytes: int, lane_bytes: int = 0
) -> int:
    """`wave_capacity`'s rule for one box's figures: the workers `n_lanes`
    lanes may have, from MemAvailable after the reserve and the lanes, and
    from the usable cores, the smaller winning. Refuses when the lanes alone
    do not fit, naming the MemAvailable they need."""
    needed = HOST_RESERVE_BYTES + n_lanes * lane_bytes
    spare = available - needed
    if spare < 0:
        raise Refused(
            f"MemAvailable {available / 1024**3:.1f} GiB cannot hold {n_lanes} lane(s): "
            f"needs {needed / 1024**3:.1f} GiB ({HOST_RESERVE_BYTES / 1024**3:.0f} GiB reserve + "
            f"{n_lanes} x {lane_bytes / 1024**3:.1f} GiB per lane), so no lane could have a worker"
        )
    memory_budget = n_lanes + spare // per_worker_bytes
    return min(usable_cores, memory_budget)


def wave_capacity(
    lanes: list[int], per_worker_bytes: int, lane_bytes: int = 0
) -> tuple[dict[int, int], dict[int, int]]:
    """Bound workers by available cores and measured memory, reserving host capacity.

    THE RULE: the memory budget is what MemAvailable affords AFTER the lanes
    themselves are paid for, divided by ONE WORKER's figure, never by a lane's.

        usable  = MemAvailable - HOST_RESERVE_BYTES - len(lanes) * lane_bytes
        workers = len(lanes) + usable // per_worker_bytes

    `lane_bytes` is what one whole lane holds (scope and containers), and a lane
    already carries its one pytest process, which is the `len(lanes)` term. A
    wave whose lanes alone do not fit (`usable < 0`) cannot give every lane even
    that one worker, so it is refused, naming the MemAvailable it needs:
    `HOST_RESERVE_BYTES + len(lanes) * lane_bytes`. `lane_bytes` of 0 means the
    lanes' own footprint is not accounted for: every lane still gets its one
    worker and all spare memory is divided by the per-worker figure. A memory bound is not a core-count cap: the
    cores bound it separately, below, and the smaller of the two wins.
    """
    cores = machine_cores()
    if not cores:
        raise Refused("CPU count is unknown; set machine affinity before starting a wave")
    reserve = reserved_cpus(cores)
    usable_cores = cores - reserve
    if usable_cores < len(lanes):
        raise Refused("CPU reservation leaves fewer cores than active lanes")
    available = mem_available_bytes()
    if available is None:
        raise Refused("MemAvailable is unknown; cannot derive total worker budget")
    derived = worker_budget(available, usable_cores, len(lanes), per_worker_bytes, lane_bytes)
    raw_budget = os.environ.get("SKYKEEP_GATE_TOTAL_WORKERS", "")
    if raw_budget:
        if not raw_budget.isdecimal() or int(raw_budget) < 1:
            raise Refused("SKYKEEP_GATE_TOTAL_WORKERS must be a positive whole number")
        budget = int(raw_budget)
        if budget > derived:
            raise Refused(f"total worker budget {budget} exceeds memory/CPU capacity {derived}")
    else:
        budget = derived
    return worker_shares(lanes, budget), worker_shares(lanes, usable_cores)


def discover_suites() -> list[str]:
    """Every gate suite, as repo-relative paths."""
    return sorted(
        str(Path(p).relative_to(ROOT))
        for p in glob.glob(str(ROOT / "tests" / "gates" / "test_gate_*.py"))
    )


#: What a lane's `LANE_EXTRA` must carry for it to start a clamd daemon
#: (`gate_lane.sh` reads the replica count from there and from nowhere else).
CLAMD_LANE_EXTRA = "SKYKEEP_CLAMD_REPLICAS=1"


def discover_clamd_suites() -> list[str]:
    """The suites that measure the REAL malware engine, as repo-relative paths.

    They live in `tests/gates/clamd_lane/` and are deliberately not in
    `discover_suites`: a standard lane holds clamd at zero replicas, and there
    every case errors. A full run carries them in the clamd wave instead, on a
    lane prepared with `CLAMD_LANE_EXTRA`. Nothing in the environment removes
    them from a full run: the scan gates cannot be disabled by configuration,
    and a measurement that could be would leave a hollow scanner unnoticed.
    """
    return sorted(
        str(Path(p).relative_to(ROOT))
        for p in glob.glob(str(ROOT / "tests" / "gates" / "clamd_lane" / "test_gate_*.py"))
    )


def clamd_lane_extra(inherited: str | None) -> str:
    """`LANE_EXTRA` for a clamd lane: what the caller exported, with any
    replica count of its own replaced by the one that starts the daemon."""
    kept = [kv for kv in (inherited or "").split() if not kv.startswith("SKYKEEP_CLAMD_REPLICAS=")]
    return " ".join([*kept, CLAMD_LANE_EXTRA])


def discover_extras() -> list[str]:
    """The parts of a full run that are not gate suites.

    `tests/bdd` is the behaviour lane, and `tests/gates/test_*.py` that is NOT
    `test_gate_*` is a harness test that happens to live there (migration
    ordering, TLS SANs, publish binds). Both belong in a full run and neither
    belongs in the longest-first table, so they are appended to whichever lane
    is least loaded once the gate suites are placed.
    """
    extras = [
        str(Path(p).relative_to(ROOT))
        for p in glob.glob(str(ROOT / "tests" / "gates" / "test_*.py"))
        if not Path(p).name.startswith("test_gate_")
    ]
    if (ROOT / "tests" / "bdd").is_dir():
        extras.append("tests/bdd")
    return sorted(extras)


def write_lane_declaration(decl_dir: Path, lane: int, *, pid: int | None = None) -> Path:
    """Write `pid=` / `exe=` / `lane=<N>` for a lane this process is running.

    `bool` is refused even though it is an `int` subclass. The caller deletes
    the file when the lane process exits, so a finished lane does not stay
    claimed. Raises `ValueError` for a lane or pid that is not a real integer;
    `OSError` if the directory cannot be created.
    """
    decl_dir = Path(decl_dir)
    if not decl_dir.is_absolute():
        raise ValueError(f"declaration directory must be an absolute path: {decl_dir}")
    if isinstance(lane, bool) or not isinstance(lane, int) or lane < 0:
        raise ValueError(f"lane must be a non-negative integer, got {lane!r}")
    if pid is None:
        pid = os.getpid()
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError(f"pid must be a positive integer, got {pid!r}")
    exe = ""
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        exe = ""
    decl_dir.mkdir(parents=True, exist_ok=True)
    path = decl_dir / f"lane-{lane}-pid-{pid}.decl"
    lines = [f"pid={pid}"]
    if exe:
        lines.append(f"exe={exe}")
    lines.append(f"lane={lane}")
    path.write_text("\n".join(lines) + "\n")
    return path


def session_decl_dir() -> Path | None:
    """Where lane declarations go: SKYKEEP_SESSION_DECL_DIR, or None (ADR-0002).

    No fallback under HOME: a unit test that drives the runner must not claim a
    lane in the live loop's directory. None means no declaration is written,
    which readers treat as unknown.
    """
    raw = os.environ.get("SKYKEEP_SESSION_DECL_DIR")
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned or not Path(cleaned).is_absolute():
        raise Refused("SKYKEEP_SESSION_DECL_DIR must be a nonempty absolute path")
    return Path(cleaned)


def lane_command(
    lane: int,
    label: str,
    suites: list[str],
    memory: str,
    workers: int | None = None,
    serial: set[str] | None = None,
    prepare_only: bool = False,
    alone: bool = False,
    teardown: bool = False,
    memory_high: str | None = None,
    run_id: str | None = None,
    *,
    maxprocesses: int | None = None,
    cpu_quota: str | None = None,
    lane_extra: str | None = None,
) -> list[str]:
    """One lane, inside its own memory-capped systemd user scope.

    With `lane_extra`, the lane is handed that `LANE_EXTRA` in place of the one
    this process inherited: the clamd wave uses it to start the daemon on one
    lane, for its prepare pass and its suite pass and for no other.

    With `memory_high`, the scope also carries `MemoryHigh`, the soft budget
    under `MemoryMax`: past it the kernel throttles the lane instead of
    killing it. It throttles on the lane's own usage, not on what the box has
    left, so it does not replace the lane-count formula.

    `MemorySwapMax=0` matters as much as `MemoryMax`: a lane allowed to swap
    does not get killed, it makes the whole box unusable instead. `choom -n 900`
    is the other half — when something must die, the kernel should pick a
    disposable lane and not the editor holding the session.

    With `workers`, the lane runs its parallel-safe suites under
    `-n <workers> --dist loadfile`. The SERIAL ones are named to the lane in
    `LANE_SERIAL` and run whole: `env` carries them rather than the argument
    list, so the suite paths stay exactly what the lane already understood and
    an older lane script cannot mistake a rule for a suite.

    With `prepare_only`, no suite is passed at all and the lane stops once its
    stack is up — that is the barrier.

    A suite pass (neither `prepare_only` nor `teardown`) carries
    `LANE_AFTER_BARRIER`: the barrier pass before it, same label, is where a
    lane's vault was built or reset, and the suite pass keeps that pass's log
    so its `fresh=` reads the vault's age and not just the last pass's view.

    With `alone`, the lane refuses to start while any other gate lane is
    running (`LANE_ALONE`); with `teardown`, it brings itself down with its
    volumes and runs nothing (`LANE_TEARDOWN`, no suites).
    """
    assignments = []
    if run_id and not teardown:
        assignments.append(f"LANE_RUN_ID={run_id}")
    if maxprocesses and not teardown:
        assignments.append(f"LANE_MAXPROCESSES={maxprocesses}")
    if workers and not teardown:
        assignments += [
            f"LANE_XDIST={workers}",
            "LANE_SERIAL=" + " ".join(sorted(serial or ())),
        ]
    if not prepare_only and not teardown:
        # A suite pass always follows a barrier pass of this same run and label
        # (the barrier wave, or the alone phase's own prepare wave): the lane
        # keeps that pass's log and carries its `fresh=` forward.
        assignments.append("LANE_AFTER_BARRIER=1")
    if alone:
        assignments.append("LANE_ALONE=1")
    if teardown:
        assignments.append("LANE_TEARDOWN=1")
    if lane_extra and not teardown:
        assignments.append(f"LANE_EXTRA={lane_extra}")
    env = ["env", *assignments] if assignments else []
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "-p",
        f"MemoryMax={memory}",
        *(["-p", f"CPUQuota={cpu_quota}"] if cpu_quota else []),
        *(["-p", f"MemoryHigh={memory_high}"] if memory_high else []),
        "-p",
        "MemorySwapMax=0",
        "--",
        "choom",
        "-n",
        "900",
        "--",
        *env,
        "bash",
        str(LANE_SCRIPT),
        str(lane),
        label,
        *([] if prepare_only or teardown else suites),
    ]


def complete_lane_run(state: Path, lane: int, run_id: str, log: Path, rc: int) -> bool:
    """Finish this run's marker only after the suite pass reached its exit line.

    A killed runner never calls this. A killed suite, a guard refusal, or a
    mismatched marker leaves the lane unsafe to reuse on the next barrier.
    """
    if rc not in (0, 1):
        return False
    try:
        if not any(line.startswith(f"LANE skykeep-p{lane} EXIT ") for line in log.read_text().splitlines()):
            return False
        path = state / f"lane-p{lane}-run.txt"
        current, started, status = path.read_text().strip().split("|")
        if current != run_id or status != "active" or not started:
            return False
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(f"{current}|{started}|complete\n")
        temporary.replace(path)
        return True
    except (OSError, ValueError):
        return False


def suite_module(suite: str) -> str:
    """The dotted classname prefix a suite's cases carry in a junit xml.

    A file suite is its module (`tests/gates/test_gate_x.py` is
    `tests.gates.test_gate_x`); a directory suite is the directory's own dotted
    name (`tests/bdd` is `tests.bdd`), under which every file's cases sit as
    `tests.bdd.<file>`. Derived from what the path IS, never by cutting a fixed
    number of characters: the old 3-character cut turned `tests/bdd` into
    `tests.` and no case ever matched it.
    """
    path = suite.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    path = path.rstrip("/").removesuffix(".py")
    return path.replace("/", ".")


def junit_summary(xml_path: Path, suites: list[str], batch_rc: int) -> list[str]:
    """One `SUITE <path> rc=<n> <summary>` line per suite, from a BATCH run.

    Under `-n` a lane runs all its parallel-safe suites in ONE pytest process —
    it has to, because `--dist loadfile` spreads FILES across workers and a
    process given one file leaves the other workers idle. That single process
    has one exit status for many suites, and the lane log's whole value is that
    it is per-suite: `gate_distribute` harvests durations from it and
    `summarise()` names the suites that failed. So the per-suite verdict is
    recovered from the JUnit XML instead of being thrown away.

    A suite that contributed NO test case at all is reported UNMEASURED, never
    passed. That is the case where the batch died partway — an OOM, a worker
    crash, `timeout(1)` — and the suites it never reached are exactly the ones
    a two-outcome report would silently call green.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError):
        # No XML at all: the batch died before writing one. Nothing in it was
        # measured, and the batch's own rc is the most honest thing to report.
        rc = batch_rc if batch_rc else TIMEOUT_RC
        return [f"SUITE {s} rc={rc} batch produced no junit xml" for s in suites]

    per_suite: dict[str, dict[str, float | int]] = {
        s: {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "time": 0.0}
        for s in suites
    }
    # The environment's signature on an errored case, per suite: a batch has
    # no per-suite output for `suite_outcome` to search, so the SUITE line
    # names it instead.
    signature_of: dict[str, str] = {}
    # Most specific module first, so a directory suite beside a suite nested
    # inside it keeps the nested one's cases. A case belongs to a module when
    # its classname IS the module or continues it after a dot, so `test_gate_x`
    # never swallows `test_gate_xy`.
    module_of = dict(
        sorted(((s, suite_module(s)) for s in suites), key=lambda kv: -len(kv[1]))
    )

    for case in root.iter("testcase"):
        classname = case.get("classname") or ""
        home = None
        for suite, module in module_of.items():
            if classname == module or classname.startswith(module + "."):
                home = suite
                break
        if home is None:
            continue
        acc = per_suite[home]
        acc["time"] += float(case.get("time") or 0.0)
        # A <failure> is a verdict; an <error> is a case that never reached its
        # assertion. Counted apart, in pytest's own words, so `suite_outcome`
        # reads a batched suite exactly as it reads a serial one.
        if case.find("failure") is not None:
            acc["failed"] += 1
        elif (error := case.find("error")) is not None:
            acc["errors"] += 1
            said = f"{error.get('message') or ''} {error.text or ''}"
            for signature in INFRASTRUCTURE_SIGNATURES:
                if signature in said:
                    signature_of.setdefault(home, signature)
                    break
        elif case.find("skipped") is not None:
            acc["skipped"] += 1
        else:
            acc["passed"] += 1

    lines = []
    for suite in suites:
        acc = per_suite[suite]
        seen = acc["passed"] + acc["failed"] + acc["errors"] + acc["skipped"]
        if not seen:
            rc = batch_rc if batch_rc else TIMEOUT_RC
            lines.append(
                f"SUITE {suite} rc={rc} no test case reached the junit xml — "
                "the batch did not run it"
            )
            continue
        rc = 1 if acc["failed"] or acc["errors"] else 0
        parts = []
        if acc["failed"]:
            parts.append(f"{acc['failed']} failed")
        if acc["passed"]:
            parts.append(f"{acc['passed']} passed")
        if acc["errors"]:
            parts.append(f"{acc['errors']} error{'' if acc['errors'] == 1 else 's'}")
        if acc["skipped"]:
            parts.append(f"{acc['skipped']} skipped")
        seen_said = f" — {signature_of[suite]}" if suite in signature_of else ""
        lines.append(
            f"SUITE {suite} rc={rc} {', '.join(parts)} in {acc['time']:.2f}s{seen_said}"
        )
    return lines


def vault_probe_sql() -> str:
    """The read-only query `gate_lane.sh` runs inside a lane's own vecstore.

    It asks the VAULT, not the containers, the three questions that decide
    whether a running lane may be reused, and prints the answers as the one
    line `VAULT_FACTS` reads:

      * the restore marker — a database-level GUC, so it survives the restore
        replacing the schema; a webserver started over it refuses to serve;
      * the demonstration installation — the six accounts `demo_setup` creates
        under deterministic ids, which no other suite creates;
      * the vault's age — its first migration's `applied_at`.

    Built from the PRODUCT's constants, never from copies: a renamed marker or
    a changed demonstration namespace must not leave the lane probing for
    something that no longer exists and calling the vault clean. The session
    is READ ONLY, because the probe runs before anything decides whether this
    vault is kept.
    """
    from skykeep.db.restore_state import RESTORE_MARKER_GUC
    from skykeep.vaultmode import demo_principal_ids

    # Both land inside SQL literals, so both are checked for shape rather than
    # trusted: the GUC is a dotted identifier and the ids are UUIDs.
    if not re.fullmatch(r"[a-z_]+\.[a-z_]+", RESTORE_MARKER_GUC):
        raise ValueError(f"unexpected restore marker name {RESTORE_MARKER_GUC!r}")
    ids = sorted(str(pid) for pid in demo_principal_ids())
    if not ids:
        raise ValueError("the product names no demonstration accounts to probe for")
    id_list = ", ".join(f"'{pid}'::uuid" for pid in ids)
    return (
        "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;\n"
        "SELECT CASE WHEN coalesce(btrim(current_setting("
        f"'{RESTORE_MARKER_GUC}', true)), '') <> '' THEN 't' ELSE 'f' END\n"
        "    || '|' || (SELECT count(*) FROM principals\n"
        f"               WHERE principal_id IN ({id_list}))\n"
        "    || '|' || coalesce((SELECT floor(extract(epoch FROM now() - min(applied_at)))"
        "::bigint\n"
        "                        FROM schema_migrations), -1);\n"
    )


def vault_verdict(output: str, max_age_hours: int | None) -> list[str]:
    """Why a running lane's vault must not be handed to a suite; `[]` if it may.

    FAIL CLOSED. Anything but the one line the probe prints — psql failing, a
    vecstore still starting, a vault with no `principals` table because a
    restore died halfway — is itself a reason: a vault this lane cannot read is
    a vault it cannot vouch for, and the lane is disposable.

    `max_age_hours` is None on a SUITE pass: the barrier judged the age moments
    earlier, and judging it again could refuse a vault that merely crossed the
    bound between the two passes.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    facts = VAULT_FACTS.match(lines[-1]) if lines else None
    if facts is None:
        seen = lines[-1][:80] if lines else ""
        return [f"its vault could not be read (probe answered {seen!r})"]

    marker, demo, age = facts.group(1) == "t", int(facts.group(2)), int(facts.group(3))
    reasons = []
    if marker:
        reasons.append(
            "its vault holds the restore marker, so a webserver started over it "
            "refuses to serve"
        )
    if demo:
        reasons.append(
            f"its vault holds Gate DX's demonstration installation ({demo} of the "
            "demonstration accounts)"
        )
    if age < 0:
        reasons.append("its vault has an empty migration ledger, so its age is unknown")
    elif max_age_hours is not None and age > max_age_hours * 3600:
        reasons.append(
            f"its vault is {age // 3600}h old, past "
            f"{MAX_VAULT_AGE_SETTING}={max_age_hours}"
        )
    return reasons


def bringup_fetch_failure(output: str) -> str | None:
    """The line of a failed `compose up`'s output that shows a FETCH failure.

    None when no line does, and then the failure is the tree's or the
    product's own — a build error, a container that came up unhealthy — and
    `gate_lane.sh` refuses without another attempt. Fail closed in that
    direction: an output this cannot read is not a fetch failure.

    The line is build output, so it is untrusted: control characters are
    dropped, anything shaped like a credential in a URL is not repeated, and
    only its first `FETCH_EVIDENCE_CHARS` characters come back.
    """
    for line in output.splitlines():
        if FETCH_FAILURE.search(line) and not FETCH_WILL_BE_RETRIED.search(line):
            seen = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
            seen = "".join(ch for ch in seen if ch.isprintable()).strip()
            seen = re.sub(r"://[^/\s@]+@", "://<redacted>@", seen)
            return seen[:FETCH_EVIDENCE_CHARS]
    return None


def bringup_refusal(log: Path) -> str:
    """What a lane's log says about a bring-up it refused, as one clause.

    Empty when the lane refused for another reason (its vault, the memory
    floor, the project name) or left no log: the runner then claims neither a
    fetch failure nor a build error. The two must not read alike — one says
    nothing about the tree and is answered by running the gate again, the
    other is the tree and is answered by reading the log.
    """
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return ""
    found = [m for m in map(BRINGUP_LINE.match, text.splitlines()) if m]
    if not found:
        return ""
    kind, attempt, allowed = found[-1].group(1), found[-1].group(2), found[-1].group(3)
    if kind == "fetch":
        return (
            f"a transient fetch failure on all {attempt} attempt(s) — an index, "
            "mirror or registry did not answer, the image was never built, and "
            "this is not a verdict on the tree: run the gate again"
        )
    if kind == "build":
        return (
            f"a build or start error on attempt {attempt} of {allowed}, not a "
            "fetch failure, so it was not retried — running the gate again "
            "will not change it: read the log"
        )
    return (
        f"a failure the lane could not judge on attempt {attempt} of {allowed}, "
        "so it was not retried: read the log"
    )


def read_lane_log(path: Path) -> list[tuple[str, int, str]]:
    """`(suite, rc, summary)` per `SUITE` line, in the order they were run."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        m = SUITE_LINE.match(line)
        if m:
            out.append((m.group(1), int(m.group(2)), m.group(3).strip()))
    return out


def read_lane_tree(path: Path) -> tuple[str, str] | None:
    """`(start, collect)` of a lane log's `TREE` line, or None when it has none.

    A lane killed before its last suite never writes one, and that is no claim
    the tree held still: None is "not recorded", which `tree_moved` does not read
    as a move, because the lane's own exit status already says it did not finish.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    found = None
    for line in text.splitlines():
        m = TREE_LINE.match(line)
        if m:
            found = (m.group(1), m.group(2))
    return found


def tree_moved(tree: tuple[str, str] | None) -> bool:
    """True when the HEAD or the dirt count of a lane's tree differs between its
    start and its collection; a pass on such a tree covers no tree anyone can name."""
    return tree is not None and tree[0] != tree[1]


def benchmark_floor_lines() -> list[str]:
    """What the summary prints about the benchmark floor.

    Owner decision 5: reported, never a stop. A missing floor, a missing
    scorecard and a revision mismatch come back as lines; `summarise`
    has already decided `green`.
    """
    scripts = Path(__file__).resolve().parent
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from bench.scorecard import floor_report

    root = scripts.parent
    # `floor_report` turns a missing file, a missing scorecard and a
    # revision mismatch into lines. It does not raise those.
    return floor_report(
        floor_path=root / "tests" / "benchmark" / "floor.yaml",
        plans_path=root / "tests" / "benchmark" / "plans.yaml",
        scorecards=root / "docs" / "planning" / "bettersearch",
    )


#: What pytest's last line counts, as the lane copies it onto a SUITE line and
#: `junit_summary` writes it for a batch: `2 failed, 40 passed, 1 error in 9.1s`.
OUTCOME_COUNT = re.compile(r"\b(\d+) (passed|failed|skipped|errors?)\b")

#: pytest's exit status for "every case ran and at least one did not pass" —
#: the only non-zero status under which any case gave a verdict.
CASES_DID_NOT_ALL_PASS_RC = 1

#: pytest's exit status for "interrupted", which is what a collection error
#: ends with: a test module that does not import is the tree's own red.
COLLECTION_ERROR_RC = 2

#: Text in a suite's output that says the environment, not the tree, stopped
#: it. Only these make an errors-only suite UNMEASURED: the endpoint not
#: answering in time, the vision model not reachable, the lane posture guard
#: refusing the invocation, the endpoint's name not resolving.
INFRASTRUCTURE_SIGNATURES = (
    "ReadTimeout",
    "VisionUnreachable",
    "posture guard REFUSED",
    # The resolver's own words, as a lane's httpx client reports them from a
    # fixture: Batch34's em, reingest_run, hv and ux were setup errors on it.
    "Temporary failure in name resolution",
)

#: Text that says the tree's own conftest is broken (pytest's own wording).
TREE_CONFTEST_BROKEN = "while loading conftest"

#: How much of a suite's output is searched for a signature: its end, where
#: pytest writes the errors and the exit reason.
SUITE_OUTPUT_TAIL_BYTES = 256 * 1024


def suite_output(log: str, suite: str) -> str:
    """The tail of what `gate_lane.sh`'s `run_one` captured for one suite.

    The lane writes `<log without .log>-<suite stem>.out` beside its log. A
    suite of a remote box, or of a batch pass, has none here, and this is "".
    The text is only searched for a signature, never printed: it is the
    product's output and may carry document content.
    """
    path = Path(log)
    if path.suffix != ".log":
        # A remote box's result names its status ref, not a log on this box.
        return ""
    out = path.with_name(f"{path.stem}-{Path(suite).stem}.out")
    try:
        with out.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - SUITE_OUTPUT_TAIL_BYTES))
            return fh.read().decode(errors="replace")
    except OSError:
        return ""


def _outcome_counts(summary: str) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    for number, word in OUTCOME_COUNT.findall(summary):
        counts["error" if word.startswith("error") else word] += int(number)
    return counts


def suite_outcome(rc: int, summary: str, output: str = "") -> tuple[str, str]:
    """`("passed" | "skipped" | "failed" | "unmeasured", why)` for one suite.

    `output` is the suite's own captured output when this box has it
    (`suite_output`), else "". FAILED is the tree's own red, UNMEASURED the
    environment's, and the word decides where triage looks first:

      * rc 1 with a failure counted: a case failed. FAILED.
      * rc 1 whose cases only errored: UNMEASURED only when the summary or the
        output carries an `INFRASTRUCTURE_SIGNATURES` entry — that is Gate HW
        with the vision model evicted, `1 passed, 21 errors`. Without one it
        is FAILED: a product failure inside a fixture errors exactly so.
      * rc 1 whose summary cannot be read: FAILED, the louder word.
      * rc 2, a collection error: FAILED. A module that does not import is
        the tree.
      * rc 4: FAILED when the output says a conftest did not load; otherwise
        UNMEASURED, the posture guard refusing before collection.
      * rc 75: UNMEASURED, the posture guard could not ask the model runtime
        at all (`COULD_NOT_ASK_RC`).
      * 124 (`timeout(1)`), 3 internal error, 5 no case collected, 137/143
        killed, and any other status pytest does not end a run with:
        UNMEASURED.
      * rc 0 that counts skips and no pass: SKIPPED. Nothing was asserted —
        Gate HW with the handwriting gate set to skip is `22 skipped` — so it
        is not counted as a verdict, and not as a failure either.

    Neither FAILED nor UNMEASURED is a pass, and both make the run red; the
    difference is what a reader does next, and whether a count of suites
    measured includes this one.
    """
    if rc == 0:
        counts = _outcome_counts(summary)
        if counts["skipped"] and not counts["passed"]:
            return "skipped", f"{counts['skipped']} skipped, none passed"
        return "passed", ""
    if rc == TIMEOUT_RC:
        return "unmeasured", "timeout"
    if rc == COLLECTION_ERROR_RC:
        return "failed", "collection error or interrupted — the tree's own red"
    if rc == USAGE_ERROR_RC:
        if TREE_CONFTEST_BROKEN in output:
            return "failed", "a conftest did not load — the tree's own red"
        return "unmeasured", "refused before collection — no test ran"
    if rc == COULD_NOT_ASK_RC:
        return "unmeasured", "the model runtime could not be asked — no test ran"
    if rc != CASES_DID_NOT_ALL_PASS_RC:
        return "unmeasured", "no case gave a verdict"
    counts = _outcome_counts(summary)
    if counts["error"] and not counts["failed"]:
        text = f"{summary}\n{output}"
        signature = next((s for s in INFRASTRUCTURE_SIGNATURES if s in text), None)
        if signature is not None:
            return "unmeasured", (
                f"{counts['error']} errored before any assertion, none failed ({signature})"
            )
        return "failed", (
            f"{counts['error']} errored, none failed, and no infrastructure signature"
            " — the tree's own red"
        )
    return "failed", ""


def own_cgroup(text: str) -> str | None:
    """The cgroup-v2 path in a `/proc/<pid>/cgroup` text, or None.

    None for a box with no unified hierarchy: a lane that cannot say which
    cgroup is its own can attribute no kill to itself.
    """
    for line in text.splitlines():
        if line.startswith("0::"):
            path = line[3:].strip()
            return path if path.startswith("/") else None
    return None


def memory_event_counts(text: str) -> dict[str, int]:
    """`memory.events` as `{name: count}`."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def _own_memory_events() -> tuple[str | None, dict[str, int] | None]:
    """This process's cgroup, and its `oom` / `oom_kill` counts if readable."""
    try:
        cgroup = own_cgroup(PROC_SELF_CGROUP.read_text())
    except OSError:
        return None, None
    if cgroup is None:
        return None, None
    try:
        counts = memory_event_counts(
            (CGROUP_ROOT / cgroup.lstrip("/") / "memory.events").read_text()
        )
    except OSError:
        return cgroup, None
    if "oom" not in counts or "oom_kill" not in counts:
        return cgroup, None
    return cgroup, {"oom": counts["oom"], "oom_kill": counts["oom_kill"]}


def oom_snapshot(now: float | None = None) -> str:
    """What a lane records before each suite: when, its cgroup, its counters.

    The scope `lane_command` launches a lane in is removed the moment the lane
    exits, and its `memory.events` with it — so the counters are read by the
    lane, about itself, around each suite, never by this runner afterwards.
    One shell-safe line; `-` marks a cgroup the lane could not find.
    """
    cgroup, counts = _own_memory_events()
    parts = [f"since={int(time.time() if now is None else now)}", f"cgroup={cgroup or '-'}"]
    if counts is not None:
        parts += [f"oom={counts['oom']}", f"oom_kill={counts['oom_kill']}"]
    return " ".join(parts)


def _parse_snapshot(text: str) -> tuple[int | None, str | None, dict[str, int] | None]:
    fields = {}
    for token in text.split():
        name, _eq, value = token.partition("=")
        fields[name] = value
    since = int(fields["since"]) if fields.get("since", "").isdigit() else None
    cgroup = fields.get("cgroup") or "-"
    counts = None
    if fields.get("oom", "").isdigit() and fields.get("oom_kill", "").isdigit():
        counts = {"oom": int(fields["oom"]), "oom_kill": int(fields["oom_kill"])}
    return since, (None if cgroup == "-" else cgroup), counts


def read_kernel_journal(since: int) -> str:
    """The kernel's messages since `since` (epoch seconds); "" if unreadable.

    Unreadable is not a refusal: it is the absence of one witness, and a kill
    nothing witnesses is never called an eviction.
    """
    try:
        proc = subprocess.run(
            [
                "journalctl",
                "--dmesg",
                "--quiet",
                "--no-pager",
                "--output=cat",
                f"--since=@{since}",
            ],
            capture_output=True,
            text=True,
            timeout=JOURNAL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def journal_kill(journal: str, cgroup: str) -> tuple[str, str] | None:
    """`(killer, evidence)` from the kernel's `oom-kill:` line whose victim
    ran in `cgroup`, or None.

    The line is the kernel's one-line kill summary: `oom_memcg=<path>` when a
    cgroup's limit was reached, `global_oom` when the box ran out, and
    `task_memcg=<path>` for the victim either way. Only a line naming THIS
    cgroup as the victim's counts — a kill elsewhere on the box in the same
    minute (the editor, a neighbouring lane) explains nothing about this suite.
    """
    for line in journal.splitlines():
        at = line.find("oom-kill:")
        if at < 0:
            continue
        body = line[at + len("oom-kill:") :]
        victim = re.search(r"(?:^|,)task_memcg=([^,]*)", body)
        if not victim or victim.group(1).strip() != cgroup:
            continue
        limit = re.search(r"(?:^|,)oom_memcg=([^,]*)", body)
        if limit and limit.group(1).strip() == cgroup:
            return "cgroup", "the kernel journal's oom-kill line: oom_memcg is the lane's own scope"
        reason = f"oom_memcg={limit.group(1).strip()}" if limit else "global_oom"
        return "host", (
            f"the kernel journal's oom-kill line names the lane's scope as the victim's, {reason}"
        )
    return None


def oom_evidence(snapshot: str, journal=None) -> str:
    """The token `gate_lane.sh` heads a SIGKILLed suite's line with.

    Evidence, never inference, read against the snapshot taken before the
    suite — a count left by an earlier suite on the same lane is not this
    one's:

      * the scope's `oom_kill` rose AND its `oom` rose: the scope reached its
        own `MemoryMax` — `cgroup`;
      * `oom_kill` rose with no `oom` of the scope's own: the kernel counts
        `oom_kill` on a victim's cgroup whichever killer chose it, and `oom`
        only when that cgroup hit its own limit, so the kill came from outside
        it — `host`;
      * no `oom_kill` on the scope, but the kernel journal's `oom-kill:` line
        names the scope as the victim's: its own `global_oom` / `oom_memcg=`
        names the killer;
      * neither: a 137 from something else — `unproven`: UNMEASURED like every
        137 (`suite_outcome`), and never re-run.
    """
    since, cgroup_before, before = _parse_snapshot(snapshot)
    cgroup, after = _own_memory_events()
    scope = os.path.basename(cgroup) if cgroup else ""
    if before is not None and after is not None and cgroup == cgroup_before:
        killed = after["oom_kill"] - before["oom_kill"]
        reached = after["oom"] - before["oom"]
        if killed > 0 and reached > 0:
            return (
                f"oom-kill=cgroup (scope {scope} reached its MemoryMax: "
                f"memory.events oom +{reached}, oom_kill +{killed})"
            )
        if killed > 0:
            return (
                f"oom-kill=host (scope {scope} memory.events oom_kill +{killed} "
                "with no oom of its own: the kill came from outside its limit)"
            )
    if cgroup and since is not None:
        found = journal_kill((journal or read_kernel_journal)(since), cgroup)
        if found:
            return f"oom-kill={found[0]} ({found[1]})"
    return (
        "oom-kill=unproven (no oom_kill on the lane's own cgroup and no kernel "
        "oom-kill line naming it: a SIGKILL from something else)"
    )


def oom_eviction(rc: int, summary: str) -> str | None:
    """`cgroup` / `host` when the lane PROVED a SIGKILL was the OOM killer's."""
    if rc != SIGKILL_RC:
        return None
    m = OOM_TOKEN.match(summary)
    return m.group(1) if m and m.group(1) != "unproven" else None


def rerun_reason(rc: int, summary: str, output: str = "") -> str | None:
    """Why the re-run pass re-runs one wave verdict alone, once — or None.

    Read on top of `suite_outcome`, which alone decides the label; this only
    says whether a second measurement could change it. `output` is the suite's
    own output where this box has it: it can change WHICH reason (a broken
    conftest is `failed`, not `refused`), never whether there is one.

      * `failed`  — the tree's own red in the wave (a case failed, a
        collection error, a broken conftest, errors with no infrastructure
        signature): re-run before it is believed;
      * `refused` — rc 4: the lane posture guard refused the invocation before
        collection (UNMEASURED);
      * `unasked` — rc 75: the guard could not ask the model runtime at all,
        its name not resolving or nothing answering (UNMEASURED);
      * `errored` — rc 1 whose cases only errored, in fixtures, on an
        infrastructure signature (UNMEASURED: Gate HW beside a lane on another
        model);
      * `evicted` — a 137 the lane PROVED was the OOM killer's (`oom_eviction`;
        UNMEASURED).

    None for everything else: a pass; a suite that only skipped; a timeout,
    because a re-run is another full cap; a kill the lane could not prove,
    because something sent signal 9 and a green re-run must not excuse it; and
    any other status pytest ends no run with (3, 5, 143).
    """
    outcome, _why = suite_outcome(rc, summary, output)
    if rc == COLLECTION_ERROR_RC:
        # FAILED, the tree's own red, and never re-run: a module that does not
        # import will not import alone either, and a green re-run must not
        # excuse it (manager ruling, Batch45: rc 2, 3, 5 and 143 are never re-run).
        return None
    if outcome == "failed":
        return "failed"
    if outcome != "unmeasured":
        return None
    if rc == USAGE_ERROR_RC:
        return "refused"
    if rc == COULD_NOT_ASK_RC:
        return "unasked"
    if rc == CASES_DID_NOT_ALL_PASS_RC:
        return "errored"
    if oom_eviction(rc, summary):
        return "evicted"
    return None


def rerun_suites(
    lane_results: list[dict], poisons=frozenset(), tail_order=(), skip=frozenset()
) -> list[str]:
    """The suites the re-run pass re-runs, once each, in the order it runs them.

    Every suite a result reported with a verdict `rerun_reason` names — this
    box's lanes and another box's evidence alike. Not a suite with no SUITE
    line: its LANE failed, not the suite, and re-running a dead lane's whole
    share alone is a second gate, not a re-run. Not a result already marked
    `rerun`, nor any suite one of those planned (that suite has had its one
    re-run: this box's own may have had it beside the other boxes' slices,
    before the pass that re-runs theirs), and not a suite in `skip`: the
    alone phase's own suites ran with the box to themselves already.

    Wave order, except that a suite which poisons its stack goes last, in
    `tail_order`, exactly as it does on a lane — Gate RS restores the vault,
    and nothing, a re-run included, runs behind it.
    """
    found = []
    spent = {suite for res in lane_results if res.get("rerun") for suite in res["planned"]}
    for res in lane_results:
        if res.get("rerun"):
            continue
        reported = {suite: (rc, summary) for suite, rc, summary in res["suites"]}
        for suite in res["planned"]:
            if suite in skip or suite in spent or suite in found:
                continue
            if suite in reported and rerun_reason(*reported[suite]):
                found.append(suite)

    def tail_rank(suite: str) -> tuple[int, int]:
        name = os.path.basename(suite)
        if name not in poisons:
            return (0, 0)
        return (1, tail_order.index(name) + 1 if name in tail_order else 0)

    return sorted(found, key=tail_rank)


#: How a wave line that defers to its re-run names what the wave saw.
_DEFERRED_LABEL = {
    "failed": "red",
    "refused": "refused",
    "unasked": "unasked",
    "errored": "errored",
    "evicted": "evicted",
}

#: Why a re-run was owed, in a FAILED entry for a suite red alone.
_RERUN_BECAUSE = {
    "failed": "red in the wave and again alone",
    "refused": "re-run alone after a refusal in the wave",
    "unasked": "re-run alone after the model runtime could not be asked in the wave",
    "errored": "re-run alone after only errors in the wave",
    "evicted": "re-run alone after an OOM eviction in the wave",
}


def _unmeasured(rc: int | None, summary: str, why: str, spent: str = "") -> tuple[str, str]:
    """`(report detail, list reason)` for a suite that gave no verdict.

    `rc` None is a suite its lane never reported; `why` is `suite_outcome`'s
    (or the lane's own word for a suite it never ran).
    """
    if rc is None:
        return f"({why}{spent})", f"not run{spent}"
    if rc == TIMEOUT_RC:
        return f"rc={TIMEOUT_RC} timeout{spent} — not a pass, not a failure", f"timeout{spent}"
    killer = oom_eviction(rc, summary)
    if killer:
        return (
            f"rc={rc} OOM-killed by the {killer}{spent} — not a pass, not a failure: {summary}",
            f"OOM-killed by the {killer}{spent}",
        )
    if rc == SIGKILL_RC:
        why = "a SIGKILL with no evidence it was the OOM killer's, never re-run"
    said = f" — it said: {summary}" if summary else ""
    return (
        f"rc={rc} {why}{spent} — not a pass, not a failure{said}",
        f"rc={rc}, {why}{spent}",
    )


#: A failed case of a pytest run, as its short summary names it. Only the id is
#: kept: the rest of the line is the product's message and may carry content.
#: A parametrized id may hold spaces (Gate TR's are file names), so the id runs
#: to the first `]` that the message separator or the line's end follows.
FAILED_CASE = re.compile(
    r"^(?:FAILED|ERROR) (\S+?::[^\s\[]+(?:\[.*?\])?)(?: - .*)?$", re.MULTILINE
)

#: Cases an accepted suite's acceptance never covers: Gate TR's sealed-original
#: (tr2) and compartment (tr5) cases. `{suite file name: case-name pattern}`.
NEVER_ACCEPTED_CASES = {"test_gate_tr.py": re.compile(r"test_tr[25]_")}


def failing_case_ids(output: str) -> list[str]:
    """The ids (`file::case`) of the cases a suite's captured output says failed."""
    return list(dict.fromkeys(FAILED_CASE.findall(output)))


#: The failure and error counts of a pytest summary line.
SUMMARY_BAD = re.compile(r"(\d+) (?:failed|errors?)\b")

#: Stands in for failures a summary counts and no id was read for: they could be
#: tr2 or tr5 cases, so an unreadable failure is never covered by an acceptance.
UNREAD_FAILURES = "<failures whose ids could not be read>"


def uncovered_cases(suite: str, cases: list[str], summary: str | None = None) -> list[str]:
    """The failed `cases` that an acceptance of a FAILED `suite` can never cover.

    A failure no id was read for is itself uncovered: the suite's pytest
    `summary` counts more failures than the ids read, or no id was read at all
    (a remote box's output, a collection error, a summary nobody could read).
    Ask only for a suite whose outcome is FAILED: no id is read for any other.
    """
    never = NEVER_ACCEPTED_CASES.get(Path(suite).name)
    if never is None:
        return []
    hit = [c for c in cases if never.search(c.split("::", 1)[1])]
    counted = sum(int(n) for n in SUMMARY_BAD.findall(summary or ""))
    return hit + [UNREAD_FAILURES] if not cases or counted > len(cases) else hit


def _accepted_line(
    suite: str, rc: int, said: str, entry: tuple[date, str], cases: list[str] | None = None
) -> str:
    """The report line of a FAILED suite the owner accepts until `entry`'s date."""
    expiry, citation = entry
    ids = f"  failing: {', '.join(cases)}" if cases else ""
    return f"  ACCEPTED    {suite}  rc={rc} {said}{ids}  (until {expiry.isoformat()}, {citation})"


def _accepted_name(suite: str, lane, rc: int, entry: tuple[date, str]) -> str:
    expiry, citation = entry
    return f"{suite} (lane {lane}, rc={rc}, until {expiry.isoformat()}, {citation})"


#: The header of the report line that names the failed suites worth a re-run alone.
RERUN_HEADER = "RE-RUN THESE ALONE:"


def reruns(failed, accepted, today) -> list[str]:
    """The failed suites worth re-running alone: `failed` minus the live accepted.

    `accepted` is `accepted_reds`' map `{suite: (expiry, citation)}`; an entry is
    live while its expiry is not before `today`, so the suite of an expired entry
    stays in the result even if the map still carries it. An accepted red is
    never re-run: the owner has already ruled on it, and a re-run cannot change
    that verdict, only spend a lane. Order is `failed`'s, each suite once.
    `today` is a parameter: this never reads the clock.
    """
    live = {suite for suite, (expiry, _citation) in (accepted or {}).items() if expiry >= today}
    return [suite for suite in dict.fromkeys(failed) if suite not in live]


def summarise(
    lane_results: list[dict],
    accepted: dict[str, tuple[date, str]] | None = None,
) -> tuple[list[str], bool]:
    """One report for the whole run, and whether it is a pass.

    Four outcomes, never two, and one non-verdict. A suite is PASSED (rc 0),
    FAILED (the tree's own red), or UNMEASURED (the environment's) —
    `suite_outcome` says which, and a suite whose lane never reported it at
    all is UNMEASURED too — or, after the re-run pass, DID NOT REPRODUCE. A
    suite whose cases all skipped is SKIPPED: listed, kept out of the count of
    verdicts, and not red, since the skip is the configuration's choice and
    not an outcome of the run. UNMEASURED is not a pass: the criterion was not
    tested, and a run that quietly counted it as green would be exactly the
    "finished and went red, looking nothing like a failure" trap this harness
    keeps falling into. It is not a failure either, and the count line says
    how many suites gave a verdict so that "N passed" is never read against a
    total that includes one that did not.

    A suite the re-run pass re-ran (a result marked `rerun`) is judged on the
    re-run against its wave verdict, and counted once; its wave line points
    below. Red in the wave and green alone DID NOT REPRODUCE: named, and not a
    failure. Red alone is FAILED, whatever the wave said. A re-run that
    measured nothing leaves a wave red FAILED — the only measurement there is
    said red — and a wave UNMEASURED still UNMEASURED, with no third run. A
    suite UNMEASURED in the wave (refused, errored, evicted) and green alone is
    simply passed: nothing was red to reproduce. Every 137 is UNMEASURED; one
    the lane did not prove was the OOM killer's says so, and is never re-run.

    The counts must add up to the suites scheduled — every suite a result not
    marked `rerun` planned, counted exactly once. A run whose counts do not is
    never green: a suite counted twice or lost is a verdict nobody can vouch
    for.

    `accepted` is the owner's accepted-reds map (`accepted_reds`:
    `{suite: (expiry, citation)}`; an expired entry is already gone from it).
    A suite whose outcome is FAILED and that the map lists is ACCEPTED: printed
    with its expiry and citation, counted in its own figure (shown only when
    there is one), named in an `ACCEPTED RED (not green)` line, and kept out
    of `failed`, so the run can be green. It is never counted as passed, and
    only FAILED is accepted: an UNMEASURED suite stays UNMEASURED and red.
    """
    accepted = accepted or {}
    lines = []
    passed = 0
    failed: list[str] = []
    failed_suites: list[str] = []  # the bare suite of each `failed` entry, for `reruns`
    accepted_red: list[str] = []
    unmeasured: list[str] = []
    not_reproduced: list[str] = []
    skipped: list[str] = []
    counted: dict[str, int] = {}

    wave = [res for res in lane_results if not res.get("rerun")]
    rerun = {suite for res in lane_results if res.get("rerun") for suite in res["planned"]}
    scheduled = list(dict.fromkeys(suite for res in wave for suite in res["planned"]))

    # The wave's verdict on each suite the re-run pass re-ran, which the re-run
    # is judged against: `(reason, rc, summary, lane)`.
    deferred: dict[str, tuple[str, int, str, object, list[str]]] = {}
    for res in wave:
        for suite, rc, summary in res["suites"]:
            reason = rerun_reason(rc, summary, suite_output(res["log"], suite))
            if suite in rerun and suite in res["planned"] and reason:
                deferred[suite] = (
                    reason,
                    rc,
                    summary,
                    res["lane"],
                    failing_case_ids(suite_output(res["log"], suite)),
                )

    for res in lane_results:
        again = bool(res.get("rerun"))
        lane = res["lane"]
        lines.append(
            f"lane {lane}  wall {res['wall']:.0f}s  exit={res['exit']}  "
            f"log={res['log']}" + ("  (re-run alone)" if again else "")
        )
        if res.get("tree"):
            lines.append(f"  tree        start {res['tree'][0]}  collection {res['tree'][1]}")
        reported = {suite: (rc, summary) for suite, rc, summary in res["suites"]}
        not_run = res.get("unmeasured_why") or "no SUITE line — the lane never ran it"
        for suite in res["planned"]:
            if not again and suite in deferred:
                reason, rc, summary, _first_lane, _cases = deferred[suite]
                lines.append(
                    f"  {_DEFERRED_LABEL[reason]:<12}{suite}  rc={rc} {summary} — "
                    "re-run alone, judged on that run"
                )
                continue
            counted[suite] = counted.get(suite, 0) + 1
            rc, summary = reported.get(suite, (None, ""))
            outcome, why = (
                ("unmeasured", not_run)
                if rc is None
                else suite_outcome(rc, summary, suite_output(res["log"], suite))
            )
            judged = outcome == "failed" and suite in accepted
            cases = failing_case_ids(suite_output(res["log"], suite)) if judged else []
            never = uncovered_cases(suite, cases, summary) if judged else []
            if never:
                why = f"{why}; never accepted: {', '.join(never)}".lstrip("; ")
            because = f" ({why})" if outcome == "failed" and why else ""
            take = suite in accepted and not never
            if outcome == "passed" and tree_moved(res.get("tree")):
                # Not a failure and not a pass: the cases ran, but against a tree
                # that changed under them, so no verdict names what it covered.
                outcome = "unmeasured"
                why = (
                    f"the tree moved under the lane (start {res['tree'][0]}, collection "
                    f"{res['tree'][1]}): rc 0 covers no tree anyone can reconstruct"
                )

            if again and suite in deferred:
                first, first_rc, first_summary, first_lane, first_cases = deferred[suite]
                first_take = suite in accepted and not uncovered_cases(suite, first_cases, first_summary)
                if outcome == "passed" and first == "failed":
                    lines.append(
                        f"  did not reproduce  {suite}  red in the wave (lane {first_lane}, "
                        f"rc={first_rc} {first_summary}), passed alone: {summary}"
                    )
                    not_reproduced.append(
                        f"{suite} (red on lane {first_lane} in the wave, rc={first_rc}; "
                        f"passed alone on lane {lane})"
                    )
                elif outcome == "passed":
                    lines.append(f"  passed      {suite}  {summary}")
                    passed += 1
                elif outcome == "failed" and take:
                    lines.append(
                        _accepted_line(suite, rc, f"{summary}{because}", accepted[suite], cases)
                    )
                    accepted_red.append(_accepted_name(suite, lane, rc, accepted[suite]))
                elif outcome == "failed":
                    lines.append(
                        f"  FAILED      {suite}  rc={rc} {summary}{because} — "
                        f"{_RERUN_BECAUSE[first]}"
                    )
                    failed.append(f"{suite} (lane {lane}, rc={rc}, {_RERUN_BECAUSE[first]})")
                    failed_suites.append(suite)
                elif first == "failed" and first_take:
                    lines.append(
                        _accepted_line(
                            suite, first_rc, first_summary, accepted[suite], first_cases
                        )
                    )
                    accepted_red.append(
                        _accepted_name(suite, first_lane, first_rc, accepted[suite])
                    )
                elif first == "failed":
                    detail, _reason = _unmeasured(rc, summary, why)
                    lines.append(
                        f"  FAILED      {suite}  rc={first_rc} {first_summary} in the wave; "
                        f"the re-run alone measured nothing ({detail}), so that verdict stands"
                    )
                    failed.append(
                        f"{suite} (lane {first_lane}, rc={first_rc}; its re-run measured nothing)"
                    )
                    failed_suites.append(suite)
                else:
                    detail, reason = _unmeasured(rc, summary, why, ", the one re-run spent")
                    lines.append(f"  UNMEASURED  {suite}  {detail}")
                    unmeasured.append(f"{suite} (lane {lane}, {reason})")
                continue

            if outcome == "passed":
                lines.append(f"  passed      {suite}  {summary}")
                passed += 1
            elif outcome == "skipped":
                lines.append(f"  SKIPPED     {suite}  {summary} — no case asserted anything")
                skipped.append(f"{suite} (lane {lane}, {why})")
            elif outcome == "failed" and take:
                lines.append(
                    _accepted_line(suite, rc, f"{summary}{because}", accepted[suite], cases)
                )
                accepted_red.append(_accepted_name(suite, lane, rc, accepted[suite]))
            elif outcome == "failed":
                lines.append(f"  FAILED      {suite}  rc={rc} {summary}{because}")
                failed.append(f"{suite} (lane {lane}, rc={rc})")
                failed_suites.append(suite)
            else:
                detail, reason = _unmeasured(rc, summary, why)
                lines.append(f"  UNMEASURED  {suite}  {detail}")
                unmeasured.append(f"{suite} (lane {lane}, {reason})")

    measured = passed + len(failed) + len(accepted_red) + len(not_reproduced)
    total = measured + len(unmeasured) + len(skipped)
    also = f", {len(skipped)} SKIPPED gave none" if skipped else ""
    mismatch = sorted(
        suite
        for suite in set(counted) | set(scheduled)
        if counted.get(suite, 0) != (1 if suite in scheduled else 0)
    )
    lines.append("")
    # The ACCEPTED figure is printed only when there is one, so a run with no
    # accepted red reads exactly as it always did.
    figure = f"{len(accepted_red)} ACCEPTED, " if accepted_red else ""
    lines.append(
        f"{passed} passed, {len(failed)} failed, {figure}{len(unmeasured)} UNMEASURED, "
        f"{len(not_reproduced)} did not reproduce; "
        f"{total} of {len(scheduled)} scheduled suite(s) accounted for — "
        f"{measured} of {total} suite(s) gave a verdict{also}"
    )
    if mismatch:
        lines.append(
            "COUNT MISMATCH: counted other than once, or not scheduled: " + ", ".join(mismatch)
        )
    if failed:
        lines.append("FAILED: " + ", ".join(failed))
        # An accepted suite never reaches `failed`, so `accepted` here is only
        # the belt to that brace; the map is already live (the loader dropped an
        # expired entry), so every entry counts as live: `date.min`.
        owed = reruns(failed_suites, accepted, date.min)
        if owed:
            lines.append(f"{RERUN_HEADER} " + " ".join(owed))
    if accepted_red:
        lines.append("ACCEPTED RED (not green): " + ", ".join(accepted_red))
    if unmeasured:
        lines.append("UNMEASURED: " + ", ".join(unmeasured))
    if not_reproduced:
        lines.append("DID NOT REPRODUCE: " + ", ".join(not_reproduced))
    if skipped:
        lines.append("SKIPPED: " + ", ".join(skipped))
    green = not failed and not unmeasured and not mismatch
    notes = []
    if green and accepted_red:
        notes.append(
            f"{len(accepted_red)} ACCEPTED red, named above — still red, never counted "
            "passed, blocking again at its expiry"
        )
    if green and not_reproduced:
        notes.append(
            f"{len(not_reproduced)} did not reproduce, named above — "
            "red in the wave, green alone, not counted against the batch"
        )
    if notes:
        lines.append(f"GATE RUN GREEN ({'; '.join(notes)})")
    else:
        lines.append("GATE RUN GREEN" if green else "GATE RUN RED")
    # The floor is ink in this report. `green` is already decided.
    lines.extend(benchmark_floor_lines())
    return lines, green


#: How many suites red alone the batch split takes on at most. Each costs the
#: suite several times over (a probe per round and lane, then the batch without
#: the seam named), and a batch with more reds than this is not one seam's.
DEFAULT_BATCH_SPLIT_LIMIT = 2


def _load_batch_split():
    """Import `gate_batch_split` by path, as `_load_distribute` does."""
    spec = importlib.util.spec_from_file_location(
        "gate_batch_split", ROOT / "scripts" / "gate_batch_split.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reds_alone(lane_results: list[dict]) -> list[str]:
    """The suites the re-run pass re-ran and found red alone, in re-run order.

    These and no others are a batch red that reproduces alone: `summarise`
    calls each FAILED whatever the wave said. Not a suite red in the wave and
    green alone (DID NOT REPRODUCE), not one whose re-run measured nothing, and
    not one never re-run — past `--rerun-limit`, or a verdict no re-run cures:
    with one measurement there is nothing to say it is the tree's and not the
    wave's.
    """
    found = []
    for res in lane_results:
        if not res.get("rerun"):
            continue
        reported = {suite: (rc, summary) for suite, rc, summary in res["suites"]}
        for suite in res["planned"]:
            if suite not in reported or suite in found:
                continue
            rc, summary = reported[suite]
            if suite_outcome(rc, summary, suite_output(res["log"], suite))[0] == "failed":
                found.append(suite)
    return found


def batch_split_lines(
    lane_results: list[dict],
    base: str | None,
    limit: int,
    lanes: list[int],
    splitter: Callable[[str], list[str]],
) -> list[str]:
    """The batch split's part of the report: which seam carries each red that
    reproduced alone (`gate_batch_split`), or why nobody asked.

    Nothing at all when no red reproduced alone: a red the wave made is not
    split, because a half-batch tree would be bisecting a flake and would name
    a seam for it. `splitter(suite)` runs one suite's split and returns its
    lines; whatever it raises is that suite's line, never the run's verdict,
    which these lines do not touch.
    """
    reds = reds_alone(lane_results)
    if not reds:
        return []
    named = f"{len(reds)} suite(s) red alone ({', '.join(reds)})"
    if not base:
        return [
            (
                f"batch split NOT run: {named}, and no --batch-base names the pushed trunk tip "
                "the batch was merged onto"
            )
        ]
    if len(reds) > limit:
        return [
            (
                f"batch split NOT run: {named} exceed --batch-split-limit {limit} — "
                "a batch this red is not one seam's"
            )
        ]
    if not lanes:
        return [f"batch split NOT run: {named}, and this run has no lane of its own to probe on"]
    lines = []
    for suite in reds:
        print(
            f"BATCH SPLIT: {suite} over the half-batch trees from {base}, one probe at a time on lane {lanes[0]}",
            flush=True,
        )
        try:
            lines.extend(splitter(suite))
        except Exception as exc:  # noqa: BLE001 - a split that broke is a line, never the verdict
            lines.append(f"batch split NOT run for {suite}: {exc}")
    return lines


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gate_run.py",
        description="Run the gate suites across N disposable lanes and report once.",
    )
    p.add_argument(
        "--lanes",
        type=int,
        default=None,
        help="lane count (default: from MemAvailable; when SKYKEEP_GATE_REMOTE_BOXES names a box, "
        "this box's own declaration over SKYKEEP_GATE_BOX_FIRST_LANE..SKYKEEP_GATE_BOX_LAST_LANE, "
        "or given with --first-lane as the free range this box may use)",
    )
    p.add_argument(
        "--first-lane", type=int, default=None,
        help=f"first lane number ({LANE_FIRST}..{LANE_LAST}; default {LANE_FIRST})",
    )
    p.add_argument(
        "--memory-per-lane",
        default=MEASURED_LANE_MEMORY,
        help=(
            "what one lane holds: the divisor for the lane count, and MemoryMax for "
            f"each lane's scope (default {MEASURED_LANE_MEMORY}, measured)"
        ),
    )
    p.add_argument(
        "--memory-high-per-lane",
        default=None,
        help="MemoryHigh for each lane's scope: a soft budget the kernel throttles at, "
        "strictly below --memory-per-lane (default: none set)",
    )
    p.add_argument("--dry-run", action="store_true", help="print the lane commands and run nothing")
    p.add_argument("--memory-blockers", action="store_true", help="show running Compose projects and their memory owners")
    p.add_argument("--reclaim-idle-project", metavar="PROJECT", help="manually reclaim one proven idle lane")
    p.add_argument("--owner-unit", metavar="UNIT", help="expected owning user service for reclamation")
    p.add_argument("--idle-seconds", type=int, help="configured uninterrupted idle observation period")
    p.add_argument("--poll-seconds", type=float, help="configured observation interval")
    p.add_argument("--label", default=None, help="label for the lane logs (default: gates-<stamp>)")
    p.add_argument(
        "--extras",
        dest="extras",
        action="store_true",
        default=None,
        help="also run tests/bdd and the non-gate suites under tests/gates",
    )
    p.add_argument("--no-extras", dest="extras", action="store_false", help="gate suites only")
    p.add_argument(
        "-n",
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help=(
            "pytest-xdist workers INSIDE each lane, for the suites that are not "
            "marked serial (default: one process per lane)"
        ),
    )
    p.add_argument(
        "--idle-box",
        action="append",
        default=[],
        metavar="BOX",
        help="a configured box with no other gate lane running: a suite that needs "
        "the box alone is sent there (repeatable; default: it stays on this box)",
    )
    p.add_argument(
        "--remote-evidence",
        action="append",
        default=[],
        metavar="BOX=FILE",
        help="where a configured box's evidence record is read from (repeatable; a "
        "box with a slice and no complete record there by the deadline is a red run)",
    )
    p.add_argument(
        "--box-declaration",
        action="append",
        default=[],
        metavar="BOX=FILE",
        help="where a configured box's capacity declaration is read from (repeatable; a "
        "listed box with none usable gets no slice and the run is refused)",
    )
    p.add_argument(
        "--declare-box",
        metavar="NAME",
        help="print THIS box's capacity declaration (MemAvailable, cores, reserved CPUs, "
        "the free lanes --first-lane and --lanes name, or --lane-list) for the "
        "integrator's plan, and exit",
    )
    p.add_argument(
        "--lane-list",
        default=None,
        help="lanes this box may use, comma-separated, in place of --first-lane and "
        "--lanes (a duplicate or a lane outside the window is refused by name)",
    )
    p.add_argument(
        "--endpoint-lanes",
        type=int,
        default=None,
        help="an optional ceiling on how many lanes, over every box, run an endpoint suite "
        "at once (default: SKYKEEP_GATE_ENDPOINT_LANES, and empty is no ceiling: every "
        "lane runs them); a box running its slice passes the slice's endpoint_lanes",
    )
    p.add_argument(
        "--rerun-limit",
        type=int,
        default=DEFAULT_RERUN_LIMIT,
        metavar="N",
        help=(
            "re-run each suite the wave left FAILED, refused, errored or OOM-evicted alone, "
            f"once, only when there are at most N of them (default {DEFAULT_RERUN_LIMIT}; "
            "0 re-runs nothing)"
        ),
    )
    p.add_argument(
        "--batch-base",
        default=None,
        metavar="REF",
        help=(
            "the pushed trunk tip the batch under test was merged onto. With it, a suite red "
            "in the wave and red again alone is run on half-batch trees on this run's first lane "
            "until one seam is named (scripts/gate_batch_split.py); without it nothing is split. "
            "Needs --batch-split-budget"
        ),
    )
    p.add_argument(
        "--batch-split-budget",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "required with --batch-base, and it has no default: the wall-clock allowance of the "
            "whole batch split. The split costs each suite several times over after the report "
            "is already written; a probe still running when the budget is spent has its process "
            "group killed and its lane brought down, and names no seam"
        ),
    )
    p.add_argument(
        "--batch-split-limit",
        type=int,
        default=DEFAULT_BATCH_SPLIT_LIMIT,
        metavar="N",
        help=(
            "split the batch only when at most N suites are red alone "
            f"(default {DEFAULT_BATCH_SPLIT_LIMIT}; 0 splits nothing)"
        ),
    )
    p.add_argument("suites", nargs="*", help="suite paths (default: every tests/gates/test_gate_*.py)")
    return p


def check_xdist(workers: int | None) -> None:
    """Refuse `-n` when pytest-xdist is not installed.

    Without this the flag is accepted, `gate_lane.sh` hands pytest a `-n` it
    does not understand, and every suite on every lane exits 4 in a second —
    a whole batch reported as failed for a reason that has nothing to do with
    the tree. The flag either delivers parallelism or refuses.
    """
    if workers is None:
        return
    if workers < 1:
        raise Refused(f"-n must be at least 1, got {workers}")
    if importlib.util.find_spec("xdist") is None:
        raise Refused(
            "-n was asked for but pytest-xdist is not installed — `uv sync` the "
            "dev group. A run that silently fell back to one worker would report "
            "a wall clock the next batch gets scheduled on"
        )


def main(argv: list[str]) -> int:
    # Whatever ends the run — a verdict, a refusal, an exception — the refs an
    # automatic split run published are deleted (GATE-INTEGRATOR-SLICE-PUBLISH).
    published: list = []
    try:
        return _run(argv, published)
    finally:
        for transport in published:
            for left in transport.cleanup():
                print(f"WARNING: {left}", file=sys.stderr)


def _run(argv: list[str], published: list) -> int:
    # `gate_lane.sh` calls back into this module after a parallel batch, to turn
    # one process's junit xml into the per-suite lines the lane log is made of.
    # A subcommand rather than a second script: the mapping and the reader that
    # consumes it belong in one file, or they drift.
    if argv and argv[0] == "--junit-summary":
        if len(argv) < 4:
            print(
                "REFUSED: --junit-summary <xml> <batch rc> <suite path...>",
                file=sys.stderr,
            )
            return 3
        for line in junit_summary(Path(argv[1]), list(argv[3:]), int(argv[2])):
            print(line)
        return 0

    # `gate_lane.sh`'s vault check, the same way round: the lane runs the probe
    # inside its own vecstore and hands the answer back here to be judged, so
    # the query and the reader of its answer live in one file.
    if argv and argv[0] == "--vault-probe":
        print(vault_probe_sql(), end="")
        return 0
    if argv and argv[0] == "--vault-verdict":
        if len(argv) != 2 or not re.fullmatch(r"[0-9]+|-", argv[1]):
            print(
                "REFUSED: --vault-verdict <max age in hours, or - to leave age out>"
                " (the probe's answer on stdin)",
                file=sys.stderr,
            )
            return 3
        hours = None if argv[1] == "-" else int(argv[1])
        # ONE line, reasons joined: the lane reads it into one variable.
        print("; ".join(vault_verdict(sys.stdin.read(), hours)))
        return 0

    # And its bring-up, the same way round: the lane hands a failed `compose
    # up`'s output back on stdin and reads ONE line — `fetch <the line that
    # showed it>`, which it tries again, or `build`, which it refuses.
    if argv and argv[0] == "--bringup-verdict":
        seen = bringup_fetch_failure(sys.stdin.read())
        print("build" if seen is None else f"fetch {seen}")
        return 0

    # And which suites need the box alone: the lane refuses such a suite while
    # another lane runs, even launched by hand, and asks the scheduler's own
    # set rather than keeping a copy. One line, space-separated basenames.
    if argv and argv[0] == "--box-alone-suites":
        print(" ".join(sorted(_load_distribute().NEEDS_THE_BOX_ALONE)))
        return 0

    # And which suites read with the vision model: the lane names that model
    # only to a pass that carries one of them, and asks the scheduler's own
    # set for the same reason. One line, space-separated basenames.
    if argv and argv[0] == "--vision-suites":
        print(" ".join(sorted(_load_distribute().NEEDS_THE_VISION_MODEL)))
        return 0

    # And the lane's `dirty=` figure for its START line, for the same reason:
    # the box runner judges that line, so one function counts what it means.
    if argv and argv[0] == "--lane-dirty-count":
        print(lane_dirty_count())
        return 0

    # The vision WAVE's suites under this environment: the box runner asks it of
    # the candidate, through the box's own lane settings, to refuse a slice that
    # carries a suite held for the wave. Empty while no vision model is set.
    if argv and argv[0] == "--vision-wave-suites":
        print(" ".join(sorted(_load_distribute().vision_wave_suites())))
        return 0

    # And where its pytest basetemps go, for the same reason: the lane makes
    # them and this module's preflight prunes them, so one function names the
    # root for both.
    if argv and argv[0] == "--lane-pytest-root":
        print(lane_pytest_root())
        return 0

    # And a SIGKILLed suite's evidence: the lane snapshots its own scope before
    # each suite and, on a 137, asks for the one token it heads the line with.
    # Here and not in the shell, so the reader of the counters and the reader
    # of the token (`oom_eviction`) are one file.
    if argv and argv[0] == "--oom-snapshot":
        print(oom_snapshot())
        return 0
    if argv and argv[0] == "--oom-evidence":
        if len(argv) != 2:
            print(
                "REFUSED: --oom-evidence <the line --oom-snapshot printed before the suite>",
                file=sys.stderr,
            )
            return 3
        print(oom_evidence(argv[1]))
        return 0

    args = build_parser().parse_args(argv)

    reclaim_values = (args.reclaim_idle_project, args.owner_unit, args.idle_seconds, args.poll_seconds)
    if any(value is not None for value in reclaim_values):
        if any(value is None for value in reclaim_values) or args.suites or args.memory_blockers:
            print("REFUSED: manual reclaim requires --reclaim-idle-project, --owner-unit, --idle-seconds, and --poll-seconds without suites", file=sys.stderr)
            return 3
        try:
            reclaim_idle_project(args.reclaim_idle_project, args.owner_unit, args.idle_seconds, poll_seconds=args.poll_seconds)
        except Refused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3
        return 0
    if args.memory_blockers:
        if args.suites:
            print("REFUSED: --memory-blockers accepts no suites", file=sys.stderr)
            return 3
        try:
            print(memory_blockers())
        except Refused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3
        return 0
    if args.lane_list and args.declare_box is None and (
        args.lanes is not None or args.first_lane is not None
    ):
        print(
            "REFUSED: --lane-list takes the place of --first-lane and --lanes, not both",
            file=sys.stderr,
        )
        return 3
    if args.declare_box is not None:
        # A box answering the integrator: what it can hold, measured now. It
        # needs no model configuration and runs nothing.
        try:
            if args.suites:
                raise Refused("--declare-box takes no suites")
            if not _load_distribute().BOX_NAME.fullmatch(args.declare_box):
                raise Refused(f"--declare-box {args.declare_box!r} is not a lower-case host label")
            listed = (args.lane_list or "").strip()
            if listed:
                if args.lanes is not None or args.first_lane is not None:
                    raise Refused(
                        "--declare-box takes --lane-list or --lanes with --first-lane, not both"
                    )
                chosen = parse_lane_list(listed)
                record = declare_box(args.declare_box, chosen[0], len(chosen), chosen=chosen)
            else:
                if args.lanes is None:
                    raise Refused(
                        "--declare-box takes --lanes (the lanes this box is free to use, "
                        "from --first-lane) or --lane-list, and no suites"
                    )
                first = LANE_FIRST if args.first_lane is None else args.first_lane
                record = declare_box(args.declare_box, first, args.lanes)
            print(json.dumps(record, sort_keys=True))
        except Refused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3
        return 0

    try:
        cfg = check_config()
        accepted = accepted_reds(
            os.environ.get("SKYKEEP_GATE_ACCEPTED_REDS"), datetime.now(UTC).date()
        )
        pytest_tmp_age = _pytest_tmp_age_seconds()
        session_decl_dir()
        per_lane = parse_memory(args.memory_per_lane)
        if args.memory_high_per_lane is not None:
            high = parse_memory(args.memory_high_per_lane, "--memory-high-per-lane")
            if high >= per_lane:
                raise Refused(
                    f"--memory-high-per-lane {args.memory_high_per_lane} must be "
                    f"strictly below --memory-per-lane {args.memory_per_lane}"
                )
        check_xdist(args.workers)
        if args.rerun_limit < 0:
            raise Refused(f"--rerun-limit must be 0 or more, got {args.rerun_limit}")
        if args.batch_split_limit < 0:
            raise Refused(f"--batch-split-limit must be 0 or more, got {args.batch_split_limit}")
        if args.batch_base and args.batch_split_budget is None:
            raise Refused(
                "--batch-base needs --batch-split-budget SECONDS: the batch split runs each red "
                "suite several times over and nothing else bounds it"
            )
        if args.batch_split_budget is not None and args.batch_split_budget < 1:
            raise Refused(
                f"--batch-split-budget must be a positive whole number of seconds, got {args.batch_split_budget}"
            )
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3

    # Once, at the start: which reds the owner accepts until when, so a reader
    # of the log sees the exceptions before the verdict they change.
    for suite, (expiry, citation) in sorted(accepted.items()):
        print(f"accepted red: {suite} until {expiry.isoformat()} ({citation})", flush=True)

    named = [str(Path(s)) for s in args.suites]
    # The clamd wave's suites: every one on a full run, and a named one when
    # somebody names it. They never reach the packer, so no standard lane is
    # ever handed one.
    clamd_known = discover_clamd_suites()
    clamd = [s for s in named if s in clamd_known] if named else clamd_known
    named = [s for s in named if s not in clamd]
    suites = named if args.suites else discover_suites()
    if not suites and not clamd:
        print("REFUSED: no gate suites found", file=sys.stderr)
        return 3
    # Extras default ON for a full run and OFF when suites were named by hand:
    # somebody re-running two short suites after a fix does not want the whole
    # behaviour lane dragged in behind them.
    want_extras = (not named) if args.extras is None else args.extras
    extras = [e for e in discover_extras() if e not in suites] if want_extras else []

    distribute = _load_distribute()
    try:
        boxes = distribute.remote_boxes()
        remote = remote_setup(args, boxes, distribute.endpoint_marker())
        endpoint_cap = distribute.endpoint_lane_cap(given=args.endpoint_lanes)
    except (Refused, distribute.SplitRefused, ValueError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3

    first = LANE_FIRST if args.first_lane is None else args.first_lane
    lane_list: list[int] | None = None
    if args.lane_list:
        if boxes:
            print(
                "REFUSED: --lane-list is read only in a single-box run (it is refused in split "
                "mode, where each box declares its own lanes)",
                file=sys.stderr,
            )
            return 3
        try:
            lane_list = parse_lane_list(args.lane_list)
        except Refused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3
        first = lane_list[0]
        args.lanes = len(lane_list)
    if args.lanes is None:
        try:
            if boxes:
                # Split mode: this box names its lanes, as every listed box names
                # its own in its declaration. A count from MemAvailable alone
                # runs from --first-lane upward over lanes a peer's stack may be
                # on, so with no --lanes this box declares itself over the range
                # its settings name, by the rule every other box's share is cut by.
                lanes, first, why = local_lanes(args.first_lane, per_lane, worker_memory_bytes())
            else:
                lanes, why = default_lanes(per_lane)
        except Refused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3
        print(f"lanes: {lanes} ({why})")
    else:
        lanes, why = args.lanes, "given on the command line"

    if lanes < 1:
        print("REFUSED: --lanes must be at least 1", file=sys.stderr)
        return 3
    if not LANE_FIRST <= first <= LANE_LAST or first + lanes - 1 > LANE_LAST:
        print(
            f"REFUSED: lanes must fit {LANE_FIRST}..{LANE_LAST} "
            f"(asked {lanes} lanes from {first})",
            file=sys.stderr,
        )
        return 3
    numbers = lane_list if lane_list is not None else list(range(first, first + lanes))
    if boxes or lane_list is not None:
        # And that range is free: no stack on it but this checkout's own.
        try:
            refuse_taken_lanes(first, lanes, mine=ROOT, lane_numbers=numbers)
        except Refused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    label = args.label or f"gates-{stamp}"
    run_id = f"{stamp}-{os.getpid()}"

    # The integrating box's side of the wire (GATE-INTEGRATOR-SLICE-PUBLISH).
    # With a box configured and neither hand-run flag given, this run moves
    # the refs itself: the candidate first (refused if its push would publish
    # anything but this batch's merges), then a declaration request to every
    # box and a bounded wait for each answer. A dry run publishes nothing and
    # reads the declarations already there. `published` is how `main` deletes
    # whatever went out, however this run ends.
    transport = fetched = None
    if boxes and not args.box_declaration and not args.remote_evidence:
        try:
            transport = open_transport(boxes, remote["candidate_ref"])
            merges = transport.check_publishable(remote["candidate"])
            print(
                f"CANDIDATE {remote['candidate']}: {len(merges)} commit(s) to publish, every one "
                "a merge of work already published"
            )
            if args.dry_run:
                fetched = transport.current_declarations(boxes, declaration_verdict, DECLARATION_MAX_BYTES)
            else:
                published.append(transport)
                transport.publish_candidate(remote["candidate"])
                asked = transport.request_declarations(boxes, run_id, DECLARATION_MAX_BYTES)
                fetched = transport.await_declarations(boxes, asked, declaration_verdict, DECLARATION_MAX_BYTES)
        except (Refused, _load_remote().TransportRefused) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3

    basenames = [os.path.basename(s) for s in suites]
    by_basename = {os.path.basename(s): s for s in suites}
    # With no box configured this is `distribute.plan` over this box's lanes,
    # under the endpoint ceiling when the owner set one; with boxes, `ordered`
    # and `notes` are this box's share and `split["boxes"]` is everybody
    # else's, each box's lane count the one it declared.
    try:
        declared_boxes(args, boxes, per_lane, worker_memory_bytes(), texts=fetched)
        split = distribute.split_plan(
            basenames, lanes, boxes, extras=extras, idle=args.idle_box,
            endpoint_lanes=endpoint_cap,
        )
    except (Refused, distribute.SplitRefused) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    ordered, _lane_load, notes = split["local"]
    here_endpoint = notes.get("endpoint_lanes", list(range(lanes)))
    every_lane = lanes + sum(share["lanes"] for share in split["boxes"])
    here_named = f"here lane(s) {', '.join(str(numbers[k]) for k in here_endpoint) or 'none'}"
    if endpoint_cap is None:
        print(
            f"ENDPOINT CAP none ({distribute.ENDPOINT_LANES_SETTING} empty): all {every_lane} "
            f"lane(s) over every box may run endpoint suites; {here_named}"
        )
    else:
        print(
            f"ENDPOINT CAP {endpoint_cap} lane(s) over every box run endpoint suites at once; "
            f"{here_named}"
        )
        if endpoint_cap < every_lane:
            # Never silent: a ceiling below the run's lanes leaves lanes that
            # carry only the suites that never call the endpoint.
            print(
                f"ENDPOINT CEILING {endpoint_cap} is below this run's {every_lane} lane(s): only "
                f"{endpoint_cap} run endpoint suites at once. Empty "
                f"{distribute.ENDPOINT_LANES_SETTING} (and no --endpoint-lanes) lets every lane run them"
            )
    for share in split["boxes"]:
        print(
            f"BOX {share['name']}: {share['lanes']} lane(s) from {share.get('first_lane')}, "
            f"{share.get('endpoint_lanes')} may run endpoint suites, "
            f"{share.get('workers')} worker(s) ({share.get('capacity_why', 'no declaration read')})"
        )

    expires = slice_expiry(remote["timeout"]) if remote else None
    slices = [
        box_slice(
            share, remote["candidate"], f"{run_id}-{share['name']}",
            lambda s: by_basename.get(s, s), endpoint=remote["endpoint"],
            candidate_ref=remote["candidate_ref"], expires_utc=expires,
        )
        for share in split["boxes"]
        if any(share["ordered"]) or share["exclusive"]
    ]
    if transport is not None:
        # Each box's evidence is copied off its status ref into this file
        # while the barrier waits, and read from here as a hand-run flag's is.
        for sent in slices:
            remote["paths"][sent["box"]] = LOG_DIR / f"gates-{stamp}-evidence-{sent['box']}.json"
    unread = [sent["box"] for sent in slices if sent["box"] not in remote["paths"]]
    if unread and not args.dry_run:
        print(
            f"REFUSED: no --remote-evidence for {', '.join(unread)} — with nowhere to "
            "read a box's evidence its slice could only ever be UNMEASURED",
            file=sys.stderr,
        )
        return 3

    plans = []
    for k in range(lanes):
        paths = [by_basename.get(s, s) for s in ordered[k]]
        if paths:
            plans.append((numbers[k], paths))

    # The restart wave: suites that stop, start or recreate containers, packed
    # over the same lanes and run once every lane of the suite wave above has
    # finished, so no browser suite is running anywhere beside them. Its own
    # label, for the vision wave's reason: its barrier pass truncates its log.
    restart_plans = []
    for k, wave_bin in enumerate(notes["restarts"]):
        if wave_bin:
            restart_plans.append((numbers[k], [by_basename.get(s, s) for s in wave_bin]))
    restart_label = f"{label}-restarts"
    # Every lane this run brings up, in either wave.
    run_lanes = sorted({lane for lane, _ in plans} | {lane for lane, _ in restart_plans})

    try:
        # A run whose only suites are a vision or exclusive phase has no lane
        # in `plans`; those phases run on the first lane, so budget that one.
        shares, cpu_shares = wave_capacity(
            run_lanes or [first], worker_memory_bytes(), per_lane
        )
        if args.workers and any(args.workers > share for share in shares.values()):
            raise Refused(
                f"-n {args.workers} exceeds a lane's share of the total worker budget "
                f"({', '.join(str(s) for s in shares.values())})"
            )
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3

    # The exclusive phase: suites that reshape the deployment itself, run after
    # every lane has finished and with nothing else on the box. They are not in
    # `ordered`, so they would otherwise simply be dropped.
    exclusive = [by_basename.get(s, s) for s in notes["exclusive"]]
    serial = set(notes["serial"])

    # The vision wave: suites that read with the vision model, run on the first
    # lane once every lane's suites have finished and, unless the alone phase ran
    # beside the other boxes' slices, before it takes any lane down. Not in `ordered` either. Its own label, for the
    # alone phase's reason: its barrier pass truncates the log it writes to.
    vision = [by_basename.get(s, s) for s in notes["vision"]]
    vision_label = f"{label}-vision"

    # The clamd wave: the real-engine suites, on the first lane re-prepared with
    # a clamd daemon, after the vision wave and alone in their wave. Its own
    # label, for the vision wave's reason.
    clamd_label = f"{label}-clamd"
    clamd_extra = clamd_lane_extra(os.environ.get("LANE_EXTRA"))

    # The alone phase runs on the first lane, and before it this run brings
    # its OTHER lanes down with their volumes — the lanes it brought up
    # itself and no others, so a lane number this run never used is never
    # touched (the phase's own lane then refuses while that one runs). It has
    # its own label, because a lane's barrier pass truncates its log at its start
    # (a suite pass keeps it), and the suite wave's logs are what `gate_distribute` harvests durations from.
    alone_label = f"{label}-alone"
    to_tear_down = [lane for lane in run_lanes if lane != first] if exclusive else []

    if notes["unknown"]:
        print(f"UNMEASURED weight (scheduled at {int(distribute.UNKNOWN_WEIGHT)}s): "
              f"{', '.join(notes['unknown'])}")
    if notes["only_timed_out"]:
        print("WARNING: only ever timed out on this endpoint, so the weight is a "
              f"guess and probably far too low: {', '.join(notes['only_timed_out'])}")
    # What a `-n` batch could not teach the packer, in words: a suite its junit
    # xml held no case for, and a lane log with no SUITE line for a suite it was
    # handed. Both read, in the table above, like a suite nobody has run.
    for warning in distribute.batch_warnings(notes):
        print(warning)
    if args.workers and serial:
        print(f"SERIAL (run whole, never under -n): {', '.join(sorted(serial))}")
    if restart_plans:
        print(
            "RESTART WAVE (a wave of their own, once no lane runs a browser suite): "
            + "; ".join(f"lane {lane}: {', '.join(paths)}" for lane, paths in restart_plans)
        )
    if vision:
        print(
            "VISION WAVE (a wave of their own on lane "
            f"{first}, once no other lane is mid-suite): {', '.join(vision)}"
        )
    # In split mode the alone phase runs once this box's own lanes are done,
    # while the other boxes are still on their slices: they are other hosts,
    # and its lane never names the vision model. Under an endpoint ceiling
    # it does so only on a lane of this box's share, which its finished lanes
    # have released; with none here, it waits for every box as before. The
    # re-run of this box's own non-clean suites follows it there, by the same
    # rule (`rerun_pass`).
    beside_slices = bool(slices) and (endpoint_cap is None or bool(here_endpoint))
    alone_early = False
    if exclusive:
        print(f"EXCLUSIVE (a phase of their own, alone on the box): {', '.join(exclusive)}")
        alone_early = beside_slices
        if alone_early:
            print(
                "ALONE PHASE starts once this box's own lanes have finished, while the "
                "other boxes' slices are still out: before the wait for their evidence "
                "and before the vision wave"
            )
        if to_tear_down:
            print(
                "ALONE PHASE brings this run's own lane(s) down with their volumes "
                f"first: {', '.join(f'skykeep-p{lane}' for lane in to_tear_down)}"
            )
    if beside_slices:
        print(
            "RE-RUN PASS of this box's own non-clean suites starts once its lanes"
            + (" and the alone phase" if exclusive else "")
            + " have finished, while the other boxes' slices are still out; "
            "another box's non-clean suites are re-run once its evidence is read"
        )

    def lane_cmd(lane, paths, prepare_only=False, *, alone=False, teardown=False,
                 lane_label=label, wave_shares=None, wave_cpu=None, lane_extra=None):
        active_shares = shares if wave_shares is None else wave_shares
        active_cpu = cpu_shares if wave_cpu is None else wave_cpu
        return lane_command(
            lane,
            lane_label,
            paths,
            args.memory_per_lane,
            memory_high=args.memory_high_per_lane,
            workers=args.workers,
            serial=serial,
            prepare_only=prepare_only,
            alone=alone,
            teardown=teardown,
            run_id=run_id,
            maxprocesses=active_shares.get(lane),
            cpu_quota=f"{active_cpu[lane] * 100}%" if lane in active_cpu else None,
            lane_extra=lane_extra,
        )

    if args.dry_run:
        for lane, paths in plans:
            print(shlex.join(lane_cmd(lane, paths, prepare_only=True)))
        for lane, paths in plans:
            print(shlex.join(lane_cmd(lane, paths)))
        for lane, paths in restart_plans:
            print(shlex.join(lane_cmd(lane, paths, prepare_only=True, lane_label=restart_label)))
        for lane, paths in restart_plans:
            print(shlex.join(lane_cmd(lane, paths, lane_label=restart_label)))
        if vision:
            print(shlex.join(lane_cmd(first, [], prepare_only=True, lane_label=vision_label)))
            print(shlex.join(lane_cmd(first, vision, lane_label=vision_label)))
        if clamd:
            print(f"CLAMD WAVE on lane {first}: {', '.join(clamd)}")
            print(shlex.join(lane_cmd(
                first, [], prepare_only=True, lane_label=clamd_label, lane_extra=clamd_extra
            )))
            print(shlex.join(lane_cmd(first, clamd, lane_label=clamd_label, lane_extra=clamd_extra)))
            print(shlex.join(lane_cmd(first, [], prepare_only=True, lane_label=clamd_label)))
        for lane in to_tear_down:
            print(shlex.join(lane_cmd(lane, [], teardown=True, lane_label=alone_label)))
        for suite in exclusive:
            print(shlex.join(lane_cmd(first, [suite], True, alone=True, lane_label=alone_label)))
            print(shlex.join(lane_cmd(first, [suite], alone=True, lane_label=alone_label)))
        for sent in slices:
            print(
                f"SLICE {sent['box']} -> {sent['slice_ref']} (evidence over "
                f"{sent['status_ref']}): {json.dumps(sent, sort_keys=True)}"
            )
        if transport is not None:
            print(
                f"PUBLISH (dry run, nothing pushed): a real run publishes candidate "
                f"{remote['candidate']} on {remote['candidate_ref']}, asks every box for a fresh "
                "declaration, publishes each slice above on its slice ref, and deletes them all "
                "after the wait"
            )
        print(
            f"\n{len(suites) + len(extras)} suite(s) over {len(plans)} lane(s); "
            f"endpoint {cfg['url']}; state dir {cfg['state_dir']}; nothing was run"
        )
        return 0

    removed = freed = 0
    for pytest_root in pytest_temp_roots():
        root_removed, root_freed = reclaim_pytest_temp_dirs(
            pytest_root, min_age_seconds=pytest_tmp_age
        )
        removed += root_removed
        freed += root_freed
    reclaimed_note = f"pytest temp preflight: reclaimed {removed} directories, {freed} bytes"
    print(reclaimed_note)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    state = Path(cfg["state_dir"])

    # The slices are written before the first lane starts, so the other boxes
    # can work while this one does; the deadline counts from here.
    for sent in slices:
        slice_file = LOG_DIR / f"gates-{stamp}-slice-{sent['box']}.json"
        slice_file.write_text(json.dumps(sent, sort_keys=True, indent=2) + "\n")
        print(
            f"SLICE {sent['box']}: {slice_file} is to be published on {sent['slice_ref']}; "
            f"its evidence ({sent['status_ref']}) is read from {remote['paths'][sent['box']]}"
        )
    if transport is not None:
        # The declarations this plan was cut from, kept beside the slices.
        for name, text in (fetched or {}).items():
            (LOG_DIR / f"gates-{stamp}-declaration-{name}.json").write_text(text)
        try:
            transport.publish_slices(slices)
        except _load_remote().TransportRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3
    remote_deadline = time.monotonic() + remote["timeout"] if slices else None

    # The lanes this run has brought down and not brought up since. The re-run
    # pass empties the box as the alone phase does, wherever that phase ran,
    # and a lane it already took down needs no second teardown.
    brought_down: set[int] = set()

    def run_wave(wave, prepare_only=False, *, alone=False, teardown=False, lane_label=label,
                 lane_extra=None):
        """Launch every lane in `wave` at once and wait for all of them.

        Waiting for ALL of them, never for the first, is half of the barrier:
        a lane that finished early must not be torn down — and must not start
        creating a stack for the next thing — while another lane is running a
        browser suite against a bridge docker is about to move.
        """
        if not wave:
            return []
        started = []
        written: list[Path] = []
        decl_dir = session_decl_dir()
        active = [lane for lane, _ in wave]
        wave_shares = worker_shares(active, sum(shares.values()))
        wave_cpu = worker_shares(active, sum(cpu_shares.values()))
        if args.workers and not teardown and any(args.workers > share for share in wave_shares.values()):
            raise Refused("requested -n exceeds this wave's total worker budget")
        try:
            for lane, paths in wave:
                if decl_dir is not None:
                    try:
                        written.append(write_lane_declaration(decl_dir, lane))
                    except (OSError, ValueError):
                        pass
                log = state / f"lane-p{lane}-{lane_label}.log"
                what = (
                    "teardown" if teardown else "prepare" if prepare_only else f"{len(paths)} suite(s)"
                )
                print(f"lane {lane}: {what} -> {log}")
                cmd = lane_cmd(
                    lane, paths, prepare_only, alone=alone, teardown=teardown,
                    lane_label=lane_label, wave_shares=wave_shares, wave_cpu=wave_cpu,
                    lane_extra=lane_extra,
                )
                started.append((lane, paths, log, time.monotonic(), subprocess.Popen(cmd, cwd=str(ROOT))))
            out = []
            for lane, paths, log, t0, proc in started:
                rc = proc.wait()
                if teardown and rc == 0:
                    brought_down.add(lane)
                else:
                    brought_down.discard(lane)
                out.append(
                    {
                        "lane": lane,
                        "planned": [] if prepare_only or teardown else paths,
                        "exit": rc,
                        "wall": time.monotonic() - t0,
                        "log": str(log),
                        "suites": read_lane_log(log),
                        "tree": read_lane_tree(log),
                    }
                )
            return out
        finally:
            for path in written:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    # THE ALONE PHASE, run below only once every lane of this box has finished
    # — in split mode before the wait for the other boxes, otherwise after the
    # vision wave — and "finished" is not "gone": a finished lane's
    # stack still holds its ports, and Gate OB's own deployments need some of
    # them. So this run's other lanes come down first, with their volumes; a
    # lane that will not come down means the phase does not start at all, and
    # its suites read UNMEASURED. Then one suite at a time on the first lane,
    # each behind a barrier pass of its own, because that lane may have just
    # run Gate RS or Gate DX at its tail and its vault then holds the restore
    # marker or the demonstration — on 2026-09-23 that marker left the lane's
    # recreated webserver crash-looping and fourteen cases read nginx 502s.
    # Both passes carry LANE_ALONE, so the lane itself refuses while any other
    # gate lane is running, including one this run did not start and so left
    # alone. A lane that cannot be prepared leaves the suite UNMEASURED.
    def alone_phase():
        """Run the exclusive suites alone on the first lane; return the report's note."""
        down = run_wave(
            [(lane, []) for lane in to_tear_down], teardown=True, lane_label=alone_label
        )
        stuck = [r for r in down if r["exit"] != 0]
        if stuck:
            for r in stuck:
                print(
                    f"REFUSED: lane {r['lane']} did not come down (exit {r['exit']}, "
                    f"see {r['log']}), so the alone phase does not start",
                    file=sys.stderr,
                )
            stuck_lanes = ", ".join(f"lane {r['lane']}" for r in stuck)
            for suite in exclusive:
                lane_results.append(
                    {
                        "lane": first,
                        "planned": [suite],
                        "exit": stuck[0]["exit"],
                        "wall": 0.0,
                        "log": stuck[0]["log"],
                        "suites": [],
                    }
                )
            return f"alone phase NOT started: {stuck_lanes} did not come down"
        for suite in exclusive:
            prepared = run_wave(
                [(first, [suite])], prepare_only=True, alone=True, lane_label=alone_label
            )[0]
            if prepared["exit"] != 0:
                said = bringup_refusal(Path(prepared["log"]))
                print(
                    f"REFUSED: lane {first} could not be prepared for {suite} "
                    f"(exit {prepared['exit']}, see {prepared['log']})"
                    + (f": {said}" if said else ""),
                    file=sys.stderr,
                )
                lane_results.append({**prepared, "planned": [suite]})
                continue
            finished = run_wave([(first, [suite])], alone=True, lane_label=alone_label)
            for result in finished:
                complete_lane_run(
                    state, result["lane"], run_id, Path(result["log"]), result["exit"]
                )
            lane_results.extend(finished)
        return (
            f"alone phase on lane {first}"
            + (", beside the other boxes' slices" if alone_early else "")
            + ", after bringing down "
            + (", ".join(f"skykeep-p{lane}" for lane in to_tear_down) or "nothing")
        )

    # THE RE-RUN PASS, run below in two halves. It re-runs, once, every suite
    # whose verdict a re-run can cure (`rerun_suites`, `rerun_reason`): one that
    # FAILED, and one UNMEASURED because it was refused before collection, its
    # cases only errored, or the OOM killer took it on the lane's own evidence.
    # Alone, because whatever the packed wave did to it — a memory race, a
    # bridge moving under a browser, the endpoint swapping models for a
    # neighbour — is what it would run into again beside the other lanes; once,
    # because a suite that is genuinely red or leaks must not spin. This is the
    # pass the integrator used to run by hand after every batch. A timeout and
    # a 137 the lane could not prove was the OOM killer's are never re-run, nor
    # is a suite no lane reported. The alone phase's own suites are not re-run:
    # they had the box to themselves already. Past `--rerun-limit`, which counts
    # the re-runs of both halves, the wave itself is broken, and nothing more is
    # re-run: those verdicts stand.
    #
    # The first half re-runs this box's own non-clean suites in split mode, as
    # soon as every lane of this box and its alone phase have finished, while
    # the other boxes' slices are still out (`beside_slices`, the alone phase's
    # own rule): alone is alone on THIS box, the other boxes are other hosts,
    # and a re-run adds endpoint load like any lane but swaps no model (Batch44:
    # four of the six re-runs, about 990 s in all, were this box's, run after the
    # last answer). The second half, after every other phase and the wait for
    # every box, re-runs whatever is left: each other box's, the vision wave's,
    # and every one in a run with no box.
    #
    # Each half empties the box exactly as the alone phase does — this run's
    # other lanes down with their volumes first (none already down), a lane
    # that will not come down meaning no re-run in that half — and then gives
    # each suite a barrier pass and a suite pass on the first lane, both with
    # LANE_ALONE, under a label of its own: a lane's barrier pass truncates its
    # log, and that log is the evidence of the re-run (and a duration measured
    # alone, which the scheduler may harvest). Gate RS (and DX) go last in each
    # half, in `gate_distribute`'s tail order; anything after a re-run Gate RS
    # on that lane — the vision wave, the second half — has a barrier pass of
    # its own first, which resets a vault holding the restore marker.
    rerun_label = f"{label}-rerun"
    rerun_notes: list[str] = []

    def rerun_pass(when):
        """Re-run each suite still owed its one re-run alone; note it for the report."""
        to_rerun = rerun_suites(
            lane_results, distribute.POISONS_ITS_STACK, distribute.TAIL_ORDER, skip=set(exclusive)
        )
        if not to_rerun:
            return
        spent = len({suite for res in lane_results if res.get("rerun") for suite in res["planned"]})
        if len(to_rerun) > args.rerun_limit - spent:
            note = (
                f"re-run pass NOT run: {len(to_rerun)} non-clean suite(s) exceed "
                f"--rerun-limit {args.rerun_limit}"
                + (f" with {spent} re-run already" if spent else "")
                + " — a wave this red is broken, not flaky, so "
                + ("their wave verdicts stand" if spent else "every wave verdict stands")
            )
            print(note)
            rerun_notes.append(note)
            return
        still_up = [lane for lane in run_lanes if lane != first and lane not in brought_down]
        down = run_wave([(lane, []) for lane in still_up], teardown=True, lane_label=rerun_label)
        stuck = [r for r in down if r["exit"] != 0]
        if stuck:
            for r in stuck:
                print(
                    f"REFUSED: lane {r['lane']} did not come down (exit {r['exit']}, "
                    f"see {r['log']}), so the re-run pass does not start",
                    file=sys.stderr,
                )
            stuck_lanes = ", ".join(f"lane {r['lane']}" for r in stuck)
            # Their verdicts stand: a red stays FAILED, a refusal, an error or
            # an eviction UNMEASURED.
            rerun_notes.append(
                f"re-run pass NOT started: {stuck_lanes} did not come down; "
                f"{len(to_rerun)} non-clean suite(s) not re-run"
            )
            return
        rerun_notes.append(
            f"re-run pass on lane {first}, alone, once each, {when}: "
            f"{len(to_rerun)} non-clean suite(s): {', '.join(to_rerun)}"
        )
        for suite in to_rerun:
            suite_label = f"{rerun_label}-{Path(suite).stem}"
            print(f"RE-RUN alone, once: {suite}")
            prepared = run_wave(
                [(first, [suite])], prepare_only=True, alone=True, lane_label=suite_label
            )[0]
            if prepared["exit"] != 0:
                said = bringup_refusal(Path(prepared["log"]))
                print(
                    f"REFUSED: lane {first} could not be prepared to re-run {suite} "
                    f"(exit {prepared['exit']}, see {prepared['log']})"
                    + (f": {said}" if said else ""),
                    file=sys.stderr,
                )
                lane_results.append({**prepared, "planned": [suite], "rerun": True})
                continue
            finished = run_wave([(first, [suite])], alone=True, lane_label=suite_label)
            for result in finished:
                complete_lane_run(
                    state, result["lane"], run_id, Path(result["log"]), result["exit"]
                )
            lane_results.extend({**result, "rerun": True} for result in finished)

    # THE BARRIER. Every lane's deployment is up and answering before any suite
    # anywhere starts. A lane creating containers moves the host's docker
    # bridges, and a browser suite already running on a NEIGHBOURING lane sees
    # `net::ERR_NETWORK_CHANGED` and locator timeouts — reds that look like the
    # product and are the schedule. It is also the one pass in which a lane
    # whose vault may not be reused (restore marker, demonstration, too old) is
    # brought down with its volumes and rebuilt: no suite is running anywhere
    # yet, so a stack going down disturbs nobody.
    barrier_started = time.monotonic()
    barrier = run_wave(plans, prepare_only=True)
    refused = [r for r in barrier if r["exit"] != 0]
    if refused:
        for r in refused:
            said = bringup_refusal(Path(r["log"]))
            print(
                f"REFUSED: lane {r['lane']} could not bring its stack up "
                f"(exit {r['exit']}, see {r['log']})" + (f": {said}" if said else ""),
                file=sys.stderr,
            )
        return 3
    print(f"barrier: {len(plans)} lane(s) up and healthy in {time.monotonic() - barrier_started:.0f}s")

    lane_results = run_wave(plans)
    for result in lane_results:
        complete_lane_run(
            state, result["lane"], run_id, Path(result["log"]), result["exit"]
        )

    # THE RESTART WAVE. `run_wave` has waited for EVERY lane, so no browser
    # suite is running anywhere, and these suites may move the docker bridge:
    # restart a container, stop and start one, or have their lane create and
    # remove one in front of them. Each lane gets a barrier pass of its own
    # first — it may have just run Gate RS at its tail, a suite pass never
    # resets, and a lane only this wave uses has no stack yet — and no suite
    # starts until every one of those has exited, because a lane reset beside
    # a running suite moves the bridge exactly as the suites do. A lane that
    # cannot be prepared leaves its own suites UNMEASURED; the others run.
    #
    # In split mode it runs BEFORE the wait for the other boxes below, while
    # they may still be mid-suite. That is safe on both counts the wait exists
    # for. The docker bridge a restart moves is this host's, and no other
    # box's browser suite runs on it. The endpoint: `split_plan` packs this
    # wave over this box's lanes only and, under a ceiling, over this box's
    # endpoint lanes only, which the suite wave above has just released, so
    # this box never runs more endpoint lanes at once than its share. The
    # vision wave is the one that must wait for every box, and it still does.
    restart_note = None
    if restart_plans:
        prepared = run_wave(restart_plans, prepare_only=True, lane_label=restart_label)
        planned = dict(restart_plans)
        ready = [r["lane"] for r in prepared if r["exit"] == 0]
        unready = [r["lane"] for r in prepared if r["exit"] != 0]
        for r in prepared:
            if r["exit"] == 0:
                continue
            said = bringup_refusal(Path(r["log"]))
            print(
                f"REFUSED: lane {r['lane']} could not be prepared for the restart wave "
                f"({', '.join(planned[r['lane']])}; exit {r['exit']}, see {r['log']})"
                + (f": {said}" if said else ""),
                file=sys.stderr,
            )
            lane_results.append({**r, "planned": planned[r["lane"]], "suites": []})
        restart_note = (
            "restart wave after every lane's suites, on "
            + (", ".join(f"lane {lane}" for lane in ready) or "no lane")
            + ("; NOT started on " + ", ".join(f"lane {lane}" for lane in unready) if unready else "")
        )
        finished = run_wave(
            [(lane, planned[lane]) for lane in ready], lane_label=restart_label
        )
        for result in finished:
            complete_lane_run(
                state, result["lane"], run_id, Path(result["log"]), result["exit"]
            )
        lane_results.extend(finished)

    # THE OTHER BOXES, before the vision wave. Each slice's evidence is judged
    # on its own, and a box that is absent, late or refused leaves its whole
    # slice UNMEASURED: no box's green stands in for another's silence. The
    # wait comes HERE because the endpoint holds one model: until every box
    # has answered or run out of time, a box may be mid-suite on the think
    # model, and a vision wave now would evict it (batch24, over two machines).
    boxes_note = None
    alone_note = None
    if slices:
        # THE ALONE PHASE, BESIDE THE OTHER BOXES' SLICES (`alone_early`). Every
        # lane of this box has exited above, so it is alone on this box, which
        # is all Gate OB needs; the slices are still out on other hosts, whose
        # evidence waits on their refs and is read below however long this takes.
        if alone_early:
            alone_note = alone_phase()
        # THE RE-RUN PASS, its first half (`rerun_pass`): this box's own
        # non-clean suites, alone on this box now that its lanes and its alone
        # phase are done, while the other boxes are still on their slices.
        if beside_slices:
            rerun_pass("beside the other boxes' slices")
        refreshing = {}
        if transport is not None:
            refreshing["refresh"] = transport.evidence_refresher(slices, remote["paths"], EVIDENCE_MAX_BYTES)
        answers = wait_for_evidence(slices, remote["paths"], remote_deadline, **refreshing)
        if transport is not None:
            # Every box has answered or run out of time: nothing reads the
            # candidate or a slice any more.
            for left in transport.cleanup():
                print(f"WARNING: {left}", file=sys.stderr)
        for sent in slices:
            text, late = answers[sent["box"]]
            result = judge_box_evidence(sent, text, timed_out=late)
            distribute.record_box_durations(
                cfg["state_dir"], sent["box"], run_id, result["endpoint"], result["measured"]
            )
            lane_results.append(result)
        boxes_note = f"{len(slices)} remote slice(s) of candidate {remote['candidate']}: " + ", ".join(
            f"{sent['box']} ({len(slice_suites(sent))} suite(s))" for sent in slices
        )

    # THE VISION WAVE. `run_wave` has waited for EVERY lane, and in split mode
    # every box has answered or timed out above, so no suite of this run is
    # running anywhere and nothing holds the endpoint on the think model. The first lane gets a barrier pass of its own — it may have
    # just run Gate RS or Gate DX at its tail, a suite pass never resets, and
    # when every named suite is a vision suite no barrier has run at all —
    # then the vision suites, alone in the wave. The lane names the vision
    # model to that suite pass and to no other (`gate_lane.sh`), so the
    # posture guard loads it before the first case and no suite of the wave
    # above was ever asked to prove it. A lane that cannot be prepared leaves
    # them UNMEASURED.
    vision_note = None
    if vision:
        prepared = run_wave([(first, [])], prepare_only=True, lane_label=vision_label)[0]
        if prepared["exit"] != 0:
            said = bringup_refusal(Path(prepared["log"]))
            print(
                f"REFUSED: lane {first} could not be prepared for the vision wave "
                f"({', '.join(vision)}; exit {prepared['exit']}, see {prepared['log']})"
                + (f": {said}" if said else ""),
                file=sys.stderr,
            )
            vision_note = f"vision wave NOT started: lane {first} could not be prepared"
            lane_results.append({**prepared, "planned": vision, "suites": []})
        else:
            vision_note = (
                f"vision wave on lane {first}, after every lane's suites: {', '.join(vision)}"
            )
            finished = run_wave([(first, vision)], lane_label=vision_label)
            for result in finished:
                complete_lane_run(
                    state, result["lane"], run_id, Path(result["log"]), result["exit"]
                )
            lane_results.extend(finished)

    # THE CLAMD WAVE. The standard lanes hold clamd at zero replicas, so the
    # real engine is measured here and nowhere else in the run: the first lane
    # is prepared again with the daemon (its first start loads signatures for
    # minutes; the lane waits for the healthcheck), the real-engine suites run
    # alone in the wave, and the lane is then prepared once more WITHOUT the
    # daemon, so nothing after this wave runs beside a clamd no lane budgeted.
    # A lane that cannot start the daemon leaves the suites UNMEASURED and the
    # run red: a run that never dialled clamd must not read as a pass.
    clamd_note = None
    if clamd:
        prepared = run_wave(
            [(first, [])], prepare_only=True, lane_label=clamd_label, lane_extra=clamd_extra
        )[0]
        if prepared["exit"] != 0:
            said = bringup_refusal(Path(prepared["log"]))
            print(
                f"REFUSED: lane {first} could not be prepared for the clamd wave "
                f"({', '.join(clamd)}; exit {prepared['exit']}, see {prepared['log']})"
                + (f": {said}" if said else ""),
                file=sys.stderr,
            )
            clamd_note = f"clamd wave NOT started: lane {first} could not start clamd"
            lane_results.append({**prepared, "planned": clamd, "suites": []})
        else:
            clamd_note = (
                f"clamd wave on lane {first}, the real malware engine: {', '.join(clamd)}"
            )
            finished = run_wave([(first, clamd)], lane_label=clamd_label, lane_extra=clamd_extra)
            for result in finished:
                complete_lane_run(
                    state, result["lane"], run_id, Path(result["log"]), result["exit"]
                )
            lane_results.extend(finished)
            restored = run_wave([(first, [])], prepare_only=True, lane_label=clamd_label)[0]
            if restored["exit"] != 0:
                clamd_note += (
                    f"; lane {first} could NOT be put back without clamd "
                    f"(exit {restored['exit']}, see {restored['log']})"
                )
        print(clamd_note)

    # THE ALONE PHASE, after the vision wave, unless it has already run beside
    # the other boxes' slices (above, `alone_early`).
    if exclusive and not alone_early:
        alone_note = alone_phase()

    # THE RE-RUN PASS, its second half (above, `rerun_pass`): every suite still
    # owed its one re-run once every verdict this run will report is in — each
    # other box's, the vision wave's, and every one in a run with no box.
    rerun_pass("after every other phase")

    header = [
        f"gate run {label}",
        reclaimed_note,
        f"endpoint {cfg['url']} (bearer from {cfg['secret_file']})",
        f"embed={cfg['embed']} think={cfg['think']} ingest={cfg['ingest'] or '<none>'}",
        f"{len(plans)} lane(s) from {first}, MemoryMax={args.memory_per_lane} each ({why})",
        (
            f"{args.workers} xdist worker(s) per lane, --dist loadfile; "
            f"{len(serial)} suite(s) held serial"
            if args.workers
            else "one pytest process per lane (no -n)"
        ),
        *([restart_note] if restart_note else []),
        *([vision_note] if vision_note else []),
        *([clamd_note] if clamd_note else []),
        *([alone_note] if alone_note else []),
        *([boxes_note] if boxes_note else []),
        *rerun_notes,
        "",
    ]
    body, green = summarise(lane_results, accepted)
    report = "\n".join(header + body)
    print(report, flush=True)

    out = LOG_DIR / f"gates-{stamp}.log"
    out.write_text(report + "\n")

    # THE BATCH SPLIT, last: every lane of this run is idle, and the report
    # above is already printed and written — a split takes the red suite
    # several times over, and the verdict does not wait for it. Only a red that
    # reproduced alone is split (`reds_alone`), and only when `--batch-base`
    # names the trunk tip the batch was merged onto. It reports which seam
    # carries the red and which could land without it, in lines appended to the
    # report; the verdict is `summarise`'s alone and is red either way. One
    # deadline covers every suite's split (`--batch-split-budget`).
    # Probes run one at a time (a neighbour's bring-up makes reds), so the
    # split is handed one lane: this run's first.
    split_deadline = time.monotonic() + (args.batch_split_budget or 0)
    split_lanes = (run_lanes or [first])[:1]

    def split_one(suite):
        return _load_batch_split().split_suite(
            ROOT, args.batch_base, "HEAD", suite, split_lanes, LOG_DIR,
            deadline=split_deadline,
        )

    split_lines = batch_split_lines(
        lane_results, args.batch_base, args.batch_split_limit, split_lanes, split_one
    )
    if split_lines:
        addendum = "\n".join(split_lines)
        print(addendum)
        with out.open("a") as written:
            written.write(addendum + "\n")
    print(f"\nreport written to {out}")
    return 0 if green else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
