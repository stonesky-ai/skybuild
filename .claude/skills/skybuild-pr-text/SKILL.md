---
name: skybuild-pr-text
description: Post a SkyBuild PR body or comment from a UTF-8 file while preserving real newlines and verifying body updates.
---

# Pull request text

Trigger for multiline PR descriptions, updates and summary comments. Write the final text in a UTF-8 file, then use `scripts/pr_text.py body --pr <number> --file <path> --checkout <SkyBuild root>` or `comment` with the same arguments. The script checks SkyBuild repository identity and verifies body writes. Do not turn a draft into a posted comment without the task's authorization.
