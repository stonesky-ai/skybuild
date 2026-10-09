# Brodson spike preparation

Task: `SKYBUILD-SHARED-INFERENCE`, manual file assignment 008 continued by 009.
Base: `257216c5498b95d5a6e56bca60c31abcd6736226`, architecture A35.
Branch: `task/brodson-spike-preparation-20261009`.
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
PYTHONDONTWRITEBYTECODE=1 nice -n 10 /home/kevin/my_code/skybuild/.venv/bin/python scripts/brodson_spike.py --output docs/research/brodson-spike/preparation-results.json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src nice -n 10 /home/kevin/my_code/skybuild/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_brodson_spike.py
```

The default mode replays offline fixtures. `--live` returns a failure before
reading input files. There is no HTTP client, discovery request, secret loading,
provider fallback, subprocess launcher or environment-based endpoint selection.
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
completion tokens and 15,360 returned prompt-plus-completion tokens. No discovery
call, automatic HTTP retry, paid fallback or unbudgeted corrective request is
included. Unknown input tokenization or missing returned usage blocks further
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
REST access is currently blocked by controller Serve/operator and sandbox
privilege boundaries. Qualification and explicit clearance remain required
before any endpoint use; no self-SSH or alternate privileged route is authorized.
