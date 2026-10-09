---
name: skybuild-reviewed-pr-integration
description: Validate an exact independently reviewed SkyBuild PR candidate and, when already authorized, merge it with a combined gate and published-tree check.
---

# Reviewed PR integration

Trigger only after the required independent review names the exact PR head. Read `scripts/integrate_reviewed_pr.py --help`. Supply exact PR number, base branch, expected head and base SHAs, a retained review-evidence file, and `--checkout` for SkyBuild. Default mode validates the candidate without merging. Add `--merge` only when integration is authorized and the required gate is selected. The script stops on changed refs and compares the published tree with the tested tree.

The default project gate records a durable run artifact pinned to the exact candidate commit and tree. Supply `--gate-artifact /absolute/private/evidence/run.json` to choose its exclusive destination, or retain the automatically allocated path reported in the result. The parent must already be an owned private directory outside the candidate. Never reuse an artifact path for a new run. Confirm success, cleanup, and the published-tree result; a passing test summary alone does not establish acceptance. Custom gates remain validation-only and cannot use this artifact option.

GitHub lacks an atomic expected-base merge guard. A base race after the last check can publish before the script detects a mismatch. This helper is not the qualified automatic publisher described in the architecture; use the repository's review and publication rules.
