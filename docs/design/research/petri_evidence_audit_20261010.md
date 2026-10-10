# Petri evidence audit qualification — 2026-10-10

The read-only `skybuild.task_state_audit` command scans the REST task inventory,
attaches per-task evidence, selects a random model-review sample and assembles
editable revision proposals. Invoke the project skill with `/petri-audit`.
The scan uses Python only. The skill supplies the independent model step.

## Profile comparison

Four live scans covered all 107 tasks in project `skybuild`. Each used stable-ID
cursor pagination until a short page and checked the inventory again at the end.
All inventories were unchanged. All scans found 33 consistent Petri records and
74 legacy records without Petri state. None established implementation correctness.

| Profile | Page size | Duration | Evidence coverage |
| --- | ---: | ---: | --- |
| basic | 17 | 9.4 s | Task, workflow and final task read |
| history | 100 | 11.3 s | Basic plus bounded journal |
| full, original | 31 | 12.4 s | History plus bounded cached execution rows |
| full, corrected | 31 | 13.6 s | Full plus guarded-transition compatibility check |

Use the corrected full profile by default. It checks more independent records
for modest additional read cost. This choice is based on evidence coverage and
adversarial review, not a measured population accuracy ranking. The history limit
was 1,000 and the execution limit was 100. Truncation remains unknown.

## Independent model review

The scan selected 12 tasks with stratified random sampling, without replacement,
using seed `20261010`. The sample contained three Ready, two Validating, two
Integrating, two Done, one Hold and two legacy records. It included consistent
records to test for missed defects. Deferred and Working were absent from the
live inventory. The sample was frozen before the model reviewed it.

The independent model confirmed all 12 original bounded conclusions. It found no
sampled false positives or false negatives. These judgments do not prove task
acceptance, physical inactivity or population accuracy. Legacy records were
classified as outside the Petri checks, rather than defective.

Separate adversarial code review found a synthetic false negative: all API views
and the latest journal snapshot could agree on incorrect compatibility fields
after a guarded workflow transition. The corrected tool compares the transition's
`event_facts.to_place`, current token place, status and phase. It excludes legacy
enrollment, which intentionally preserves compatibility fields. Regression tests
cover status-only, phase-only and combined corruption, plus that exception.

Raw scans and model reviews remain private operator artifacts. Reports record
source-content SHA-256 values, evidence-content SHA-256 values, settings and
revision bindings. A matching local source contract does not attest the deployed
runtime version. The model must review fresh report bindings after a rescan.

## Editable output

The scan writes `report.json`, `sample.json`, `review-template.json`, evidence
packets and initial `revisions.json` into a new private directory. Fill the review
template using the skill's review contract. Run `assemble` to create a separate
`revisions.json`. Each proposal has an expected task revision, pending decision,
editable `proposed_changes`, actions and evidence references. False-positive
dismissals remain separate. No command applies revisions or calls a model.

Use the skill's CLI examples. Omit the seed for a new recorded random sample;
choose uniform sampling for uniform selection. A later authorized correction
must reread the live task revision and use the guarded workflow. Unknown evidence
requires verification; it does not authorize reopening a task.
