---
name: searchchat
description: "Search supported local agent-session history with VCC block references. Use for /searchchat, locating past sessions, checking whether a topic appeared before, or exploring unknown history."
---

# Search Chat

As a VCC companion skill, use VCC's deterministic priority tiers; do not reimplement discovery with ad hoc shell loops.

1. Resolve `../conversation-compiler/scripts/VCC.py` relative to this skill directory. If missing, search installed skill roots for `conversation-compiler/scripts/VCC.py`; stop with a clear dependency error if unavailable.
2. Select source scope:

   - If the user names one or more clients, search only those clients.
   - If the user asks for all/global/cross-platform history, search every existing root and label results by client.
   - Otherwise, identify the client running this skill from explicit harness/runtime context and search that client's root first. Do not infer the current client merely because its directory or environment variable exists.
   - Expand to the remaining existing roots only when the current-client search has no match, only weak/ambiguous matches, or cannot be performed.
   - If the current client cannot be identified reliably, search all existing roots and state that fallback.

3. Run the structured history search. Pass the active runtime client explicitly when known; omit it when unknown so VCC discloses its all-source fallback:

   ```bash
   python "<VCC.py>" history-search "<natural-language anchors>" \
     --current-client "<copilot|codex|claude>" --query-mode all --format json
   ```

   Use `--client <client>` for explicit scope or `--client all` for global search. Use `--query-mode phrase` for an exact literal phrase and `any` only for exploratory recall.
   When the active task JSONL path is available, pass it with `--exclude-session` so the search does not rank its own prompt or tool calls as history evidence.
4. Review the ranked role-aware results. A user/assistant block with all anchors normally outranks an unexplained tool-result hit; scoring selects candidates but never proves a decision.
5. Materialize the strongest sessions in the managed cache with `--literal` or repeated `--term`, then open the cited `.txt` ranges. Add `--cache-dir` only to override the default cache root. Treat virtual `::rendered` lines as discovery evidence only. If the user requires a read-only/no-write workflow, do not materialize; answer from structured discovery output, label it as discovery evidence, and state that broader transcript context was not persisted or inspected.
6. Inspect the report's `errors`, `tiers_searched`, and `expansion_reason`. Partial errors do not invalidate healthy matches, but must be disclosed.
7. Report scope, expansion behavior, grouped session paths, and the strongest materialized evidence.

Do not persist views for unmatched candidates. Keep only selected-session cache entries needed for follow-up, and never commit or sync sensitive transcripts.
