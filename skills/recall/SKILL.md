---
name: recall
description: "Recover actionable context from a supported past agent session and verify current state. Use for /recall, resuming work, referenced sessions, continuation summaries, or compaction recovery."
---

# Recall

Recover the original conversation first, then reconcile it with current reality.

1. Use an explicit JSONL path when provided. For current-session compaction, first resolve the current session/thread identifier or source path from explicit runtime metadata and open that exact log; locate the newest compaction boundary and recover the preceding exchange. Only use content search when current-session identity is unavailable. For other requests, locate the session using the prioritized platform procedure below. Do not treat a continuation summary as authoritative.
2. Resolve `../conversation-compiler/scripts/VCC.py` relative to this skill directory. If missing, search installed skill roots for `conversation-compiler/scripts/VCC.py`; stop with a clear dependency error if unavailable.
3. Materialize the selected JSONL with `--chain-window 2 --diagnostics` in VCC's private managed cache; use `--cache-dir` only to override its location and never write beside the source.
4. For compaction recovery, follow `recall_selection`: read the pre-compaction brief view, then the latest brief view. If no boundary exists, read only the latest view. Expand `--chain-window` only when required evidence is missing, and compare recovered evidence with any continuation summary.
5. Search concrete anchors with `--literal` or repeated `--term`; reserve `--grep` for an explicitly requested regex. Use structured output when comparing candidates.
6. Read the cited `.txt` ranges with surrounding context. Reconstruct intent, rejected alternatives, decisions, unfinished work, and the evidence supporting each claim.
7. Freshly inspect every current workspace file or runtime state needed to continue. Session content may be stale, incomplete, or superseded by external edits.
8. Continue the task from verified state. In the handoff, distinguish:

   - recovered session facts;
   - current-state verification;
   - remaining uncertainty or proposed next work.

## Locate an unknown session

Apply this priority order:

1. Search only explicitly named clients when the user specifies them.
2. Search every existing root immediately when the user explicitly asks for global or cross-platform recall.
3. Otherwise identify the client running this skill from explicit runtime context and search its root first. Do not infer it only from directory or environment-variable presence.
4. Stop at the current-client tier when a strong session match contains the request anchors and enough conversation context to resume safely.
5. Expand to other existing roots only if the current tier has no match, an ambiguous/weak match, or cannot be searched.
6. If the current client is unknown, search all existing roots and disclose the fallback.

Let `history-search` resolve platform defaults rather than duplicating client paths in this skill. Run `python "<VCC.py>" history-search "<anchors>" --current-client <client> --format json`. For current-session compaction, also pass `--current-session <exact-jsonl>`; this exact tier is searched before history roots. Use `--client` for explicit scope and omit `--current-client` when runtime identity is genuinely unknown. Materialize the chosen session before using `.txt` ranges as recovery evidence. Report whether expansion occurred and why.

If a batch search exits nonzero, retain valid stdout matches and inspect stderr for failed inputs. Do not select a different session merely because the exact current log has a tolerable incomplete tail; VCC ignores only an unterminated malformed final line by default. Use `--strict` when partial recovery is unacceptable.

Honor explicit read-only/no-write constraints: skip cache materialization, use structured discovery evidence, and disclose that full chronological reconstruction was not performed. If cache writes fail without such a constraint, use discovery-only evidence only when it is sufficient; otherwise stop. When searching for an older session, pass the active task path with `--exclude-session` when available to prevent circular self-matches.

Treat cached views as reproducible sensitive data. Keep selected-session entries for follow-up, regenerate them when the source changes, and do not commit or sync them.
