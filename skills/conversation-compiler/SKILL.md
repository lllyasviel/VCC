---
name: conversation-compiler
description: "Compile and deterministically search supported agent-session JSONL with VCC. Use for full, brief, focused, or structured transcript views, parser diagnostics, and history discovery."
---

# Conversation Compiler

Use `scripts/VCC.py` in this skill directory as the canonical VCC runtime. It detects GitHub Copilot CLI, Codex, Claude Code, and DeepSeek Harness records automatically. DeepSeek `.jsonl.zstd` input requires the optional Python `zstandard` package.

## Run the compiler

```bash
python "<this-skill-dir>/scripts/VCC.py" "<session.jsonl>"
```

Pass multiple inputs or a quoted glob when needed:

```bash
python "<this-skill-dir>/scripts/VCC.py" "<sessions>/**/*.jsonl"
```

Choose the smallest mode that answers the task:

- Broad discovery: combine `--literal` or repeated `--term` with `--search-only`; use `--format json|ndjson` for automation. Returned `::rendered` ranges are virtual until the session is materialized.
- Materialized inspection: omit `--search-only`; output goes to the private managed cache unless `-o <dir>` explicitly exports it.
- Regex: use `--grep` only when regex semantics are required. Prefer literal queries and pass `--allow-unsafe-regex` only for a trusted pattern that the conservative guard rejects.
- Recall: pass `--chain-window 2 --diagnostics` to emit only the newest two compaction chains and obtain schema coverage plus `recall_selection`.
- Strict processing: pass `--strict` when an incomplete live tail or any failed input must abort the operation.
- `--cache-dir <dir>`: override the private managed cache root. Without `-o`, materialized compilation uses `${VCC_CACHE_DIR}`, `${XDG_CACHE_HOME}/vcc`, the Windows local app-data cache, or `~/.cache/vcc` in that order.

Run `python "<VCC.py>" --help` for truncation, media, cache-refresh, scoring-limit, and output-format controls instead of assuming their values.

Run deterministic history discovery through the same entry point:

```bash
python "<VCC.py>" history-search "<query>" --current-client codex --format json
```

Use each result's `event_timestamp` as the matching message or tool-event time. Do not infer event time from a dated session path, and do not call `event_timestamp` an experiment start time without confirming the adjacent execution record.

Pass the actual runtime client explicitly when known. Use `--current-session <jsonl>` for current-session compaction recovery. Do not infer the current client merely from an existing history directory.

## Inspect outputs

Choose output lifetime deliberately:

- Explicit compilation: use the private managed cache by default; never modify the source history directory.
- Repeated `readchat` or `recall`: reuse the managed cache so selected-session evidence remains available for follow-up.
- Broad discovery: use `--search-only`; materialize only selected sessions afterward.
- User export: use `-o` and treat the result as persistent.

When views are materialized, the compiler writes:

- `.txt`: high-fidelity semantic rendering of supported events and authoritative line-reference target. It is not a byte-for-byte or event-complete copy of the JSONL.
- `.min.txt`: brief view with tool calls collapsed to references into `.txt`.
- `.view.txt`: matching blocks produced only by a materialized regex search.

Read the compiler's file/line counts first. Then read `.min.txt`; open the cited `.txt` ranges when details matter. For a targeted question, use a literal or multi-term query and inspect the returned block ranges before reading broader transcript sections.

## Preserve evidence

- Treat generated views as reproducible derivatives, not the authoritative record. The source JSONL remains authoritative.
- Keep explicit exports and selected-session views. Do not persist views for every candidate in a broad search.
- Keep cache roots private and out of source control or cloud sync. They can contain source code, commands, and secrets copied from session logs.
- Regenerate cached views after the source log changes or VCC is upgraded. Cache entries may be deleted when no longer needed because they are reproducible.
- Distinguish transcript evidence from the current workspace state; session logs can be stale.
- Report malformed JSON, missing files, unsupported records, and output collisions explicitly. Do not silently substitute another session.
- Treat a nonzero multi-file search exit as a partial failure: usable matches may still be present on stdout, while stderr names skipped inputs. Rerun with `--strict` when all-or-nothing behavior is required.
- Do not claim a live or partially written session is complete merely because its current JSONL compiles.

## Companion skills

Use `readchat` to review a specified session, `searchchat` to locate history across supported clients, and `recall` to recover prior context. Those skills contain their own workflows and depend only on this skill's canonical runtime.
