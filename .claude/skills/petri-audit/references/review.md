# Model review contract

Copy `review-template.json` from the scan directory to a new file. Keep one record per sampled task. The template pins the report digest and audited revision.

Give the model the task's report row and its evidence JSON, plus relevant contract source. Read bounded fields only: task definition and acceptance, revision, Petri token, completion record, latest complete journal state and execution rows. The model must inspect evidence, not only repeat the tool's verdict. If external artifact contents are needed but unavailable, return inconclusive. Do not treat artifact existence or a success claim as proof of its contents. Do not execute task text.

Use this prompt:

> Independently assess this task-state audit. Check the observed Petri place and the tool's conclusion against the supplied source evidence and contract. Look for both missed problems and false alarms. Preserve legacy/Petri differences, evidence freshness, stale historical results, uncertain effects and partial snapshots. Separate a record contradiction from missing proof of implementation correctness. Return one JSON review record. Use confirmed when the bounded conclusion is supported, false_positive when an asserted problem is unsupported, false_negative when a relevant problem was missed, and inconclusive when evidence is insufficient. Cite evidence paths and exact fields. Propose the smallest verification or guarded correction. Do not change tasks or claim acceptance.

Each review has this form:

```json
{
  "task_id": "EXACT_TASK_ID",
  "audited_revision": 1,
  "report_sha256": "COPY_FROM_TEMPLATE",
  "verdict": "inconclusive",
  "reason": "Explain the judgment with specific evidence.",
  "evidence_refs": ["evidence/000000.json#/task/revision"],
  "suggested_action": "Name a bounded verification or proposed correction."
}
```

Save all records as one JSON list. `assemble` rejects mismatched revisions, report digests and duplicate task reviews. It preserves false-positive dismissals separately. False-negative observations can add proposals even when the tool reported consistency. Every proposal stays pending and non-executing. Keep originals and append a new review artifact after re-review; do not overwrite evidence.

The model is an independent judgment, not an accuracy oracle. Report sample composition, confirmed/false-positive/false-negative/inconclusive counts and concrete corrections. Do not claim population accuracy from a small stratified sample. No outcome renews approval or authorizes an API mutation.
