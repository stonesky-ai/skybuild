# Brodson spike preparation

Task: `SKYBUILD-SHARED-INFERENCE`, manual file assignments 008–010.
Base: `257216c5498b95d5a6e56bca60c31abcd6736226`, architecture A35.
HTTP preparation branch: `task/brodson-spike-http-preparation-20261009`, based on
published offline preparation `baa71c431bf74229974ec117b1fb8e05e1ce86fa`.
The published pinned-brief base `9e41ac131afe3e4f44b85a99f9acd8f4d04178f7`
was deliberately merged into the same branch for REST assignment reconciliation.
The parent owns independent exact-head review of the correction and merged delta.
Owner: lead dispatcher. The implementation owns only the six paths confirmed in
`next-task-20261009-009.md`. The central ledger and production code retain their
existing authority and scope.

This preparation provides three isolated cases and a deterministic offline
replay. Every response, generation latency and token count in
`recorded-responses.json` is synthetic. `preparation-results.json` records the
replay, fixture digests, acceptance and correction costs; its CPU validation
timings are local observations. It establishes no model acceptance rate,
endpoint reliability, available capacity or premium-token savings.

## Cases and independent assertions

1. **UTF-8 clipping code change.** Replace the return expression of a small
   fixture while preserving its strict argument validation. Fifty-six assertions
   cover empty/ASCII/multibyte/combining/emoji input and byte boundaries. A
   character-by-character oracle checks maximal prefixes. A bounded AST
   interpreter permits names, literals, slices and UTF-8 encode/decode only;
   arbitrary Python, imports and shell commands are never executed. Each
   expression is limited to 1,024 UTF-8 bytes and 100 AST nodes. Fixture input is
   at most 512 UTF-8 bytes. This small interpreter removes the need to execute
   returned source in a general subprocess.
2. **Interval review.** Find one planted half-open-boundary comparison defect.
   A structured finding must identify the correct source line and show a real
   counterexample. Independent integer-set intersection validates its expected
   behavior. Unsupported extra findings and false positives fail acceptance.
3. **Run-length encoder test design.** Return four to eight JSON parameter rows
   with empty, adjacent-repeat, separated-repeat and Unicode coverage. Separate
   reconstruction/canonical-run invariants validate expectations. The table must
   detect mutants that merge nonadjacent characters and drop the final run.
   Each input has at most 128 characters; returned tests are data.

The synthetic replay deliberately has one first-pass acceptance and two corrected
acceptances across five simulated responses. These counts exercise accounting;
they describe authored fixtures. Invalid proposals retain failure evidence.
File reads, prompt bytes, candidate structure and response bytes are bounded.
Case/source identity is checked and complete fixture digests accompany results.
Prompt text is selected separately from oracle/check data.

## Commands and current execution boundary

Use the existing Python environment; no install or service is needed:

```bash
PYTHONDONTWRITEBYTECODE=1 nice -n 10 /home/kevin/my_code/skybuild/.venv/bin/python scripts/brodson_spike.py --output /tmp/brodson-offline-replay.json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src nice -n 10 /home/kevin/my_code/skybuild/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_brodson_spike.py
```

The default mode replays offline fixtures. `--live` returns a failure before
reading input files. The HTTP exercise is a Python function requiring exactly an
`httpx.MockTransport`; it has no CLI live path, secret loader, environment-based
endpoint selection or real transport. `live_admission` unconditionally refuses.
Live transport would require a separately authorized and reviewed change after
REST-worker qualification and explicit parent clearance. A command flag cannot
grant that clearance. Recorded real responses would require a later authorized
fixture contract; this version accepts explicitly synthetic recordings only.

The current host-watch skill governs resource checks. Inspect the existing fresh
state before checks, preserve 8 GiB available memory and use niceness 10 for CPU
work. The watcher PID is outside this sandbox's visible namespace; fresh samples
and its held state lock provide local evidence, while process provenance and
host-wide uniqueness remain for the controller owner. No replacement watcher or
privileged workaround is part of this task.

## Proposed future request ceiling

