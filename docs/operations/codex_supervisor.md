# Local Codex session recovery from cron

`scripts/skybuild_codex_supervisor.py` is a bounded local recovery aid for one
explicitly prepared Codex session. It does not choose a SkyBuild task or grant
model, billing, deployment, or publication authority. Do not enable it as a
general SkyBuild worker launcher. The later execution-control admission design
in [architecture section 8](../design/architecture.md#8-controlled-execution-invariants)
still governs automatic task execution.

The script holds an exclusive `flock` while it owns a child. Each cron tick also
checks the kernel process table for a live Codex process in the checkout. If
one exists, it waits. It pins prompt files by SHA-256, requires an explicit
UTC deadline within eight hours, requires at least 10 GiB of available memory
before launch, stops its recorded process group if available memory falls below
8 GiB, and permits at most one resume of the recorded Codex session.
GNU `timeout` enforces the deadline even if the supervisor process dies. A
missing session ID, changed prompt, failed turn, or uncertain state parks the
request for human inspection. A completed request never runs again.
The resume command applies only to a recorded `codex exec` session. It cannot
transparently take over the current interactive Codex app thread. It never uses
`--last`, which could select another session.

Before enabling cron on a host, run `probe` while a known Codex process is
active in the checkout and confirm its PID appears. The process table visible
inside a sandbox may differ from the host's. A missing PID means this guard
cannot protect that session; leave cron disabled until the host process view is
understood. The included test proves detection of a visible test process, not
visibility of the interactive Codex app thread.

```bash
python3 /home/kevin/my_code/skybuild/scripts/skybuild_codex_supervisor.py probe \
  --state-dir /home/kevin/my_code/skybuild-codex-run \
  --checkout /home/kevin/my_code/skybuild
```

## Prepare one run

Use a dedicated state directory outside the checkout. Store prompts there or
in another private path. The initial prompt must name the task, scope, source
revision, allowed effects, checkpoint location, and stop deadline. The resume
prompt must tell Codex to inspect the worktree, test and commit evidence, live
processes, and external effects before continuing only unfinished work. It must
not repeat a deployment, publication, or other effect on uncertain evidence.

```bash
install -d -m 700 /home/kevin/my_code/skybuild-codex-run
python3 /home/kevin/my_code/skybuild/scripts/skybuild_codex_supervisor.py prepare \
  --state-dir /home/kevin/my_code/skybuild-codex-run \
  --checkout /home/kevin/my_code/skybuild \
  --prompt-file /home/kevin/my_code/skybuild-codex-run/initial-prompt.txt \
  --resume-file /home/kevin/my_code/skybuild-codex-run/resume-prompt.txt \
  --deadline-utc "$(date -u -d '+8 hours' '+%Y-%m-%dT%H:%M:%SZ')" \
  --request-id skybuild-overnight-001
```

Prepare refuses an active request and a persistent `STOP` file. A new run
requires a new explicit `prepare` after prior run reaches a terminal state.
No default prompt exists.

## Cron entry

After the script is on this box at the path below, install this entry in the
user's crontab. It starts no model until a valid request has been prepared.

```cron
* * * * /usr/bin/python3 /home/kevin/my_code/skybuild/scripts/skybuild_codex_supervisor.py tick --state-dir /home/kevin/my_code/skybuild-codex-run 2>&1 | /usr/bin/logger -t skybuild-codex-supervisor
```

Run one manual `tick` only when ready to begin the prepared session. Check
state without launching anything:

```bash
python3 /home/kevin/my_code/skybuild/scripts/skybuild_codex_supervisor.py status \
  --state-dir /home/kevin/my_code/skybuild-codex-run
```

To stop future launch or resume, create
`/home/kevin/my_code/skybuild-codex-run/STOP`. The active supervisor then
terminates only its recorded process group. Cron notices the file on its next
tick. Inspect `request.json` and the bounded, private `codex.jsonl` before
preparing another request. Do not remove the lock file while a process may
hold it. Do not remove a `STOP` file until its effect has been inspected.

This helper cannot prove subscription billing protection, provider quota,
approval expiry beyond the prepared deadline, or that an unreported external
effect did not happen. The operator must qualify those conditions before
preparing a model run. A process table check prevents an obvious duplicate; it
does not make an uncertain external effect safe to repeat.
