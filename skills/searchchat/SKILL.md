---
name: searchchat
description: "Search across conversation logs in ~/.claude/projects/. Use when: the user says /searchchat, wants to search chat history, or explore the ~/.claude/projects/ directory."
---

Read `.claude/skills/conversation-compiler/SKILL.md` now.

Tip: To narrow the search scope before full compilation, query the memory index first:
```bash
python "absolute/path/to/VCC_memory.py" search "query" --fuzzy -v
```
This returns matching JSONL paths and chain indices so you can target VCC `--grep` to specific files instead of scanning everything.
