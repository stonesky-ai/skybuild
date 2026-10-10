# SkyBuild instructions for Claude

Read [AGENTS.md](AGENTS.md) for shared repository rules.

Invoke [coding preflight](.claude/skills/skybuild-coding-preflight/SKILL.md) for “resume work,” a new checkout, missing paths, unfetched refs, wrong source imports or uv-cache errors. Run `scripts/coding_preflight.py` with only the relevant prerequisite checks and stop dependent work on failure.

Invoke [session failure review](.claude/skills/skybuild-session-failures/SKILL.md) for “audit session transcripts,” “repeated tool errors,” or “failed exit(2).” Its Python helper combines bounded log selection, invocation/result pairing and safe diagnostics.

Invoke **Close-PR** when asked to “Close-PR”, “close PR”, “close out a PR”, or “close a development branch/cycle”. Read and follow [.claude/skills/close-pr/SKILL.md](.claude/skills/close-pr/SKILL.md). Skill instructions do not grant authority beyond the owner's request and repository policy.