These limits reserve a possible later evaluation; current live calls are zero.
Three cases allow one proposal and at most one corrective response each: six
serial generation requests total, concurrency one. Each permits 512 output
tokens, a 60-second timeout, 16 KiB response bytes, at most 8,192 prompt bytes and
2,048 input tokens after a tokenizer is qualified. Total ceilings are 3,072
completion tokens and 15,360 returned prompt-plus-completion tokens. One
GET `/v1/models` discovery precedes generation; it is separately counted. No
automatic HTTP retry, paid fallback or unbudgeted corrective request is included. Unknown input tokenization or missing returned usage blocks further
live admission. Thinking settings and all returned usage must be recorded without
assuming hidden reasoning is free or absent.

Preserve exact prompts/responses and their digests, requested settings, observed
model alias/fingerprint, wall latency, returned usage and all rejected attempts.
Measure first-pass acceptance, final acceptance, corrective usage/latency and
independent validation/review cost. Stop on timeout, ambiguous completion,
endpoint error, partial/malformed/oversized response, unknown/exhausted budget,
second defective response, unavailable shared capacity or owner stop. Retain
uncertain exposure after a timeout; do not treat it as a freed server slot.
Corrections consume the same total budget. A failed fixture with no recorded
correction stops the replay too.

The initial [canary](../brodson-canary/report.md) observed one tiny task requiring
correction. Its reported Qwen/Recall/BGE slot counts are shared ceilings, with
integration and product-test demand protected. This preparation uses none of
those slots and performs no reload, stress test, live discovery or reliability
claim. The architecture's [shared inference contract](../../design/architecture.md#14-cpu-first-work-and-inference-routing)
and [historical research](../../design/research/shared_inference_capacity_20261008.md)
remain governing context.

## Review and stop conditions

The parent has an already available `gpt-6-astra` agent for stronger design/review
under its existing approved profile. This worker launches no new stronger
profile. Its independent exact-head verdict and usage evidence remain pending
with the parent; local tests and synthetic fixtures cannot approve their author.
The parent owns frozen-bundle integration. Pushes preserve the task branch;
this task opens no task PR and does not merge or deploy.

The owner cutoff is **2026-10-09 15:20:53 UTC**, including nested inference.
Begin checkpoint and closeout by 15:15:53 UTC. An answer or path confirmation
does not renew that cutoff. Checkpoint exact head/push status, remaining gates,
test evidence and unconsumed future request budget in the separate task result.
The existing scoped Wonko REST client and pinned installation CA now transport
this assignment and its review feedback. REST transport qualification grants no
Brodson endpoint clearance. No self-SSH or alternate privileged route is authorized.

## Fake HTTP protocol and durable accounting

`run_fake_http` accepts the existing synthetic fixtures, an explicit MockTransport,
an owned canonical mode-0700 journal directory, synthetic authority, and a
synthetic token-count callback. This authority is a test input, never proof of
live admission. The fixed HTTPS origin is `https://llm.brodson.net`, port 443;
there is no endpoint override. Discovery must return exactly one matching loaded
`qwen3.5-think` alias with explicitly synthetic reserved-capacity metadata. This
condition exercises fail-closed selection and qualifies no actual capacity.

Each serial POST uses `/v1/chat/completions`, stream false, temperature zero,
max_tokens 512 and enable_thinking false. Correction includes the previous
assistant content and deterministic failure reason; all message bytes and the
synthetic count, including corrective history, must fit the existing bounds.
Unknown counters, malformed usage, boolean/negative/excessive counts, inconsistent
totals, mismatched model, multiple choices and non-stop completions stop the run.
No reasoning-token accounting claim follows from the requested thinking flag.

The client disables inherited proxies and redirects and uses no retry wrapper.
A dedicated owned child runs each fixed HTTP operation. The parent enforces a
monotonic deadline of at most 60 seconds across dispatch, header wait, raw stream
and JSON parsing, killing and joining only that child on expiration. Phase
timeouts alone are insufficient. Raw bytes are capped before parsing; compressed
responses are refused. Error bodies and exception text are discarded. The
request child never evaluates returned code. The AST interpreter remains the
only mechanism for evaluating a proposed expression.

The private journal lock serializes the whole run. Its immutable synthetic
identity binds assignment, head, case-manifest digest, origin, alias, limits,
expiry and canonical journal root. Each GET/POST reservation is atomically
replaced and file/directory synced before the simulated effect. Counts derive
from retained entries: one discovery, six generations, two per case. Each
consumed generation retains 2,560 tokens of conservative exposure, including
unknown outcomes, up to 15,360. A failed write prevents a request. An uncertain
attempt blocks subsequent requests and replay; no timeout renews authority or
frees capacity. Completed sanitized responses may be replayed through CPU
validation without another request. Reopening syncs persisted state before
trusting it. A lock without its state refuses a reset. Reader and writer share
the same 512 KiB journal cap.

