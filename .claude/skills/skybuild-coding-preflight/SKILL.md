---
name: skybuild-coding-preflight
description: Combine SkyBuild checkout, path, local-ref, runtime-import and cache checks when resuming work, changing checkouts, or recovering from missing paths, unfetched refs or environment errors. Does not execute the dependent task.
---

# Coding preflight

Use the standard-library helper before dependent work when entering an unfamiliar checkout or when a demonstrated prerequisite error needs correction. Select only relevant checks; do not run every check before every read. CodeGraph remains the first code-navigation tool for an indexed checkout.

```sh
rtk proxy python3 scripts/coding_preflight.py --checkout /absolute/task/checkout \
  --require docs/design/architecture.md --discover '.claude/skills/*/SKILL.md' \
  --ref refs/remotes/origin/dev-003 \
  --python /absolute/task/checkout/.venv/bin/python \
  --uv-cache /absolute/task/checkout/.uv-cache
```

Replace the example ref and paths with the assignment's actual prerequisites. The helper combines root/origin verification, explicit-file checks, tracked filename discovery, local-ref resolution, cache admission and an exact-checkout source import. It prints at most 20 discovered paths per glob. `--require` also accepts an explicitly provided absolute evidence path, including a transient handoff file. Use `--discover` for uncertain tracked filenames before reading them. Git-ignored or untracked files require explicit `--require` paths.

Run this helper with system `python3`: its checks use only the standard library and must work before project dependencies or uv caches are ready. Run project commands afterward through `scripts/project_python`. `--python` checks an existing interpreter without installing or syncing dependencies; it replaces inherited `PYTHONPATH` with this checkout's source and verifies the imported package location. The helper does not prove the environment matches the lockfile; `project_python` retains that responsibility. Cache admission checks permissions on the existing directory or nearest parent; a later filesystem or mount change can still invalidate it.

Exit 2 means a prerequisite failed. Stop dependent commands. For an unavailable `refs/...` name, inspect the clone's fetch coverage and fetch only the required ref under existing authority; the helper never fetches. For a missing interpreter, prepare the environment through the project runner under existing authority. For an unwritable cache, select a task-owned writable `UV_CACHE_DIR`. Missing transient evidence requires reconciliation or a fresh authorized qualification; never replay external effects to recreate evidence. After the cause changes, rerun the affected preflight checks.

Preflight success does not authorize Git writes, API mutations, service starts or deployment. Before an unfamiliar API mutation, inspect both the API input model and the action-specific `Store` validator. For example, `create_task` rejects unsupported initial phases even if the generic input model accepts them. Before HTTP qualification, inspect actual compose binding, transport and declared routes; a known loopback binding is not reachable through the remote hostname and port. Do not guess a health endpoint or downgrade HTTPS.

For patch-context failures, read the current bounded source and make a smaller context patch. For repeated nested shell/Python quoting errors, put the logic in a short heredoc or task-owned script. These failures do not justify rerunning unchanged commands or adding a universal quoting wrapper.
