# Brodson metadata and token-count qualification

Task `SKYBUILD-SHARED-INFERENCE`, base `42c3256d1dd05af2c1c457d214231cce9f69c066`, branch `task/brodson-live-qualification`. This is a separate bounded controller command. It does not enable assignment010's `--live`, install an adapter, launch workers or claim spare inference capacity. Current implementation evidence is entirely offline and synthetic; no Brodson calls or real credential reads occurred during preparation.

## Two explicit operator decisions, one budget

The [committed REST brief](../../design/assignments/brodson-qualification-20261009.json) must be published, received and verified through the manual pilot. Wonko receives the brief and returns evidence; the controller keeps the friend's credential. A receipt does not launch the command. The parent first records independent exact-head review and a private metadata-only operator authorization. That invocation makes at most two requests and always exits before counting or generation:

1. `GET /models`, using the router metadata contract that exposes actual spawned child arguments.
2. `GET /props?model=qwen3.5-think&autoload=false`.

The first response must identify a unique already loaded target before the second request. Missing target/status is a preserved failure, not permission to probe another route. Metadata is normalized into private state: full server command arguments and model paths are omitted; only the explicit sleep setting, selected model metadata and relevant props are retained. A digest of the received bytes accompanies each response.

A separate continuation authorization must name the same run, source head, REST assignment digest, state directory, profile and environment reference. It also binds the exact normalized metadata digest, reviewed build and template hash, and explicitly attests that the template/request shape is deterministic and the serving profile has not changed since metadata collection. These fields are an operator attestation, not cryptographic proof of a person, independent review, receipt authenticity or server immutability. The parent must obtain the actual evidence before writing them.

Continuation requires metadata less than 30 minutes old on the same boot, the same original approval deadline or an earlier one, and the original persisted monotonic approval endpoint. Wall-clock rollback cannot renew that endpoint; a reboot blocks reuse. Stale metadata stops the run without an extra GET. A new separately authorized run needs a new explicit budget; deleting state or changing directories must never be used as recovery. The current invocation must expire no later than **2026-10-09T15:20:53Z**. No authorization file is included in the repository.

The complete run permits14 HTTP attempts: two metadata GETs, six input-count POSTs and six generation POSTs, all serial. Metadata and counting have 10-second total wall deadlines and 64 KiB/4 KiB response caps. Generation has a 60-second total wall deadline and 16 KiB response cap. Complete request JSON is limited to 8,192 UTF-8 bytes. Redirects, HTTP retries, inherited proxy/CA settings, alternative route probes and provider fallback are disabled. The existing HTTPS system trust verifies `https://llm.brodson.net`; there is no TLS bypass.

## Qualification evidence and limitations

The initially reviewed source contract is llama.cpp `99b95488c`, whose historical exact build string is `b11371-99b95488c`. That historical string is an allowlist entry for reviewed protocol behavior, not proof that the current endpoint runs it. Current props must exactly match the operator-approved string; each generation must return the identical fingerprint. A different current build is retained as metadata and blocks counting pending separately reviewed implementation/profile changes.

The selected router child must report status `loaded`, and props must report `is_sleeping:false`. The child's actual argv must contain exactly one canonical pair `--sleep-idle-seconds`, `-1`. Absence, duplicates, positive values and the unsupported equals spelling fail. Server defaults can come from system/user configuration; absence of a CLI flag cannot prove disabled sleep. `autoload=false` prevents loading an unloaded child but can still wake a sleeping child, so the explicit disabled-sleep evidence matters. External administrative changes remain outside this client's control. The exact immutable serving profile and template attestation remain operator responsibilities.

Reported per-slot context must accommodate the full 2,560-token reservation ceiling, and the server must report a positive slot count. These checks validate reported configuration; they do not allocate or claim an idle slot. Metadata freshness is checked before every count and generation, including between a count and its corresponding generation.

Input counting uses `POST /v1/chat/completions/input_tokens?autoload=false`. The exact same serialized JSON bytes then go to `POST /v1/chat/completions?autoload=false`. The body fixes the model, messages, temperature 0, `max_tokens:512`, `n:1`, nonstreaming output, and boolean `chat_template_kwargs.enable_thinking:false`. No tools or multimodal inputs are sent.

The server's input-count handler shares its chat parser and tokenizer with generation. It provides the actual rendered-prompt count, removing the need to guess bytes-to-tokens or download a tokenizer. However, arbitrary Jinja templates may read current time: identical JSON alone cannot prove identical rendering. An independently reviewed deterministic-template attestation must bind the exact template hash, effective profile and permitted request shape. The implementation intentionally does not substitute a substring blacklist for that review.

Counts must be integer values 1..2048, excluding bool. Before each generation, the runner durably reserves the counted input plus 512 output tokens against 15,360 aggregate tokens. Reservations are never refunded in this run. Returned usage must contain the exact counted prompt value,1..512 completion tokens, and total equal to their sum. Cached prompt tokens remain included. Missing usage, count disagreement, different model/build, tool calls, malformed output, truncated completion or timeout stops all later calls. A post-response mismatch is detection, not retroactive enforcement against a misbehaving or changed server.

