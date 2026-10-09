# Bounded Brodson attempt accounting

Safety contract: a request cannot start before its attempt and worst-case token reservation are durable. A lost response or crash never refunds that exposure. Restart cannot redispatch an unresolved attempt. Generation requires qualified metadata and current approval; attempts are serial and stay within the aggregate reservation budget.

`Qualification.tla` models two generation operations, a reservation of three abstract tokens each, a budget of six, qualification, approval expiry, crashes, replies and restarts. Pending/inflight state becomes indistinguishable after a crash. Correct restart blocks such a run. The implementation is stricter: interruption between complete requests also stops instead of reconstructing a continuation. Metadata's explicit phase boundary is the only resumable intermediate point.

Run SANY, then TLC with one worker and a bounded heap:

```sh
sany Qualification.tla
JAVA_TOOL_OPTIONS=-Xmx256m tlc -workers 1 -deadlock -metadir /tmp/brodson-qualification-states Qualification.tla
```

Observed with TLC 2.19 on 2026-10-09: **144 generated states, 84 distinct states, depth 11; no invariant violation**. Checked `Budget`, `ExposureRetained`, `AtMostOnce`, `QualifiedEffects` and `Serial`. No fairness or liveness property is claimed; `-deadlock` disables deadlock reporting because blocked/expired runs intentionally stop.

Mutation challenge: use a scratch config with `BrokenRestart = TRUE`. This deliberately resets unresolved state and reservation on restart. TLC rejects `ExposureRetained` after **36 generated states, 25 distinct states, depth 6**:

`Init → Qualify → Intent(1) → Send(1) → Crash → Restart`

The sent operation becomes idle with zero reservation. This is the precise recovery behavior prohibited by the implementation. The original source/config remain unmutated. An initial spec typo in the disjunction assignment was corrected before the successful run; the mutation above is a separate deliberate challenge.

Assumptions mapped to code: one process owns the private state-directory lock; atomic replacement plus file/directory fsync supplies durable intent; local storage is not maliciously altered or rolled back; the operator preserves one run directory and does not create a new run to bypass a consumed budget; remote calls may finish after disconnection. Approval uses a retained same-boot monotonic endpoint as well as UTC checks. Metadata qualification and deterministic-template review are external predicates represented by `qualified`, not proved by this model. The model does not certify TLS, llama.cpp behavior, tokenization, arbitrary templates, billing, or model capacity.