Evidence retains sanitized complete message bodies, successful JSON responses,
returned usage, model metadata, validation outcomes and coarse failure categories.
Every completed HTTP outcome also stores `parent_elapsed_wall_ms`, measured by
parent monotonic time around the request operation, including IPC and owned-child
termination on deadline. Success, HTTP/transport errors and deadline timeouts all
retain this measurement. `packet.latency_ms` separately describes child-observed
mock response processing; it is not the parent wall measurement. A restart that
refuses an uncertain consumed attempt preserves the original latency and exposure
rather than timing or dispatching another attempt. Authentication-shaped fields and bearer strings are
redacted recursively, including structured JSON chat content. No credential
reference is read; mock authorization uses an explicitly synthetic constant.
Every HTTP result is labeled synthetic, with live_calls zero and synthetic
input tokenization. Exact sanitized prompts and responses live in the journal;
the committed preparation artifact retains the synthetic HTTP journal entries
alongside the offline replay and summary. Its temporary journal root no longer
exists; the artifact is evidence, not a resumable authority record. The CLI
command above regenerates offline evidence only.

## Unresolved live gates and owner questions

The two environment reference names `SKYBUILD_INFERENCE_BASE_URL` and
`SKYBUILD_INFERENCE_SECRET_FILE` were unset in this worker. Root `.env` and the
historical local secret-path reference were absent on metadata-only inspection.
No token contents were read, copied or provisioned. Missing credential reference
is a named live blocker.

Before designing a live policy, the parent must resolve:

1. Which verified tokenizer and exact chat template count complete corrective
   history? How do reasoning tokens relate to completion/total usage?
2. What authoritative model identity and shared capacity reservation qualify
   the loaded alias beyond historical canary metadata and configured slots?
3. What authenticated REST assignment binds independently approved exact HEAD,
   case digests, profile, finite expiry and a protected registered journal root?
   Who reconciles missing/moved journals and ambiguous server outcomes?

A caller-supplied dictionary, file name, time window or matching local hash is
not trusted REST authorization. The synthetic journal cannot protect against an
owner deliberately deleting both lock and state or choosing a fresh root. Future
REST authority must register immutable attempts and protected storage, compare
actual clean Git HEAD with the independently reviewed approved head, bind the
qualified tokenizer/template/profile/capacity and enforce finite shared budgets.
Until REST workers qualify and the parent delivers explicit later clearance
through REST, the unconditional live boundary remains closed. There are no
endpoint observations or live attempts in this assignment, including discovery.

## Restart recovery checkpoint, 2026-10-09

The recovered mock HTTP changes now share one total wall deadline across
discovery and generation, persist its expiry in the journal, and retain the
earliest deadline across restart. Child transport timeouts use the remaining
budget. An expired run cannot dispatch another request or renew its budget.
The timeout regression checks parent time across discovery and generation;
the new restart regression preserves consumed attempts and token exposure.
All 90 focused tests passed in 2.51 seconds. These are synthetic checks only.
The previously committed preparation artifact describes the earlier runner;
it has not been regenerated as evidence for this recovered change.

The owner requested committing and pushing the recovered work. Jeltz's
authenticated dispatcher was notified through Cord. Independent exact-head
review and frozen-bundle integration remain with the dispatcher; this
checkpoint does not claim either gate has passed.

## Deadline recovery correction, 2026-10-09

Independent review found that reconstructing a monotonic deadline from wall
time could renew expired work after a clock rollback, and that a shortened
deadline was not durable across another restart. The journal now records the
monotonic deadline with the host boot ID, durably keeps the earliest observed
deadline, and refuses reuse after reboot because the old monotonic epoch cannot
be trusted. Regression coverage exercises deadline shortening, attempted
extension, rollback with a completed discovery prefix, boot change, and wall
rollback during preflight. The wall/monotonic pair is captured before
preflight, so later wall-clock changes cannot extend authority expiry. All
checks use explicit `httpx.MockTransport`; the focused suite passes 94 tests.
No endpoint call or worker launch was made. Exact-head independent review and
frozen-bundle integration remain pending.
