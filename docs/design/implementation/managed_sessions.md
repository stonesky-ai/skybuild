# Bounded sessions and the Wonko merge worker

Owner instruction, 2026-10-10: save this setup and start to use it. Use ASD-STE100 for specifications and communications. This instruction permits the setup and isolated process tests. It does not select a bundle or remove a publication gate.

## Operating rules

Run each new analysis session in a separate systemd user service. Set its working directory to the exact checkout. Start Codex with the same checkout as its working directory. Serena can then use `--project-from-cwd` without selecting the broad `/home/kevin/my_code` workspace. All child processes, including Pyright and Node, stay in the service cgroup unless a command explicitly starts a different service. Do not start child services from an analysis session.

Use `scripts/managed_session.py` for new sessions. The default limits are 3 GiB for `MemoryHigh`, 4 GiB for `MemoryMax`, no swap, and one hour for the runtime. An explicit runtime cannot exceed eight hours. The analysis profile uses `Nice=10` and `OOMScoreAdjust=300`. The merge profile uses `Nice=0` and `OOMScoreAdjust=0`. These differences favor merge work when CPU or memory is under pressure. They do not give the merge process an unconditional memory guarantee.

Use one private state directory on each host: `/home/kevin/my_code/skybuild-managed-state`. All launchers on that host must use this directory. The directory must have mode 0700 and the current user as its owner. Do not select a second state directory to evade an occupied slot or a memory check. The launcher keeps an 8 GiB host reserve. Admission also accounts for the full memory limits of runs with no confirmed cleanup record. A failed or incomplete launch keeps its record. Inspect that record and the physical service before another attempt. Do not retry an unknown operation.

Run merge work on Wonko. Use the merge profile for CPU commands. Do not start Codex, Serena, Pyright, or another model in the merge service. The launcher permits one merge run at a time. It records the exact `refs/heads/dev-NNN` target and requires the target in the final command arguments. The command must validate that target and enforce the existing review, bundle, gate, and publication rules. The helper does not supply a distributed publication lock. The operator must coordinate any other publisher on another host.

The service uses `ExitType=main`, `RemainAfterExit=no`, and `KillMode=control-group`. systemd stops the remaining children when the main command exits. It sends SIGKILL after the ten-second stop timeout. `OOMPolicy=kill` stops the full service on an OOM event. `ExecStopPost` writes a small cleanup record after systemd stops the children. The record contains the memory peak, service result, and remaining process count. It contains no command output or credentials. The launcher releases the merge slot only after it receives a terminal service result and verifies this record.

A lost launch reply, a missing cleanup record, or a nonterminal service state keeps the merge slot occupied. A launcher failure does not permit another merge. The service has its own runtime limit and can finish after the launcher exits. Do not repeat an external publication to recover a missing response. Reconcile the target and retained evidence.

## Commands

Run coding preflight on the executor with `--expected-host wonko`. Check the user bus on that host before launch. Use the exact checkout and a host-watch sample from that host. The host watcher must run for the full job duration plus at least two sample intervals. Keep one watcher for the host; do not create a second watcher to replace uncertain ownership.

For an analysis session, use the installed absolute Codex path and the exact checkout:

```sh
rtk proxy /absolute/checkout/scripts/project_python \
  /absolute/checkout/scripts/managed_session.py \
  --profile analysis --expected-host wonko --checkout /absolute/checkout \
  -- /usr/local/bin/codex -C /absolute/checkout
```

For a merge run, pass the exact target and the existing reviewed integration command. Keep `--base dev-NNN` in that command. Do not infer the branch from a local checkout. Do not use a custom gate for publication. Do not launch a publication until its bundle, exact source, review evidence, and required gate are ready.

## Host limits and rollout

Read-only Wonko checks found systemd 259, cgroup v2, a working user bus, and about 22.6 GiB available at the time of the check. The `memory.low` values of the user-service ancestors were zero. Password-free sudo was unavailable. This user-level setup therefore does not claim effective `MemoryLow` protection. Effective protection needs a separately configured parent reservation or a privileged service configuration. Other programs outside the managed state directory can still use host memory.

Do not move or terminate another session. Existing sessions keep their current lifecycle. Apply this setup to new runs. The existing `JobUnitManager` is unchanged. It retains completed units for its own observation contract. Do not use that adapter for analysis sessions. Use `managed_session.py`, which has a separate durable cleanup hook and does not need a retained active unit. Existing service properties do not change with a source edit.

The service wrapper uses only the Python standard library. Its systemd cleanup hook uses `/usr/bin/python3` so cleanup does not depend on a package install or a project environment. Use the checkout's `scripts/project_python` for project tests and commands.
