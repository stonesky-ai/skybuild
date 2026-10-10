# Existing-project migration method

This is a reusable planning method, not an implemented importer or a change to the runtime contract. Verify the target project's actual workflow and supported operations before execution. Preserve uncertainty where old evidence cannot establish present intent or completion.

## Preserve the source and authority

Inventory tasks, histories, source revisions, acceptance criteria, evidence references, milestones, dependencies and current owners. Record the source system and cutoff time. Use complete pagination where required. Preserve source IDs; propose an explicit ID mapping for collisions rather than silently merging records.

Prepare an authority manifest naming the project, source authority, frozen source, destination, authorized operator, permitted operations and cutover conditions. Keep one editable authority at a time. Rehearse import and verify counts, identities, history and relationships before an authorized cutover; reconcile destination effects before any retry. A partial import is not permission for a second writer or a blind reimport.

Preserve original event times and actors as source provenance. Record the migration actor and actual migration time separately. Do not fabricate past Petri transitions, passing checks or acceptance attestations. Append migration and later correction events; do not rewrite history.

## Hold pending reconciliation

Propose legacy records for Hold with a clear reason: “Legacy classification requires evidence and intent reconciliation.” Capture the prior status, phase, source revision, evidence references and migration batch. Keep the legacy marker as provenance after reconciliation; mark the reconciliation resolved rather than erasing its origin.

The hold is neither a finding of incompleteness nor an automatic revocation of historical completion. A workflow hold does not prove a running process has stopped. Reconcile active claims, workers and external effects before changing ownership or releasing replacement work.

Do not assume an enrollment endpoint forces Hold. Some implementations select a place from the existing definition or preserve compatibility fields. Inspect enrollment and guarded-control contracts. If the desired quarantine cannot be established through qualified operations, keep the proposed import undispatched and record that capability gap. Never force managed metadata directly.

## Map the work graph

Read each edge in both directions. Identify prerequisites, dependents, shared outputs, missing targets, cycles and external dependencies. A chain can include tasks from several milestones.

Keep two relationships distinct:

- **Requires output:** task A requires an outcome from task B. This can block execution.
- **Contributes to milestone:** task A supports objective M. This does not imply M is an execution prerequisite.

Record edge direction and meaning explicitly. Use the target's supported representation; do not encode milestone membership as a blocking dependency merely because one dependency field exists. “Milestone” is a planning role until its runtime representation is verified.

Preserve recorded edges. Propose inferred links separately with source evidence, confidence and an unresolved/approved/rejected decision. Detect cycles before proposing execution readiness; do not break a cycle arbitrarily. An obsolete milestone only identifies a group for review. Shared prerequisites or useful minor outcomes can survive independently or support a replacement milestone.

## Interview humans about intent

Group questions by connected work and milestone. Present a compact diagram or edge list, prior classifications, evidence, proposed dispositions and useful exceptions. Ask about the objective first, then ambiguous tasks. Do not ask the owner to review every row when one answer can resolve a group.

Suggested questions:

1. Is this milestone still an objective, replaced, completed, or abandoned? What evidence supports that decision?
2. Which outputs in this chain remain useful? Does an active milestone or external consumer need them?
3. Should those tasks move to a named milestone, remain independent, or wait for a specific trigger?
4. Which proposed dependencies are real prerequisites, and which are only historical associations?
5. For uncertain historical completion, what evidence or bounded verification would be sufficient? Who can accept it?

Record answers as durable decisions: decision ID, human identity, time, rationale, affected task revisions, approved edge changes, replacement objectives and unresolved exceptions. Silence is not approval. A changed affected revision requires reconciliation before applying the answer. Keep intent decisions separate from evidence of implementation correctness.

## Reconcile through ordinary tasks

Prefer one linked reconciliation task for a bounded original task or coherent group. Give it specific evidence to inspect, expected outputs, authority limits and acceptance criteria. Preserve each original identity and criteria. This is an ordinary task, not a parallel migration queue; its output is a proposed disposition of the linked originals. Track its own lifecycle separately from theirs. Prevent duplicate assignments and reconcile existing ownership before dispatch.

Direct release of an original can be appropriate when a clearly recorded reconciliation mandate fits its definition. Avoid silently replacing original acceptance criteria with “investigate” or causing a worker to rebuild work that already exists. Make any scope revision explicit and preserve its prior version.

Propose the smallest supported outcome:

| Evidence and intent | Proposed disposition |
| --- | --- |
| Complete and still relevant | Normal completion evidence and authorized acceptance path; do not fabricate a new implementation |
| Incomplete, useful and defined | Ready for bounded work after dependency and authority checks |
| Missing evidence or unresolved intent | Hold with a named question, resolver and next action |
| Waiting for a date or milestone | Deferred with a concrete supported trigger |
| Obsolete, duplicate or replaced | Supported retirement/supersession disposition with reason and lineage; if unsupported, retain Hold and record the gap |

Historical “done” means recorded as done. Evidence coverage can be verified, unavailable or contradicted. Missing modern validation alone is not proof that old work must be reopened. Proposals and human intent decisions cannot bypass completion, review, integration or execution gates.

## Editable decision packet and pilot

Use operator-selected Markdown and JSON files, outside any retired writable ledger. Keep source snapshots separate from decisions. A useful packet contains:

- Scope, source cutoff, authority manifest and exact source references.
- Per-task identity, source revision, original classification, evidence coverage and proposed hold reason.
- Typed graph edges with evidence, confidence and decisions.
- Grouped HIL questions and durable answers.
- Proposed dispositions, scope changes, expected live revisions and reconciliation-task links.
- Pilot membership, acceptance checks, unresolved capabilities and cutover/recovery conditions.

Use pending decisions and empty changes until supported, for example:

```json
{
  "source_task_id": "SOURCE-TASK-ID",
  "source_revision": "SOURCE-REVISION",
  "destination_task_id": null,
  "expected_live_revision": null,
  "legacy": true,
  "original_classification": {"status": "done", "phase": null},
  "evidence_coverage": "unverified",
  "proposed_place": "hold",
  "decision": "pending",
  "decision_refs": [],
  "proposed_changes": {},
  "reconciliation_task_id": null
}
```

This is an editable planning example, not an accepted API payload or an executable schema. Add source evidence, edge proposals and interview groups as needed; do not invent identifiers to imply existing records.

Pilot a small mix of historically done, blocked and unfinished tasks, including an obsolete milestone chain and a useful shared task. Verify preserved history, correct proposed dispositions, dependency direction, no duplicate external work, and explicit unresolved questions. Release subsequent batches only after reviewing those results under existing authority.

A state auditor can help attach evidence where available. Verify its actual coverage: a legacy `not_applicable` verdict is not an audit of journal continuity or physical activity. The desired tool policy is common checks for every task, with only genuinely Petri-specific checks marked inapplicable. Report missing tooling as follow-up work, not completed migration evidence.
