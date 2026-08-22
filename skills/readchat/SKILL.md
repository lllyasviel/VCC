---
name: readchat
description: "Compile and review one known agent session with VCC. Use for /readchat, events.jsonl, rollout logs, a supplied conversation JSONL, or evidence from a specific past session."
---

# Read Chat

As a VCC companion skill, review the requested session without relying on summaries alone.

1. Resolve the input path. If the user provides a session directory, select its conversation JSONL explicitly; for Copilot CLI this is normally `events.jsonl`. Do not guess among multiple plausible files.
2. Resolve the companion compiler as `../conversation-compiler/scripts/VCC.py` relative to this skill directory. If it is absent, search the active client's installed skill roots for `conversation-compiler/scripts/VCC.py`; stop with a clear dependency error if it cannot be found.
3. Materialize the selected session in VCC's private managed cache. Override it with `--cache-dir` only when the environment requires a different private location; never write derived views beside the source:

   ```bash
   python "<VCC.py>" "<session.jsonl>"
   ```

4. Inspect reported sizes before reading. For a general session review, read the generated `.min.txt` files chronologically; for a targeted question, search concrete anchors first and read only the relevant brief sections. Higher numbered chains are newer.
5. Use `--literal` or repeated `--term` for ordinary names, paths, decisions, and errors. Reserve `--grep` for a user-requested or genuinely necessary regular expression.
6. Open the cited `.txt` ranges with enough surrounding lines to reconstruct the exchange. Use `.txt`, not `.min.txt`, as evidence for exact details.
7. If cache materialization is unavailable, use `--search-only` only when concrete anchors can answer the question, disclose the reduced evidence, and otherwise stop with the write error.
8. Answer with the recovered facts, unresolved ambiguity, and the source session path. Label any comparison with current files as a separate, freshly verified observation.

Treat cached views as reproducible sensitive data. Keep them for follow-up questions, regenerate them when the JSONL changes, and do not commit or sync them.
