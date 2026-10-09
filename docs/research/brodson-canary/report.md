# Brodson coding canary — 2026-10-09

Task: SKYBUILD-BRODSON-CANARY-001. Base: `7aac729` on `dev-002`; isolated branch: `task/brodson-canary`. The owner authorized this small call sequence against the reported zero-charge shared pool. No runtime authority switch, deployment, paid provider, model reload, or concurrent load test occurred.

## Observed endpoint profile

Authenticated `GET https://llm.brodson.net/v1/models` returned three loaded llama.cpp aliases. Sanitized metadata is in `models.json`. Qwen `qwen3.5-think` identifies Qwen3.5-9B Q5_K_M, four configured slots and reported `n_ctx=16384`; Recall `recall-honcho-8b` reports Q5_K_M, two slots and `n_ctx=8192`; BGE-M3 reports F16, one slot, `n_ctx=8192` and 1024 embedding dimensions. Configured aggregate context is 65536/16384/8192 respectively. These are observations, not tested capacity guarantees. Model listing does not prove available spare capacity, exact hardware, reservation behavior, rate limits, or billing enforcement.

## Real coding task and outcome

The client previously accepted floating retry counts, failing later in `range`, and accepted booleans as integers. The bounded task required an integer other than bool in 0..5, early ValueError, unchanged timeout validation, and unchanged error message.

One nonstreaming `POST /v1/chat/completions` proposal used temperature 0, `max_tokens=512`, and requested `enable_thinking=false`. It completed in 0.861 seconds, returning 147 prompt and 66 completion tokens. It failed: undefined `self.retries` and `self.timeout`, bool accepted, and changed message. This proposal was rejected without application.

One corrective retry supplied explicit failures and constructor signature. It completed in 0.885 seconds, returning 232 prompt and 62 completion tokens. The proposal satisfied the narrow contract and was applied after inspection. Total returned usage: 507 tokens across two serial generation calls. Server fingerprint: `b11371-99b95488c`. Full sanitized prompts, response text and server timing fields are in `response-1.json` and `response-2.json`. Client timings include network and queue time; they are not isolated inference benchmarks. No reasoning content was returned, but this alone does not prove all internal reasoning behavior.

Independent CPU validation: `uv --cache-dir /tmp/skybuild-brodson-uv-cache run --extra test pytest -q tests/test_client.py` passed 32 tests. New cases reject bool, float, string, None, containers, and out-of-range integers before HTTP client creation. Accepted retry counts 0, 1 and 5 produce exactly retries+1 attempts. Existing retry identity, redirects and credential safety tests pass. The default uv cache was read-only; a task-local temporary cache resolved setup. No remote CPU was needed for a subsecond suite.

Independent exact-head adversarial model review is still required before integration. This canary author's assessment and CPU tests do not satisfy that gate.

## Research and limits

The [official Qwen3.5-9B card](https://huggingface.co/Qwen/Qwen3.5-9B) describes a 9B model with native context 262144 tokens and optional extension. That upstream maximum does not override this endpoint's reported 16384 context. Quantization and serving configuration may change quality; no comparison was run.

The [llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) documents OpenAI-compatible chat, model discovery and slot-based serving. The observed context division matches the configured total divided by parallel slots; no context exhaustion or saturation probe was performed. API compatibility beyond these two successful routes remains unqualified.

The [BAAI BGE-M3 card](https://huggingface.co/BAAI/bge-m3) describes multilingual retrieval, 1024 dimensions and 8192-token sequence length. It is an embedding model, not a coding fallback; embedding correctness was not tested.

The [Recall author card](https://huggingface.co/dman1011/recall-honcho-8b) describes a Qwen3-8B adaptation for explicit conclusion extraction. Its narrow purpose supports evaluating memory extraction separately rather than assuming coding skill. The endpoint alias and filename match that name, but upstream artifact identity/hash was not verified. No Recall generation was requested.

Boundary hypothesis: this profile may help with very small proposals when exact source, explicit type semantics, deterministic tests and qualified independent review surround it. Failure on a tiny first attempt rules out trusting unchecked edits. Two attempts on one task establish neither general pass rate nor a maximum safe task size. Next useful qualification is a few varied, owner-bounded tasks measured by first-pass acceptance and correction cost, not request volume. No claim is made about security changes, concurrency, architecture, tool calling, long context, streaming, cancellation, fairness, or reliability under shared demand.
