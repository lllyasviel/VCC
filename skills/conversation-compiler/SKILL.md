---
name: conversation-compiler
description: "VCC (View-oriented Conversation Compiler) documentation. Compile Claude Code JSONL logs into adaptive views."
---

# VCC — View-oriented Conversation Compiler

Compile Claude Code JSONL logs into adaptive views for reading, searching, and context recovery.

## Usage

```bash
uv run "path/to/VCC.py" <input.jsonl ...> [options]
```

| Option | Description |
|--------|-------------|
| `-o <dir>` | Output directory (default: same as input) |
| `-t <N>` | Token truncation limit (default 128) |
| `-tu <N>` | User message token limit (default 256) |
| `--grep <pattern>` | Regex search pattern (Python `re` — use `a|b`, NOT `a\|b`) |

This tool also supports multi-file processing:

```bash
cd "/path/to/target/folder" && uv run "path/to/VCC.py" *.jsonl --grep "keyword"
```

## Output Files

| File | When | Description |
|------|------|-------------|
| `.txt` | Always | Full transcript, lossless |
| `.min.txt` | Always | Brief overview |
| `.view.txt` | `--grep` only | Search-focused view |
| stdout | `--grep` only | Search results with block-level line range references |

## Rules

- **Forward slashes only in bash commands.** Backslashes in double quotes are escape characters — `\U`, `\l`, `\.` get silently mangled. Use `"C:/Users/..."` not `"C:\Users\..."`. This applies to ALL bash commands (not just this skill), on ALL platforms (Windows and Linux alike).
- Do NOT use `-o` unless the user explicitly requests an output directory. By default, compiled files are written next to the input JSONL — this is the intended behavior.
- Do NOT clean up compiled output files after use. Leave them in place unless the user explicitly asks to clean up.

## Workflow

**1. Compile**

```bash
uv run "path/to/VCC.py" "path/to/conversation.jsonl"
```

Produces `.txt` + `.min.txt` next to the input file. Long conversations are automatically split into numbered chunks. The console output lists every produced file with line/word counts — read it to understand the conversation's size and structure before proceeding:

```
conversation_1.txt  (2808 lines, 34545 words)
conversation_2.txt  (9622 lines, 116015 words)
conversation_3.txt  (3242 lines, 37946 words)
conversation_1.min.txt  (101 lines, 1572 words)
conversation_2.min.txt  (811 lines, 9504 words)
conversation_3.min.txt  (205 lines, 3340 words)
```

Chunks are numbered chronologically — higher numbers are more recent. (During `/recall`, the last chunk is the current conversation; the second-to-last is likely where the previous session's context ends.) Use this to gauge how many chunks exist and how large each is, so you can target your `.min.txt` reads and `--grep` searches to the right chunks.

**2. Read `.min.txt`**

The `.min.txt` is a scannable outline: user/assistant text is kept (truncated), tool calls collapse to one-line summaries with line references back to `.txt`.

```
[user]

Take a look!

[assistant]

Let me take a look.

* Read "code.py" (a_1.txt:19-21,24-34)
```

Each `*` line shows the tool name, key parameter, and two `.txt` line ranges: the call (19-21) and the result (24-34). Read these ranges in `.txt` for full details.

**3. Search with `--grep`**

Always use `--grep`, never system grep nor your embedded Grep tool. This script's `--grep` returns important block-level line RANGES that no other grep tools can provide. The output paths are relative to CWD, so `cd` close to the target first to keep outputs short and save tokens.

```bash
cd "/path/to/target/folder" && uv run "path/to/VCC.py" "path/to/conversation.jsonl" --grep "keyword"
```

Stdout example (`#` prefix = shortened filename)：

```
(#752a87.txt:L67-L81) [assistant]      →  .txt lines 67-81
  77: one matched line ...             →  .txt line 77
```

Optionally read `.view.txt` for focused search view.

**4. Jump to `.txt`**

All line references point to `.txt`. Read the referenced range for full context. Remember to read a bit more lines before and after the referenced range to get more context.

# SKILLs

## /readchat — Read a specific conversation log

**Trigger**: The user wants to read a specific JSONL conversation log.

**Action**: Follow the Workflow above with the user-specified filenames or requests.

## /searchchat — Search across conversation logs

**Trigger**: The user wants to search across conversation logs in `~/.claude/projects/`.

**Action**:

1. `ls ~/.claude/projects/` — browse project directories, narrow the search area.
2. `cd ~/.claude/projects/<project> && uv run "absolute/path/to/VCC.py" *.jsonl --grep "keyword"` — search top-level conversations first.
3. If no results: `cd ~/.claude/projects/<project> && uv run "absolute/path/to/VCC.py" **/*.jsonl --grep "keyword"` — expand to subagents.

**Critical**: Always `cd` into the target directory first, then use VCC globs — a single `cd && uv run VCC.py <glob> --grep` is the correct pattern. Without `cd`, grep output contains full absolute paths instead of short relative paths, wasting tokens. All content search on JSONL must use VCC's `--grep`, never system grep or the embedded Grep tool — VCC's `--grep` returns block-level line ranges with role tags that no other grep can provide.

## /recall — Recover context from a previous conversation

**Trigger**: The conversation opens with a context-continuation summary ("This session is being continued from a previous conversation..."), or the user says `/recall`.

**Action**:

1. **Find JSONL filename** — The continuation message ends with `read the full transcript at: <path>.jsonl`. Find that JSONL filename. If no JSONL path is present (e.g. the user invoked `/recall` manually without a continuation header), use the `/searchchat` method to locate the JSONL before proceeding to steps 2–3.

2. **Follow VCC Workflow** — Follow the full Workflow above (compile → read `.min.txt` → `--grep` → jump to `.txt`) with the JSONL file found in step 1. The summary is lossy — only the original JSONL is authoritative.

3. **Verify against current state** — After recovering the conversation, cross-check with reality:
   - **REALLY read files** — Read all files referenced in the conversation with your Read tool. Pre-loaded content from system-reminders or prior conversation turns does NOT count — it may be truncated, stale, or lossy. You must issue a fresh Read call for each file, even if you believe you already have its content. 99.97% of the time, the user has externally modified nearly every file between sessions. If you skip reading and miss their external changes, your failure rate is effectively 100%.
   - **Key details** — Identify specific values, paths, configs, and logic mentioned in the conversation. Compare against the actual files to catch drift or errors.
   - **Understand the journey** — Trace the user's intent, decision sequence, and direction changes. Understand not just what was done, but how and why they got there.
