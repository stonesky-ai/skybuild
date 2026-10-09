# Four-concurrent Brodson capability probes, 2026-10-09

Task: `SKYBUILD-SHARED-INFERENCE`. Report base: `c7ba05cad4cba1c024124ae3bb3f00a58ff464e2`.

**The tested profile reliably delivered the small task-classification example, but did not deliver accepted code repair, bug-review or test-design answers.** Each of three four-concurrent probes accepted one answer out of four. Thinking disabled produced quick but incorrect technical answers; thinking enabled exhausted both tested output ceilings without final technical answers. These are bounded observations on four small cases, not a general model qualification.

## Authority and execution scope

The owner described the friend's GPU inference as entirely free, explicitly authorized proceeding, permitted ordinary inference to wake/reload the selected already spawned model, and requested four concurrent sessions with higher latency accepted. The operator retained separate authorization and durable state for each new probe. Earlier expired state and consumed attempts remained preserved. These specific permissions did not authorize explicit model-load/unload endpoints, configuration changes, paid fallback, worker launch or automatic repository execution.

Runs 012–014 used independently reviewed private operator commands and published helper source at `93d8a6cae4fe889220ebb2a98670ac8426084367`. They did not change the repository's serial qualification runner or establish that its serial assignment authorized concurrency. Each probe made two metadata GETs, four concurrent input-count POSTs, then four concurrent generation POSTs only after every count passed. Each used a fresh durable marker, consumed intent before transport, no retries, and a 600-second local monotonic deadline. Counting had a 60-second ceiling; generation had a 180-second ceiling. Credentials stayed with the controller and are excluded from this report.

The selected alias was `qwen3.5-think`; metadata and all generation fingerprints matched `b11371-99b95488c`. The actual template hash was `7f0e529032c25183bcd66c7f238da2d377f43be754a94e2725a58c4e16d2ed67`. Independent review found rendering deterministic for the permitted plain-text, no-tools shape with either explicit thinking setting. This does not establish deterministic generation, installed weights, exclusive GPU ownership or an unchanged future serving profile.

Count and generation used identical serialized request bodies, temperature zero, one nonstreaming completion and input counts in 1..2048. Generation reservations used the accepted input count plus the full output ceiling. Request and response sizes were bounded. Returned code was never executed: technical candidates used the existing bounded AST/data validators, and classification used exact expected arrays.

## Cases and limits

The first three prompts are the existing [fixture cases](cases.json): repair a UTF-8 byte-limited prefix expression; demonstrate a half-open interval boundary defect; and design run-length-encoding tests with independent reconstruction and mutation checks. The fourth classified five synthetic task records into eligible and blocked arrays using an explicit rule and preserving input order. All three runs reused those same prompts; repeated success is not held-out accuracy evidence.

| Run | Thinking | Output cap per request | New reservation cap | Required integer context | Generation response cap |
|---|---|---:|---:|---:|---:|
| 012 | Disabled | 512 | 10,240 | 2,560 | 16 KiB |
| 013 | Enabled | 2,048 | 15,360 | 4,096 | 16 KiB |
| 014 | Enabled | 4,096 | 24,576 | 6,144 | 32 KiB |

All runs completed ten HTTP requests. There were also two preserved earlier metadata requests, so cumulative HTTP attempts through run 014 were **32**, including twelve generations. Completion of HTTP handling does not mean acceptance of the answer.

## Returned usage and observed latency

Usage below sums every generation envelope, including malformed or truncated candidates. Cached prompt tokens remain included. Reservations are retained accounting amounts, not returned usage or newly available capacity.

| Run | Prompt tokens | Completion tokens | Total returned tokens | Reservation retained | Accepted |
|---|---:|---:|---:|---:|---:|
| 012 | 635 | 474 | 1,109 | 2,683 | 1/4 |
| 013 | 627 | 6,627 | 7,254 | 8,819 | 1/4 |
| 014 | 627 | 12,771 | 13,398 | 17,011 | 1/4 |
| **Total** | **1,889** | **19,872** | **21,761** | **28,513** | **3/12** |

Parent-observed per-request wall latency includes the request operation; simultaneous durations should not be added as batch elapsed time.

