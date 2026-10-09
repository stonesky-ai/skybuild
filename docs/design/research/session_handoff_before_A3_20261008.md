# Historical handoff before architecture A3

Archived context only. The canonical [session handoff](../session_handoff.md), [architecture](../architecture.md) and current owner instructions supersede every task/order/tool-state statement below. Original dated-plan links now point to the active architecture for navigation; they do not make this historical text current.

# SkyBuild planning session handoff

Saved 2026-10-08 14:09 CDT for relaunch from `/home/kevin/my_code`, so the next session can work with sibling `skybuild` and `skykeep` checkouts.

## Resume here

Read this handoff and [current governing architecture](../architecture.md). Read the applicable AGENTS.md in the new workspace and each repository before edits. The owner explicitly says **planning only: do not start building anything yet**. Do not start/restart workers, services, scheduled launchers or aragog; do not migrate live data or file implementation todos merely because they appear in the plan.

The new product name is **SkyBuild**. The dedicated repository is `https://github.com/stonesky-ai/skybuild`, cloned by the owner into `/home/kevin/my_code/skybuild`. The canonical plan now lives there under `docs/design/`. Do not reuse or rename Dark Build Factory or SkyTrends: the repository choice is settled.

## Repository transfer completed

The owner authorized cloning SkyBuild, moving the current planning document and committing it as the first file. After this session's network attempt failed, the owner cloned the repository from a Linux shell. The clone's origin was verified. It was initially empty; during the owner's push/pull setup, an earlier `dum` commit (`0aca2ca`) added `dum.txt` before the planning commit. That existing file and history were preserved.

The canonical plan is now [current governing architecture](../architecture.md). Commit `2da489c992f55e3484e475bdf3a175a97e1d90c7` on `main` changes only `docs/design/skybuild_plan_20261008.123713.r2.md`. It is the first planning document, following the owner's test commit. The original SkyKeep copy was removed after the committed destination was verified. The Cord backlink and this handoff now point to SkyBuild. Supporting evidence remains in SkyKeep; the plan uses checked links to the sibling checkout. No push was performed by this session.

Repository/document setup is complete. Continue planning and the tool checks below. This documentation commit does not authorize implementation, migration, deployment or fleet activity.

## Restart checkpoint and tool setup

Updated 2026-10-08 after the owner installed planning tools. On resume, perform the checks below without asking the owner to repeat the tool instructions. Continue within the planning-only boundary above.

- The handoff, SkyBuild revision 2 plan, SkyKeep AGENTS.md, RTK instructions, and the CodeGraph, pyright-lsp, Caveman and TLA+ skills have been read. Read applicable instructions again if the new session lacks their contents. Use Caveman full for chat; persisted documents remain normal prose.
- `rg` is now available at `/usr/bin/rg`. RTK remains required for shell commands whose output is read. The earlier missing-ripgrep problem is resolved.
- Serena is configured and enabled in `~/.codex/config.toml`, launched through the available `uvx` executable. A standalone `serena` command was not on PATH; that does not establish an installation failure. The pre-restart session exposed no Serena MCP tools, so a working connection has not been verified.
- **First tool check after restarting Codex:** inspect this session's available tools for Serena. If present, select the correct checkout and verify its project identity before code navigation. Use a server owned by this session; do not reuse another session's process or project state. Report whether Serena is actually callable, not merely configured. Do not ask the owner to remember to request this check.
- If Serena tools are still absent, report that precise limitation and inspect relevant startup diagnostics when available. Do not reinstall Serena or change configuration merely because a tool is absent. `/mcp` is a command entered inside terminal Codex, not a shell command; keep troubleshooting instructions concrete.
- CodeGraph is available. `codegraph status` verified `/home/kevin/my_code/skykeep` as the indexed project. Preserve the existing rule against creating or refreshing an index without the applicable authorization; configuration and graph contents are not proof of deployed runtime state.
- `pyright-langserver` already existed at `/home/kevin/.local/bin/pyright-langserver`; this session did not install it. Claude's pyright-lsp plugin is installed and enabled in settings, but its live operation was not tested. The Codex session exposed no LSP tool. Follow the pyright-lsp skill's fallback when no semantic tool is available, and use CodeGraph first for call paths and blast radius.
- The owner explicitly enabled `~/.codex/skills/tlaplus/SKILL.md` for this task. `tlc` and `sany` are available under `~/.local/bin`. Apply the skill to SkyBuild launch permits, leases, fencing, retries and shutdown races: state invariants, write a small bounded model, run SANY before TLC, inspect counterexamples, check vacuity, and record bounds and implementation assumptions. No specification or model-checking run has been created yet. Keep checker artifacts in scratch space or a dedicated spec directory.
- A shared architecture-review skill was recommended but has not been created. OpenAI Docs MCP and other suggested tools have not been installed by this session. Do not turn recommendations into installation or implementation authorization.

