# VCC: View-oriented Conversation Compiler

[English](README.md) | [简体中文](README_cn.md) | [日本語](README_jp.md)

VCC compiles local agent-session JSONL into readable, searchable transcript views with stable block roles and line-range references. It supports GitHub Copilot CLI, Codex, and Claude Code and detects their record formats automatically.

VCC is the implementation accompanying “View-oriented Conversation Compiler for Agent Trace Analysis” ([paper](https://arxiv.org/abs/2603.29678)). Academic reproduction materials live in [VCC-experiments](https://github.com/lllyasviel/VCC-experiments).

## Supported clients

| Client | Typical local input | Normalized content |
|---|---|---|
| GitHub Copilot CLI | `${COPILOT_HOME:-$HOME/.copilot}/session-state/*/events.jsonl` | messages, reasoning, tools, results, compaction |
| Codex | `${CODEX_HOME:-$HOME/.codex}/sessions/YYYY/MM/DD/rollout-*.jsonl` | messages and function/custom tool events |
| Claude Code | `$HOME/.claude/projects/**/*.jsonl` | messages, thinking, tools, results, compaction |

The source JSONL remains authoritative. Generated views are reproducible derivatives and can become stale when a live session is appended.

### Default source priority

`searchchat` and `recall` do not search all clients unconditionally:

1. An explicitly requested client or client set always wins.
2. An explicit global/cross-platform request searches every existing source.
3. Otherwise VCC searches the current agent client's history first.
4. Other clients are searched only when the first tier has no reliable match, is ambiguous, or is unavailable.
5. If the current client cannot be identified from runtime context, VCC falls back to all existing sources and reports that fallback.

Directory presence alone does not identify the current agent. Search results state which tier and roots were used.

## Skills

VCC ships as four companion skills that must be installed together:

| Skill | Purpose |
|---|---|
| `conversation-compiler` | Compile known JSONL files and inspect artifacts directly |
| `readchat` | Review one known session with exact transcript evidence |
| `searchchat` | Discover sessions across local history without materializing every candidate |
| `recall` | Recover prior decisions and reconcile them with current workspace state |

See [INSTALL.md](INSTALL.md) for client-specific installation and verification. See [SKILLS.md](SKILLS.md) for skill descriptions, packaging, portability, and release rules.

## Quick start

Compile one session into VCC's private managed cache:

```bash
python "skills/conversation-compiler/scripts/VCC.py" "path/to/session.jsonl"
```

Search many sessions without writing transcript files:

```bash
python "skills/conversation-compiler/scripts/VCC.py" "path/to/**/*.jsonl" \
  --grep "literal-or-regex" --search-only
```

Prefer explicit query semantics and structured output in automation:

```bash
python "skills/conversation-compiler/scripts/VCC.py" "path/to/**/*.jsonl" \
  --term VCC --term cache --match all --ignore-case --format ndjson --search-only
```

Search local histories with deterministic current-client priority:

```bash
python "skills/conversation-compiler/scripts/VCC.py" history-search "VCC cache" \
  --current-client codex --format json
```

Materialize only a selected session in a private managed cache:

```bash
python "skills/conversation-compiler/scripts/VCC.py" "path/to/selected.jsonl" \
  --grep "literal-or-regex"
```

Without `-o`, VCC uses `${VCC_CACHE_DIR}`, `${XDG_CACHE_HOME}/vcc`, the Windows local app-data cache, or `~/.cache/vcc` in that order. Use `--cache-dir` only to override that private location. Use `-o <dir>` only for an explicit export. VCC rejects equal input stems in one shared export directory.

## Output views

| Artifact | Created when | Purpose |
|---|---|---|
| `.txt` | materialized compile | High-fidelity semantic view and line-reference target |
| `.min.txt` | materialized compile | Brief chronological view with collapsed tool references |
| `.view.txt` | materialized compile with `--grep` | Matching blocks in conversation structure |
| stdout matches | `--grep` | Reverse-chronological, role-tagged match list |
| `metadata.json` | managed cache | Source path, size, timestamps, generation parameters, and artifact hashes |

`--search-only` writes none of these artifacts. Its `::rendered` ranges are virtual discovery references; rerun the selected source without `--search-only` before citing or opening exact transcript ranges.

## Output lifetime policy

VCC does not maintain a memory database or upload session content. It does create local derived files when a view is materialized.

- Explicit compile: use the private managed cache and never modify the source history directory.
- `readchat` / `recall`: reuse selected-session cache entries for follow-up.
- Broad `searchchat`: use `--search-only`; do not persist unmatched candidates.
- Explicit export: use `-o`; treat outputs as user-owned persistent artifacts.

Cache entries are reproducible. Regenerate them after the source JSONL changes or VCC is upgraded, and delete old entries when they are no longer referenced.
Valid full/brief cache entries are reused by default. Canonical source path, size, timestamps, file identity, truncation parameters, and the VCC version are part of the validity key; Windows additionally verifies source SHA-256 because replacement can preserve file identity there. Use `--cache-policy refresh` to force regeneration.

## Structured search and ranking

Use `--literal` for one exact literal string, repeated `--term` with `--match all|any` for multi-anchor queries, and `--grep` only for regex. `--format json|ndjson` emits schema-versioned block records containing source, role, full-view range, matched patterns, matching lines, and a deterministic relevance score. User and assistant matches outrank unexplained tool output; ranking chooses candidates but is not evidence of a conclusion.

`history-search` enumerates Copilot, Codex, and Claude roots, searches the explicitly supplied current client first, and expands only on no/weak matches unless scope or expansion is overridden. If the current client is unknown, it searches all sources and reports that fallback. `--current-session` adds an exact first tier for compaction recovery.

Each structured match includes `event_timestamp`, taken from the matched source event when available. Text output labels it as `event=...`. Treat this as the time of the matching message or tool event, not automatically as the time an experiment started or an artifact was produced; inspect adjacent tool calls when that distinction matters. Dates embedded in session paths identify where a client archived or first created the session and may differ from the matching event time when a session is reused.

Diagnostics schema v2 separates source accounting from normalized output. `source_records_supported + source_records_ignored + source_records_unknown` always equals `source_records_total`; `normalized_records_emitted` may differ because one source event can emit multiple normalized records. `recall_selection` identifies the pre-compaction and latest brief views so an agent can skip older chains by default.

For recall, pass `--chain-window 2` to materialize only the two selected chains. VCC rejects regexes longer than 4096 characters and common nested-unbounded-repeat/backreference patterns by default; use literal or term queries whenever possible. `--allow-unsafe-regex` is an explicit trusted-input escape hatch, not a timeout guarantee.

## Why VCC is more than text grep

VCC lexes supported formats, normalizes them into one conversation model, parses role-aware blocks, assigns a stable full-view line coordinate system, and lowers that representation into full, brief, and focused views. Search results therefore identify whether a match came from a user message, assistant output, reasoning, tool input, or tool result and provide the full block range needed for context.

## Implementation and algorithm

For each input file, VCC runs this deterministic pipeline:

1. **Lex** JSONL records incrementally and detect Copilot, Codex, or Claude format.
2. **Normalize** client-specific messages and tool events into one record model.
3. **Merge and split** streamed assistant chunks and compaction chains.
4. **Parse** messages, reasoning, tools, results, and media references into an intermediate representation (IR).
5. **Assign lines once** on the full IR so every derived view shares one coordinate system.
6. **Lower** the IR into brief and regex-focused selections without renumbering full-view lines.
7. **Emit** materialized files or stream search-only matches.

The executable is intentionally thin. The implementation lives in `scripts/vcc/` with one-way dependencies:

| Module | Responsibility |
|---|---|
| `common.py` | Shared version, limits, errors, and text utilities |
| `normalizers.py` | Codex and GitHub Copilot client-specific schema adapters |
| `parser.py` | Client detection, JSONL validation, chain construction, media handling, diagnostics, and IR construction |
| `renderer.py` | Stable line assignment and full, brief, and focused view lowering |
| `query.py` | Block matching, deterministic scoring, per-source limiting, and text/JSON/NDJSON output |
| `cache.py` | Atomic writes, cache keys, manifests, integrity validation, cleanup, and permissions |
| `compiler.py` | One-session application pipeline connecting parser, renderer, and storage |
| `cli.py` | Argument validation, glob expansion, multi-input isolation, cache policy, and exit status |

`scripts/VCC.py` only configures the executable and dispatches to `vcc.cli`; `history_search.py` remains a separate history-discovery service that invokes the same public CLI protocol. Internal modules do not depend on the entry point, and parser/renderer/query do not depend on CLI policy.

Images and documents embedded as base64 are decoded only for materialized views; `--search-only` keeps placeholders and does not decode media. Tool calls and results are linked by tool ID. The full view is authoritative for VCC line references, but the source JSONL remains authoritative for unsupported or intentionally omitted events.

## Time and space complexity

Let, for one file:

- `C` be decoded textual/JSON content size;
- `R` be JSONL record count;
- `B` be IR node/section count;
- `L` be rendered output size;
- `M` be total decoded media bytes and `Mmax` the largest single decoded payload;
- `F` be the number of input files.

| Stage | Time | Peak memory / disk notes |
|---|---|---|
| Input expansion and mtime sort | `O(F log F)` | `O(F)` paths |
| Lex + normalize | `O(C + R)` | `O(R)` parsed records plus the current raw line |
| Merge + chain split | `O(R)` | `O(R)` |
| Parse + IR construction | `O(C + B + M)` | `O(C + B + Mmax)` transient memory; up to `O(M)` media output |
| Line assignment + emit | `O(B + L)` | up to `O(L)` rendered buffers |
| Brief/focused lowering | `O(C + B)` | section and visibility indexes are built once per IR |
| Regex matching | pattern-dependent | simple/literal patterns are usually near `O(C)`; pathological Python `re` patterns can backtrack superlinearly |

Therefore one materialized file is linear in its content and output aside from pattern-dependent regex behavior, with bound `O(C + B + L + M)`. Peak working memory is `O(C + B + L + Mmax)` for the current file. VCC explicitly releases each file's result before processing the next, so peak working memory is based on the largest file rather than the sum of all files.

Persistent disk usage for one materialized file is `O(Lfull + Lbrief + Lview + M)`; across files it is the sum of those terms plus `O(F)` small cache metadata. `--search-only` uses `O(1)` persistent output space and does not decode embedded media.

A valid-cache check is `O(1)` in source size on POSIX filesystems. On Windows it is `O(C)` time with `O(1)` extra memory because VCC streams the source through SHA-256 to detect replacements that preserve size, timestamps, and file identity.

## Token consumption

Running `VCC.py` consumes **zero LLM/API tokens**: it is a local deterministic Python program. Tokens are consumed only when an agent reads the generated text or search stdout.

The console's `words` count is not an OpenAI, Anthropic, or GitHub model-token count. VCC's lightweight tokenizer groups letter/digit runs, counts punctuation separately, and ignores whitespace, so use it only as a relative size estimate.

Let `U` be retained user blocks, `A` retained assistant text blocks, `S_tool` the total lexical size of emitted tool-call summaries, `tu` the `-tu` limit, and `t` the `-t` limit:

- Full-view context is approximately proportional to all visible transcript text: `Θ(Cvisible)` lexical content.
- Brief-view content is roughly bounded by `O(U·tu + A·t + S_tool + headers)` VCC lexical units; thinking and tool-result bodies are normally omitted. Some summary fields, such as paths and patterns, are not length-capped.
- Focused/search output is proportional to matching lines plus block metadata, not the complete transcript.

For lowest agent token use: run `--search-only`, materialize only selected sessions, read `.min.txt`, and open only cited `.txt` ranges. Exact model tokens must be measured with the tokenizer of the model actually consuming the view.

## Current status and roadmap

VCC 2.3.1 is ready for personal workflows, local team use, and a public beta. It is not intended to be a centralized, multi-tenant conversation-history service. Within the currently validated scope, there are no known release-blocking P0/P1 issues.

Current release evidence includes:

- deterministic parsing and search for Codex, Claude Code, and GitHub Copilot CLI logs;
- 44 automated tests, four skill-package validators, and representative sanitized fixtures for all three clients;
- verification against a real Codex session containing multiple compaction boundaries;
- Linux, macOS, and Windows CI across the supported Python range, plus a reproducible benchmark tool;
- bounded media decoding, cache-integrity checks, conservative regex guards, and source-aware recall selection.

The source JSONL remains authoritative. Generated views and caches are derived artifacts: they may be deleted after use, or retained privately when repeated lookup justifies the storage and privacy cost. `--chain-window` reduces downstream IR, rendering, disk, and agent-context costs, but normalization still retains the current input's parsed records, so very large single-session logs can still require memory proportional to that file.

Prioritized follow-up work:

1. Implement stateful, single-pass normalizers for each client to reduce peak memory on very large logs without changing deterministic output.
2. Expand real-world schema fixtures, schema-drift checks, malformed-input tests, and fuzz coverage as client formats evolve.
3. Add OS-independent regex execution isolation or hard timeouts; the current guard is deliberately conservative and `--allow-unsafe-regex` remains an explicit escape hatch.
4. Track performance regressions over larger benchmark tiers, including peak RSS and long-running cache behavior.
5. Add an optional cross-platform high-integrity source-hash mode for deployments whose filesystem does not provide reliable replacement identity; Windows hashing is already automatic.
6. Consider an opt-in, privacy-preserving incremental content index only if measured workloads justify it. VCC will not duplicate raw conversation text into a permanent index by default.

Future client schemas are not assumed compatible until covered by fixtures and tests. VCC does not upload session data or require a cloud service.

## Privacy and limitations

Session logs and generated views may contain source code, commands, file paths, tool output, credentials, or other sensitive material.

- Keep cache directories private and out of source control and cloud sync.
- `--cache-dir` applies best-effort owner-only permissions on POSIX systems.
- Do not publish generated views without reviewing them.
- By default, an unterminated malformed final JSONL line is treated as a live-session tail and ignored with a warning; malformed middle records fail that input.
- Multi-file runs isolate failed inputs, continue processing healthy files, and exit nonzero when any input failed. Use `--strict` for fail-fast behavior and to reject an incomplete tail.
- Embedded media extensions are sanitized, base64 is validated, and each decoded item is limited to 64 MiB by default.
- Broad materialized searches can consume substantial disk and memory; prefer `--search-only`.
- A shared `-o` directory rejects equal input stems before writing.
- Generated views reflect the source at compile time and do not prove current workspace or runtime state.

## CLI reference

```text
VCC.py INPUT [INPUT ...]
  --grep REGEX       Search role-aware blocks
  --search-only      Require --grep; search incrementally without writing views
  --cache-dir DIR    Override the private managed cache root
  --cache-policy P   Reuse a valid cache or force refresh
  --strict           Reject an incomplete final record and stop at the first input error
  --literal TEXT     Search one literal string
  --term TEXT        Add a literal anchor; repeat and combine with --match
  --match all|any    Multi-term query semantics
  -i, --ignore-case  Case-insensitive search
  --format FORMAT    text, json, or ndjson search output
  --max-matches-per-input N  Keep the N highest-scoring blocks per input
  --diagnostics      Emit parser coverage, compaction boundaries, and unknown event types
  --max-media-bytes N  Cap each decoded embedded item; 0 means unlimited
  --chain-window N  Materialize only the newest N chains; 0 means all
  --allow-unsafe-regex  Bypass conservative regex safety checks
  -o, --output-dir   Export all outputs to a selected directory
  -t N               Brief-view assistant/tool truncation limit (default: 128)
  -tu N              Brief-view user truncation limit (default: 256)
```

`--grep` uses Python regular expressions. Escape regex metacharacters when searching for literal user text.

Run `VCC.py history-search --help` for source selection, exact-current-session, query-mode, expansion, scoring, and result-limit options.

Run `python benchmarks/benchmark_vcc.py` for a deterministic JSON benchmark of search-only, latest-two materialization, and cache-hit paths. Adjust `--records-per-chain`, `--chains`, `--payload-size`, and `--repeat` to compare versions on the same machine.

## Citation

```bibtex
@article{zhang2026vcc,
  title={View-oriented Conversation Compiler for Agent Trace Analysis},
  author={Lvmin Zhang and Maneesh Agrawala},
  year={2026}
}
```
