# Review and quality decisions

Each section retains its original ADR ID. Status applies to the decision, not implementation. [Architecture](../design/architecture.md) governs current requirements.

<a id="adr-0033"></a>
## ADR 0033: Independent review and deferred complexity gates

**Accepted separate adversarial review and post-MVP quality feature; metric defaults proposed, 2026-10-08.** Run applicable project checks, then a bounded adversarial review in a separate qualified model session for every code task. Findings name affected code/contracts and concrete fixes or tests. Same provider/model may author and review only through separate sessions; author cannot self-approve. Rework, findings, dispositions, tests and budgets stay with the task. Stale passes, unresolved blockers or absent reviewer capacity cannot advance integration.

The MVP is a running SkyBuild that rebuilds/extends itself with parallel workers under controls, qualified author/reviewer paths and bundled integration. The launch-free API is earlier. Defer dedicated complexity/size gates and their GUI until after MVP. Later proposed defaults include Ruff McCabe 10 and a warning above 50 statements per function; neither is an accepted universal limit. Preserve necessary behavior, safeguards and meaningful tests; candidate code cannot weaken trusted checks. See [review policy](../design/review_policy.md) and architecture sections 3, 13 and 16.

<a id="adr-0034"></a>
## ADR 0034: Post-MVP debt and serialized repository review

**Accepted priority, thresholds and serialization; counting/fencing proposed, 2026-10-08.** Early after MVP, pin Ruff including McCabe in review and fix every actual repository debt finding. A legacy baseline cannot silently waive debt indefinitely; false positives or non-debt need narrow authorized dispositions. Fixes retain behavior/tests and pass ordinary review/publication.

Run a background whole-repository review after the first of 1,000 accepted commits or 5,000 changed source lines since the last completed review. Allow only one active review; the next waits until every actionable prior finding has an accepted published fix. Deduplicate accidental runs and findings. Expose both thresholds in the GUI later.

Proposed: count accepted source additions and deletions once across merges, excluding docs/generated files. Persist baseline, counters, trigger, source revision, run and finding IDs. Fence the review slot; retain it through unknown outcomes and coalesce triggers while held. Model budgets, billing, review qualification and cutoff still apply. Recurring review begins after initial debt cleanup; it is not an MVP prerequisite. Architecture sections 3, 13 and 16.