| Case | Run 012 | Run 013 | Run 014 |
|---|---:|---:|---:|
| UTF-8 clip | 1.222 s | 42.482 s | 87.280 s |
| Interval review | 1.199 s | 42.442 s | 87.249 s |
| RLE test design | 4.290 s | 42.446 s | 87.242 s |
| Task classification | 0.764 s | 8.520 s | 8.512 s |

In run 014, the four generation starts were recorded within 45.918 milliseconds, and the final response completed 87.280 seconds after the first start. This verifies concurrent client dispatch and response handling in that probe. It does not measure the server's internal scheduling, sustained throughput or capacity under competing projects.

## What failed and what passed

Run 012 returned normal completion envelopes for all four requests. All model/build/count/usage checks passed, but three technical answers failed independent validation:

- **UTF-8 clip:** the proposed expression used keyword arguments outside the allowed interpreter grammar. It also sliced characters before encoding, so it could return `€` for a one-byte limit. Relaxing syntax validation would not repair the byte-budget error.
- **Interval review:** the proposed intervals were disjoint. Both the implementation and contract returned false; the claimed expected true did not demonstrate the boundary defect.
- **RLE test design:** Markdown fences violated the JSON-only requirement. Fence removal for offline diagnosis still exposed a wrong expected result for `Hello  World`: the initial runs claimed three `l` characters and two `o` characters instead of two and one. The other seven rows matched the local oracle. This was not merely a formatting failure.
- **Task classification:** the exact eligible array `["a", "d"]` and blocked array `["b", "c", "e"]` passed. It used 125 prompt and 16 completion tokens.

The rejected RLE response used **172 prompt + 384 completion = 556 tokens**. Summing only successfully parsed candidate results would incorrectly omit those tokens.

Runs 013 and 014 each returned three `finish_reason: length` responses with empty final content. Their technical cases consumed exactly 2,048 and 4,096 completion tokens respectively. No completed answer was lost to the 180-second timeout. Each run-014 technical reasoning string extended the corresponding run-013 truncated string and continued reconsidering solutions and output formatting. Doubling the ceiling continued that behavior without producing final JSON. Partial reasoning was not extracted or counted as accepted code, a finding or a test suite. The exact model/runtime cause of this behavior remains unestablished.

Task classification passed in both thinking-enabled runs, each using **123 prompt + 483 completion = 606 tokens**. Thinking increased latency and usage on this example without improving correctness. The difference in prompt counts between profiles is retained rather than assumed away; each returned count matched its own preceding count request.

## Practical conclusion and pending evidence

Simple rule-based extraction/classification with a cheap exact checker is the demonstrated candidate task class. For a rule this simple, deterministic CPU processing remains preferable when the inputs are already structured. Broader usefulness needs varied held-out examples, especially where language interpretation adds value. Three passes on the same task record list do not establish general classification accuracy.

The tested profile is not qualified for autonomous code repair, bug review or test design. The nonthinking RLE output contained useful draft rows, but a real semantic defect still required correction. Thinking with larger output budgets did not improve accepted outcomes; increasing the ceiling alone approximately doubled technical latency and completion consumption from run 013 to 014.

A separately bounded correction comparison could measure whether concise invariant feedback and stricter output instructions improve the technical drafts. Such results must be distinguished from first-attempt accuracy, retain unchanged validators and count all returned usage. No general-purpose worker, automatic patch application, retrieval/embedding qualification or paid-provider fallback follows from this report.

**Run 015 is pending in this report.** No result, success rate or usage for that run is asserted or included in the totals. Its evidence requires separate assessment before this report is extended.

## Evidence provenance

Raw operator authorizations, journals and responses remain private. This sanitized report excludes credentials, private file locations, server command arguments and internal reasoning text. Retained run-journal SHA-256 values identify the assessed snapshots:

| Run | Journal SHA-256 |
|---|---|
| 012 | `121e351e0a136f1ff33fe29a22a0ff513a47d34c33106fe26294e41901429385` |
| 013 | `d2218bcd08764f5b97b8b1ed69b81b88b486a20e16428d480384a58b7488acc7` |
| 014 | `5544fef84b9d75d2e4ece722ec6ac1d21bf54bb7f7ee0a19c82f181f9034dc43` |

Each operator command received independent review before its recorded execution. A separate reviewer must review this report before publication; its author cannot supply that approval. The report records bounded capability evidence under `SKYBUILD-SHARED-INFERENCE` and changes no ledger ownership or execution policy.
