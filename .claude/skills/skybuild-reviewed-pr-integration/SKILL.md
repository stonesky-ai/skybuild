---
name: skybuild-reviewed-pr-integration
description: Validate an exact independently reviewed SkyBuild PR candidate and, when already authorized, merge it with a combined gate and published-tree check.
---

# Reviewed PR integration

Trigger only after the required independent review names the exact PR head. Read `scripts/integrate_reviewed_pr.py --help`. Supply exact PR number, base branch, expected head and base SHAs, a retained review-evidence file, and `--checkout` for SkyBuild. Default mode validates the candidate without merging. Add `--merge` only when integration is authorized and the required gate is selected. The script stops on changed refs and compares the published tree with the tested tree.

GitHub lacks an atomic expected-base merge guard. A base race after the last check can publish before the script detects a mismatch. This helper is not the qualified automatic publisher described in the architecture; use the repository's review and publication rules.

The built-in publication gate requires 10 GiB of available memory on this host, leaving test headroom above the owner’s 8 GiB reserve. If admission fails, retain the failed run and wait for resources; do not substitute a weaker custom gate or reduce the reserve.