The next substantive work remains the requirements interview and refinement of the existing plan. Paid-work authorization, contact-loss/stop behavior and Keeper adoption are still unanswered. Only the planning document was committed in SkyBuild; no implementation, service restart, fleet action, new log scan, migration or todo filing occurred.

For future requests, **“Save handoff”** means update the existing task handoff with decisions, unfinished work, constraints and exact resume checks, then provide its path. **“Resume SkyBuild”** means read this handoff and the current plan, perform the recorded resume checks, and continue the authorized planning work without asking the owner to restate context.

## Owner requirements and decisions

- Stonesky is the suite: SkyKeep is the vault application; SkyHermes is a Hermes instance that can call SkyKeep; SkyAudit is secure auditing; SkyTrends is the metrics application; SkyBuild is the build product. Cord remains the messaging capability; Keeper remains the remote worker component.
- Owner reported consuming roughly 44% of weekly allowance after a morning reset, with opaque multi-box activity and repeated integrator spawning. The build environment was deliberately stopped. We have not independently verified that every remote process is stopped or established which runs caused the spend.
- For the next two days, only specific controlled work is intended. Conserve model tokens; CPU is plentiful but must still preserve host reserves. Use deterministic scripts for recurring inspection, status, admission and recovery. Smaller model sizes and bounded llm.brodson assistance are preferred when adequate; tasks specify model SIZE, not vendor. Record actual engine/model separately.
- Build the communications/control system on PostgreSQL and migrate **all existing build REST API persistence** from SQLite early, including data pull-over. The former mailbox-only Cord migration is superseded.
- **SkyBuild PostgreSQL is a separate database from SkyKeep's application PostgreSQL**, with separate credentials, migrations, backups and lifecycle. A second schema in the same application database is insufficient. Physical host/server placement remains undecided. The application database is outside this migration.
- First implementation deliverable, when authorized, is a SkyBuild REST service with a hello-world website. Now plan this in the existing dedicated SkyBuild repository, rather than implementing in SkyKeep first. Separate the mechanical API/tests move from SQL behavior changes; migrate Keeper/shared tooling incrementally with thin compatibility commands.
- Retain Keeper's local and central start/stop, drain, checkpoint, graceful shutdown and update abilities. Plan reuse/reconciliation of existing aragog Keeper sessions; no blanket fleet resume or VM start is authorized.
- Every component must be queryable. Use independent box observers plus self-reported progress/adapters; an in-process heartbeat cannot report its own process death. Capture process identity, elapsed time, progress, OOM evidence, provider limits/reset times, tracebacks and artifacts. Distinguish stale/unreachable from dead, and alive from useful progress.
- Integration/test runs need per-test/attempt results, error artifacts, interrupted/incomplete states, coverage and lint outputs when collected. Preserve evidence before worktree cleanup. Avoid model-based monitoring and repeated unchanged relaunches.
- Architecture should prevent known problems structurally: one admission path, durable restrictive controls, fenced ownership, resource reservations, shared eligibility predicates, compatible versions and bounded scripted recovery with verified postconditions.
- Keep a few coherent tooling packets; avoid hundreds of tiny tasks/integration delays. Tooling changes skip product lanes under existing owner rules; use relevant tooling checks when implementation is authorized.
- User requested interviews multiple times, with “I don't know” and “More info” choices and tradeoffs. Continue one question at a time. Prior questions below are not answered by later unrelated messages.

## Current documents

The authoritative current planning draft is [current governing architecture](../architecture.md), in `/home/kevin/my_code/skybuild/docs/design/`. It contains three Mermaid diagrams and six proposed work packets, including migration, observers, test results, spend/control rules and acceptance cases.

Supporting documents below remain under `/home/kevin/my_code/skykeep/docs/design/`:
- `skymaker_current_inventory_20261008.md`: bounded read-only deployment/launch inventory from a Sol subagent. Filename preserves previous working name.
- `skymaker_migration_inventory_20261008.md`: bounded API/storage/log-miner inventory from a Luna subagent.
- `skymaker_failure_lessons_20261008.md`: Luna review of pre-collected seven-day estimates and fans-on lessons, with evidence limitations.
- `cord_plan_20261008.120240.r3.md`: superseded Cord plan; backlink now points to SkyBuild revision 2.
- `cord_plan_20261008.120240.architecture-review.md`, `.r1.md`, `.original.md`: preserved earlier Cord drafts. Their mailbox-only/in-repo assumptions are historical.

The old `skymaker_plan_20261008.123713.r1.md` was renamed to SkyBuild r2 and no longer exists. The canonical r2 plan is committed in SkyBuild at `2da489c992f55e3484e475bdf3a175a97e1d90c7`. Supporting planning documents and this handoff remain uncommitted in SkyKeep. Local links and diagram fences were checked; diagrams were not rendered. The shared SkyKeep checkout has unrelated dirty files; do not stage or commit those.

## Findings worth carrying forward

