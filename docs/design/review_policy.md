# Review and compactness policy

Derived from architecture revision A33, 2026-10-08. [Architecture section 13](architecture.md#13-compact-component-design) governs. Independent adversarial review is accepted; exact metric/enforcement mechanics below are proposed. The owner explicitly defers dedicated complexity/size gates and their GUI until the self-building MVP is running. This document is planning, not implemented policy.

## MVP review path

Run existing applicable project checks and targeted tests before spending reviewer tokens. Each code task then receives review in a separate qualified model session; the same model/provider is allowed. Supply the task contract, exact diff and relevant source, test evidence and unresolved findings. The author cannot approve its own patch. One bounded reviewer covers applicable adversarial perspectives; multiple permanent reviewer roles are unnecessary.

Return pass, actionable changes requested, or blocked with missing evidence and a resolver. Every blocking finding names the exact revision, affected file/line/symbol or contract, severity, evidence or plausible failure scenario, and a concrete correction or test with expected behavior. Examples include an unauthorized request accepted by a named route, a repeated timeout launching duplicate work, or a missing test of a specific boundary. Distinguish demonstrated defects, unresolved hypotheses and optional suggestions. Do not invent findings to meet a quota.

The author responds to each finding with a fix/test artifact or a reasoned dispute. Dispositions append to the journal; they do not erase the finding. Corrections rerun affected checks and return to independent review. Reviewers who edit the candidate become authors of that edit. Head/contract/policy changes invalidate affected acceptance. Unresolved required findings block bundling; unavailable reviewer capacity or exhausted correction allowance leaves a named blocker. Never rotate reviewers merely to obtain a pass. These operations share the original task, budgets and model cutoff.

## Deferred mechanical profile

Implement under SKYBUILD-QUALITY-GATES after SKYBUILD-SELF-BUILD-MVP is running. Begin with deterministic measurement and explicit rule selection; pin tool versions and counting semantics before enforcement. Defaults are practical tool conventions, not a universal Python code-quality standard.

| Criterion | Proposed initial policy | Meaning |
| --- | --- | --- |
| Control-flow complexity | Ruff `C901`, maximum McCabe complexity 10 | New or worsened violations block; a trusted legacy baseline preserves visible inherited debt without forcing unrelated refactors. |
| Function size | Ruff `PLR0915`, warning above 50 statements | Request a concrete simplification assessment. Size alone does not prove bad design or justify deleting behavior. |
| Code volume and growth | Report source/logical lines per changed function/module and added/deleted totals, separating production, tests and generated files | Explain growth against accepted scope. No universal maximum file length or mandatory shrink percentage is selected. |
| Existing mechanical correctness | Project-selected syntax, lint, type and targeted test checks | Reuse trusted existing commands; avoid stacking overlapping lint tools or requiring every check on every unaffected file. |
| Duplication and scaffolding | Focused evidence from changed code and dependencies | Examine repeated behavior, unused paths and unnecessary layers; do not infer dead code from one empty reference search. |

GUI settings cover thresholds, severity, applicable paths, baseline/exceptions and correction limits. Owner/admin policy changes are journaled versions. Candidate edits to linter configuration, suppressions, generated-file labels or reviewer prompts cannot lower their own acceptance requirements. Baselines bind the original symbol/rule and evidence; moving or renaming code cannot silently launder debt. Narrow exceptions retain reason, scope, authorized actor and review trigger. Do not combine incompatible metrics from different tool versions as if they were identical.

Show before/after metrics and unresolved findings on the task page. A required analyzer failure means unavailable evidence, not a zero score. Deterministic failures return directly to rework. Required exceptions or unresolved disputes use existing task/owner-question handling, not an additional multi-person approval framework. Exact correction-count defaults and exception lifetime are implementation-time choices.

## Adversarial criteria and bounded prompt

Review applicable criteria: accepted behavior and API/data contracts; boundary/error cases; retries, idempotency and concurrent updates; authorization and sensitive data; resource/query limits; tests that detect the claimed defect; and code proportional to the actual requirement. Additional tests should target a concrete missing behavior or failure path. Avoid boilerplate tests that merely mirror implementation. Full integration remains a combined bundle check.

The following is an authored compact prompt template, informed by established review guidance. It is not claimed to be a proven or popular universal prompt. Keep its version with evidence and qualify it against representative accepted/rejected changes before broad autonomous use.

> Independently review this exact patch against its task contract and relevant source. First inspect available mechanical and test evidence. Challenge correctness, failure paths and assumptions; identify concrete missing tests. Find unnecessary behavior, duplication, one-use wrappers, speculative abstractions or redundant test scaffolding. Propose the smallest behavior-preserving correction. Preserve required interfaces, transaction/authority checks, observability and useful tests. Do not optimize line count by minifying or moving complexity elsewhere. For each finding provide severity, exact location or contract, evidence/scenario, required change or test, and expected result. Separate blocking defects from optional suggestions; return no findings when justified. State missing evidence. Do not edit or approve work you authored.

Use narrower perspective additions only for the affected risk. No fixed multi-agent debate is required. Measure escaped defects, false-positive findings, correction cycles and total model usage through integration; fewer lines or more objections alone do not demonstrate improvement. Qualified inexpensive models may review bounded task classes; high-consequence authority/concurrency changes need an appropriately qualified reviewer. Shared-endpoint coding qualification alone does not establish review qualification.

## Primary-source basis

Checked 2026-10-08. Ruff documents [McCabe C901](https://docs.astral.sh/ruff/rules/complex-structure/) and a [default maximum of 10](https://docs.astral.sh/ruff/settings/#lint_mccabe_max-complexity). Its [PLR0915 rule](https://docs.astral.sh/ruff/rules/too-many-statements/) defaults to 50 statements. These rules must be explicitly selected in the proposed project profile; a threshold value alone does not enable a rule.

[Google's reviewer guidance](https://google.github.io/eng-practices/review/reviewer/looking-for.html) discusses design, functionality, unnecessary generality, useful tests and distinguishing optional style comments. The workflow, GUI, budgets and proposed enforcement here are SkyBuild design choices, not guarantees made by those sources. No new linter, runtime gate or model evaluation was installed or run for this planning revision.
