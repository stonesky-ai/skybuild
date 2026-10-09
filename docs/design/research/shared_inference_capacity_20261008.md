# Shared inference capability evidence

Recorded 2026-10-08 for architecture A31; refreshed with A32 live discovery evidence. This is evidence and a qualification proposal, not runtime acceptance. [Architecture section 14](../architecture.md#14-cpu-first-work-and-inference-routing) governs.

## Latest live discovery: A32 evidence follow-up

On 2026-10-08, the resumed session declared network access enabled. One bounded authenticated HTTPS GET to `/v1/models` succeeded with HTTP 200, using `SKYBUILD_INFERENCE_BASE_URL` and `SKYBUILD_INFERENCE_SECRET_FILE` from SkyBuild's ignored `.env`. The credential remained in memory and was sent only as a Bearer header to the configured origin. TLS verification remained enabled; redirects were refused; timeout was 12 seconds and response size was capped at 65,536 bytes.

| Returned model ID | Returned object | Returned owner label |
| --- | --- | --- |
| `bge-m3` | `model` | `llamacpp` |
| `qwen3.5-think` | `model` | `llamacpp` |
| `recall-honcho-8b` | `model` | `llamacpp` |

This establishes endpoint reachability and a successful model-list response using the supplied credential. It does not independently establish that authentication is required, identify exact weights or quantization, verify generation/embedding behavior, or measure concurrency/quality. The allowlisted response contained no context-length fields. Slot counts remain owner-reported 4/2/1. No generation, embedding, stress test or administrative request was made. No token value was printed or persisted. Previous DNS failures below are historical and no longer the current blocker.

This is an evidence refresh under A32, not a new architecture decision. Next useful qualification is a capped serial protocol smoke test using synthetic input, followed separately by representative coding/retrieval evaluation. Do not treat the model listing or a future toy completion as coding qualification.

## What was known before successful discovery

| Source | Observation | Limit |
| --- | --- | --- |
| Owner | Friend's server is free to use; Qwen `qwen3.5-think` has 4 parallel slots, `recall-honcho-8b` has 2, `bge-m3` has 1. Owner expects some limited Qwen coding and wants project-visible tradeoffs. | Reported capacity, not measured exclusivity, identity or throughput. The later explicit BGE-M3 name resolves the earlier “bge-me” wording for this plan. |
| Local configuration | Existing `~/.config/skykeep/gate-lane.conf` selects `https://llm.brodson.net`, provider `openai-embed`, and a credential-file reference whose file exists. | Configuration is not proof of server reachability or accepted authentication. No secret value is retained here. |
| Session discovery | HTTPS metadata discovery could not proceed: Python hostname resolution returned `gaierror`, errno -3, temporary name-resolution failure. A web metadata lookup also failed. | Session/network restriction or DNS failure does not establish server downtime. No authentication or inference reached the endpoint; initial discovery stopped before reading the credential. |
| SkyKeep source | Session CodeGraph inspected `scripts/endpoint_shape.py`: OpenAI-shaped model listing, chat and embedding routes; configurable auth, output fields and response parsing. | Extract useful neutral protocol behavior; do not import the vault package or assume its current provider/model settings qualify SkyBuild. |

The owner then provided `/tmp/brodson-model.secret` and reconfirmed the same HTTPS base URL. The file exists. A single retry still failed hostname resolution before reading the key or sending authentication. This was the discovery reference at that point; the later owner-selected external file and generic `.env` settings supersede it.

Following the owner’s explicit Bearer-header instructions, a final bounded request read `/tmp/brodson-model.secret` into an in-memory variable and constructed `Authorization: Bearer <key>` for `/v1/models`. It again failed with DNS `gaierror`, errno -3, before HTTP authentication could reach the server. The key was never printed, persisted or added to Git; the temporary file was left untouched. Stop retries until network/DNS conditions change.

During those initial attempts, no `.env*` contents were read. The later successful discovery read only SkyBuild’s authorized installation settings, not SkyKeep vault configuration. The dedicated existing configuration sufficed to identify the endpoint and key reference. No credentials were printed, copied into documents or sent to another service. No model load, restart, benchmark or infrastructure action occurred.

## Upstream role evidence, not deployed-model verification

- [BAAI BGE-M3 model card](https://huggingface.co/BAAI/bge-m3): embedding retrieval model supporting dense, sparse and multi-vector approaches. The serving API may expose only a subset. Proposed use is versioned retrieval of relevant code, documents and task evidence, not code generation.
- [Recall-Honcho-8B publisher model card](https://huggingface.co/dman1011/recall-honcho-8b): Qwen3-8B fine-tune specialized for explicit fact extraction using a specific Honcho prompt/output format. General summaries or build-journal extraction require separate validation; the deployed alias is not yet matched to these weights. Proposed fact proposals retain source links and never replace the append-only journal.
- Qwen's family name alone cannot establish the installed variant, quantization, thinking controls or coding quality. No family benchmark is treated as evidence for this endpoint. Bounded code proposals are an owner-requested capability to qualify.

## Bounded next discovery and qualification

Owner-confirmed credential location, now referenced by `SKYBUILD_INFERENCE_SECRET_FILE`: `/home/kevin/my_code/.config/brodson-model.secret`, replacing the temporary path for future discovery. The owner explicitly wants it kept there. A metadata request loaded this file in memory and used the Bearer header; DNS again failed with `gaierror`, errno -3, before HTTP authentication. No secret was printed, copied, committed, moved or deleted. Future requests should use this reference when network resolution is available.

DNS and the model-list request now succeed. Continue to use the generic `.env` settings and external credential reference in memory for any further authorized small requests. Keep TLS verification, refuse redirects carrying credentials to another origin, bound response sizes/time, and sanitize output. Discover actual model IDs, context/output controls, protocol, queue/cancellation semantics and capacity metadata where available; unsupported fields remain unknown. Do not probe administrative reload routes or infer safe concurrency from a model listing.

For coding qualification, pin a small set of representative patches/tests and held-out cases, exact model/template and compact contexts. Fix request/output/reasoning/time/correction caps before calls. Start serially with synthetic or explicitly permitted source material. Measure valid patch application, meaningful checks, acceptance/rework, latency and frontier review tokens through integration. Include refusal and malformed/partial output. A successful smoke test establishes protocol behavior only.

Qualify Recall against source-attributed explicit facts and false/misattributed claims. Qualify BGE against retrieval relevance, stale/deleted source handling and access-scope separation. Do not require either to enable already-qualified Qwen work. Before concurrent adoption, agree SkyBuild's share and test bounded server-side queue/overload behavior in an authorized shared window; verify whether the pools compete for host resources. No such evaluation ran in this session.

The initial capacity defaults are ceilings from the owner report, subject to a smaller available share. Actual model identity, context/quantization, coding acceptance rate, combined throughput, external load visibility, cancellation proof and product-test scheduling remain open. No premium-token savings figure is claimed.