Only a complete JSON-object candidate failing the independent CPU assertions earns one correction. The corrective request contains the original request, exact previous content and fixed validation reason. A second defective proposal stops the remaining cases. Existing bounded AST/data validators inspect candidates; no returned code, shell command or test file is executed. The three tiny cases provide observed spike evidence only, not general coding competence or available capacity.

Primary source anchors: [router status and actual arguments](https://github.com/ggml-org/llama.cpp/blob/99b95488c/tools/server/server-models.cpp#L2034-L2037), [config precedence](https://github.com/ggml-org/llama.cpp/blob/99b95488c/common/arg.cpp#L719-L772), [autoload control](https://github.com/ggml-org/llama.cpp/blob/99b95488c/tools/server/server-models.cpp#L1859-L1884), [count handler](https://github.com/ggml-org/llama.cpp/blob/99b95488c/tools/server/server-context.cpp#L5813-L5861), [thinking kwargs](https://github.com/ggml-org/llama.cpp/blob/99b95488c/tools/server/server-common.cpp#L1355-L1379), [time-dependent template context](https://github.com/ggml-org/llama.cpp/blob/99b95488c/common/chat.cpp#L1080-L1087), [generation fingerprint](https://github.com/ggml-org/llama.cpp/blob/99b95488c/tools/server/server-task.cpp#L443-L447).

## Private execution contract

Invoke only after the parent has supplied actual authorization and assignment files, using the reviewed checkout and its own package:

```sh
PYTHONPATH=/absolute/reviewed-checkout/src /absolute/project-python -m scripts.brodson_qualification \
  --authorization /private/operator-authorization.json \
  --assignment /private/verified-rest-assignment.json
```

Run from the reviewed checkout. Both files must be owned, mode 0600 regular files. The authorization schema is `brodson-operator-authorization-v1`; all fields are mandatory:

- `run_id`, `phase` (`metadata` or `spike`), `operator_approval_id`, UTC `approved_until`;
- `reviewed_head`, `review_artifact_sha256`, `rest_assignment_sha256`, `rest_receipt_id`;
- `zero_charge_profile` exactly `owner-confirmed-zero-charge-brodson`, canonical external `state_dir` and controller `env_file`;
- `limits` exactly the committed `LIMITS` object;
- `metadata_sha256`, `template_sha256`, `approved_build_info`, `deterministic_template_reviewed`, `profile_unchanged_since_metadata`.

For metadata, the last five fields may be null/false because no qualified template/build is yet claimed. For continuation they must contain the actual hashes, exact approved build and true attestations. Assignment and metadata digests use the module's canonical `encode` function: sorted compact UTF-8 JSON, Unicode unescaped, no NaN. The template digest hashes the exact UTF-8 template string. The assignment file must bind the published committed brief. The current clean source head must match the reviewed head, with identical brief bytes at that head and assignment base.

The environment file is the existing ignored controller `.env`, mode 0600, with only the existing `SKYBUILD_INFERENCE_BASE_URL` and `SKYBUILD_INFERENCE_SECRET_FILE` references selected. There is no shell evaluation, environment expansion, installation, secret transfer or fallback. The HTTP transport reads the referenced private token only after the attempt intent has been persisted. Tokens are never printed, put into request bodies or saved in state; reflected token content causes a fixed failure. Errors exposed by the command are fixed status/reason codes.

One cooperating process holds a nonblocking advisory lock for the full invocation in the exact private state directory. `run.json` is mode 0600 and atomically replaced with file and directory fsync before each request. Every attempt records its exact body/hash, route, normalized response or fixed failure, response hash, wall latency and reservation. A pending attempt or interruption between completed requests blocks restart rather than replaying. Completed or metadata-only repeated invocations reuse their saved outcome without new requests. Lost responses retain uncertainty and reserved capacity; there is no claim of remote cancellation. Local timeouts bound client waiting, not server execution after an uncertain disconnect.

## Offline verification

Run focused tests with the task checkout's `src` on `PYTHONPATH`. Synthetic transports assert that the actual fsynced file already contains the pending attempt and reservation before every fake effect. Tests cover the 14-request ceiling, exact count/generation bytes, semantic correction, unknown usage, malformed/truncated/oversized responses, identity drift, explicit sleep settings, stale metadata, authorization, partial crashes and transport bounds. The [small TLA+ model](../../design/models/brodson_qualification/README.md) checks bounded crash/reservation invariants and preserves a deliberately broken restart counterexample. Neither local tests nor the model constitute independent review or live qualification.

Preparation check: `tests/test_brodson_qualification.py`, existing `tests/test_brodson_spike.py`, and `tests/test_manual_assignment.py` produced **116 passed in 0.89 seconds**. Package import resolved to this task checkout. Every transport test used fake responses; actual friend credentials and the endpoint were untouched.
