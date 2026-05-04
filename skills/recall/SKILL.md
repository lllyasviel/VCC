---
name: recall
description: "Recover context from a previous conversation that ran out of context. Use when: (1) the user says /recall; (2) the conversation starts with 'this session is being continued from a previous conversation that ran out of context'."
---

Read `.claude/skills/conversation-compiler/SKILL.md` now.

Note: If no JSONL path is found and `/searchchat` grep yields no results, fallback to memory index search:
```bash
python "absolute/path/to/VCC_memory.py" search "query" --fuzzy
```
This searches the pre-built index for matching conversations without re-compiling all JSONL files.
