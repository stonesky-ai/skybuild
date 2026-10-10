---
name: skybuild-managed-sessions
description: Start owner-authorized SkyBuild analysis sessions or CPU merge work in bounded services on Wonko. Use for Serena and Pyright lifecycle cleanup and serialized merge or gate commands.
---

# Managed SkyBuild sessions

The owner approved this setup on 2026-10-10 and asked to start to use it. Use ASD-STE100 for specifications and communications. Read the operational contract in `docs/design/implementation/managed_sessions.md` from the reviewed runtime checkout.

- Use `/home/kevin/my_code/skybuild-managed-runtime-20261010` on Wonko for the installed runner. Verify its deployed Git head against the retained review before use. Run coding preflight on Wonko with `--expected-host wonko`. Do not use a Jeltz resource sample for a Wonko job.
- Use one host state directory: `/home/kevin/my_code/skybuild-managed-state`. Do not select a different state directory to bypass a merge slot or memory check. Check the fresh host-watch sample. Its remaining lifetime must exceed the full job duration plus two sample intervals.
- Start new analysis sessions with `scripts/managed_session.py --profile analysis`. Set `--checkout` and the Codex `-C` argument to the exact same checkout. Serena's `--project-from-cwd` must resolve to that checkout. Do not start a child service that escapes the session cgroup. Existing sessions do not move into the new service automatically.
- Start CPU merge or gate commands with `--profile merge --target-ref refs/heads/dev-NNN`. Supply the actual authorized ref, not a default from this example. Include and check the same target in the final command arguments. Do not start a model, Codex, Serena, or Pyright in this profile. Preserve the required review, frozen bundle, gate, and publication controls.
- Defaults are `MemoryHigh=3 GiB`, `MemoryMax=4 GiB`, no swap, and a one-hour runtime. Keep the 8 GiB reserve and account for all active managed jobs. Adjust a limit only with fresh resource admission and appropriate peak evidence. Keep the runtime explicit for bounded jobs.
- A service stops all remaining children when its main command exits. The cleanup hook saves the result and memory peak after child termination. Missing or invalid evidence retains merge ownership and memory exposure. Inspect the physical service and publication state before a new attempt. Do not replay an external effect or clear an unknown slot to permit a retry.
- This user-level setup does not guarantee effective `MemoryLow` protection. Wonko's parent reservations were zero, and password-free sudo was unavailable. Memory caps and admission reduce pressure; programs outside this setup can still use host memory.

Use the checkout's `scripts/project_python` for project tests. The runner and its cleanup hook use only the standard library; the cleanup hook uses `/usr/bin/python3` to avoid dependency preparation during shutdown. Do not change the existing `JobUnitManager` completion contract as part of this operational setup.
