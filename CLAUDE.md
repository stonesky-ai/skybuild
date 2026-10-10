# SkyBuild instructions for Claude

Read [AGENTS.md](AGENTS.md) for shared repository rules.

Invoke **Close-PR** when asked to “Close-PR”, “close PR”, “close out a PR”, or “close a development branch/cycle”. Read and follow [.claude/skills/close-pr/SKILL.md](.claude/skills/close-pr/SKILL.md). Skill instructions do not grant authority beyond the owner's request and repository policy.

Invoke **petri-audit** for `/petri-audit`, `$petri-audit`, “audit task states”, “Petri evidence audit”, or “actionable task revisions”. Read [.claude/skills/petri-audit/SKILL.md](.claude/skills/petri-audit/SKILL.md). It combines a read-only Python scan with independent model sample review and editable proposals; it never applies task changes.