- Independent model-launch paths: installed role respawner, fleet-watch timer, watchdog; Keeper ensure adds a worker restart path. Pausing just one does not prove the fleet is stopped. Fans-on's historical “every box busy” directive is superseded by the current controlled-spend requirement.
- Fans-on says jeltz runs no build jobs. Proposed target excludes jeltz from build-worker allocation; control/observation and explicitly permitted coordination processes are distinct. Current deployment inventory includes historical local workers, not authority to restart them.
- Inspected source snapshots: main SkyKeep `383d3d375`; roles checkout `/home/kevin/skykeep-roles-checkout` at `fde9bd6fc`; `/home/kevin/my_code/skykeep-todo-live` at `1c314af98`. These are historical inspection points; verify actual deployed code before implementation. Main checkout/CodeGraph was stale relative to todo-live. Do not reindex without following owner instructions.
- The source v19 build DB has 27 tables. `/dump` omits several domains and cannot serve as the complete migration source. Use a consistent SQLite backup, all-table importer/validation and manifests for Git refs, briefs, readiness JSON and reviews stored outside SQLite.
- Current database path discovered from configured service: `/home/kevin/.local/share/skykeep-todo/todo.db`. We queried it only for earlier status. Never directly mutate it. Earlier blocked-count answer was 39 at 09:37 CDT; that number is stale.
- A read-only report saw repeated integrator launches, including earlier UTC-day close spacing and later roughly 15-minute starts. At least one integration finished. Launch timestamps alone neither prove wasted tokens nor explain the weekly allowance decline.
- User-systemd bus, crontab and network access were restricted in this session. Do not infer remote live state from process registry files. Relaunch with broader workspace access may not resolve network/systemd restrictions; test only when relevant.

## Seven-day analysis boundary

The retained brief `/home/kevin/my_code/skykeep-todo-live/todo/TOOLING-TOP-20-SKILL-SCRIPT-COMBOS-FROM-SESSION-LOGS.md` contains estimated counts from 266 Claude JSONL files / 27,355 tool calls. The categories overlap; token estimates are characters divided by four, not billing data. No separate retained seven-day failure/frustration report or aggregate miner output was located in the bounded searches. Do not claim an exhaustive failure review was completed.

Existing `/tmp/skykeep-log-scanner` has `seam/LOG-REPEAT-SCANNER`, `scripts/agents/session_mine.py`, `docs/dev/session-mine.md` and tests. Completion/landing remains unresolved. Do not reconstruct or rerun a full log analysis with a paid model. Reuse aggregate findings and, if later authorized, stream logs through the existing CPU miner with bounded redacted samples. The architecture now prioritizes shared log/result readers, box/unit/process observation, usage collection, lane/seam summaries, Git snapshots and brief assembly.

The current session spawned and completed bounded Sol/Luna inventory agents and a Luna failure-lessons agent. None changed services or ran a new log scan. Their final reports are saved above; no active subagent work needs continuation.

## Interview questions still unanswered

1. Paid model work: owner-selected manual work windows, per-job approvals, or unattended capped work? Proposed default: selected jobs, expiring manual window, conservative reservations, one paid model run at a time and no automatic paid retry. Numeric budget and expiry are unset.
2. Stop or loss of control-service connectivity: finish a bounded safe step/checkpoint, finish the whole job, or terminate except protected operations? Proposed default: bounded safe step, checkpoint, stop. Actual deadlines and protected-operation behavior need agreement.
3. Keeper adoption: adapt existing Keeper first or replace its execution loop? Proposed default: adapt through shared API client, simplify after controls work.

Additional future questions: control/database hosting and backup readiness, concrete spend limits, critical-operation deadlines. Repository choice is **answered**, not an open interview question. Do not treat elapsed time as permission to run jobs.

## Earlier Astra work and historical requests

Recovered Astra session: `/home/kevin/.codex/sessions/2026/10/08/rollout-2026-10-08T09-51-42-01a11bff-ab20-7ce0-a37d-8ae49833f37a.jsonl`. It reviewed Cord, created PostgreSQL and fast-track revisions, then began inspecting todo filing around 10:24 CDT. The log ends about 10:25 before a filing operation. Owner had asked for deferred tooling todos then, but the latest expanded SkyBuild scope remains a planning draft and this session filed none.

Older session tasks concerned shared Claude/Codex marshall behavior, model-size-neutral task specifications and a repetition scanner. Their scratch worktrees included `/tmp/skykeep-model-review`, `/tmp/skykeep-log-scanner`, and `/tmp/skykeep-marshall-shared`. Landing state is not established here. They are background context, not permission to resume autonomous build work now.

## Next useful action

Resume planning from the canonical SkyBuild plan. Repository setup and the initial documentation commit are complete. Perform the recorded tool checks, continue the requirements interview and tighten the proposed work packets. Keep supporting evidence in SkyKeep until a separate move is requested. Wait for explicit implementation authorization before any build, migration, deployment or fleet start.
