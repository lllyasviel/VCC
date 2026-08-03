# Installation Guide for Agents

## Requirements

- Python 3.10+
- All four VCC skill directories installed together

## Choose an install location

Copy `conversation-compiler`, `readchat`, `recall`, and `searchchat` from this repository's `skills/` directory into one supported skill root:

| Client | Project scope | Personal scope |
|---|---|---|
| GitHub Copilot CLI | `.github/skills/`, `.agents/skills/`, or `.claude/skills/` | `${COPILOT_HOME:-$HOME/.copilot}/skills/` or `$HOME/.agents/skills/` |
| Codex | Use the project skill location supported by the active Codex surface | `${CODEX_HOME:-$HOME/.codex}/skills/` |
| Claude Code | `.claude/skills/` | Use the personal skill location supported by the active Claude Code version |

Do not install only an entry skill: `readchat`, `recall`, and `searchchat` share the canonical runtime at `conversation-compiler/scripts/VCC.py`.

## Install

1. Clone this repository into a temporary location.
2. Copy the four directories from `skills/` into the selected skill root without renaming them.
3. Preserve each directory's `SKILL.md`, `agents/`, and `scripts/` contents.
4. Remove the temporary clone only after verification.

## Update

Replace the four installed VCC directories as one unit. Do not update only `VCC.py` or one entry skill because their command contract and workflow instructions evolve together.

## Verify

Run a structural/runtime check:

```bash
python "<skill-root>/conversation-compiler/scripts/VCC.py" --version
python "<skill-root>/conversation-compiler/scripts/VCC.py" --help
python "<skill-root>/conversation-compiler/scripts/VCC.py" history-search --help
```

Then reload skills or start a new agent session:

- GitHub Copilot CLI: run `/skills reload`, then `/skills info readchat`.
- Codex: start a new task after installation and confirm the four skills are discoverable.
- Claude Code: start a new session or use the skill reload mechanism provided by the installed version.

Finally invoke `readchat` against a known session JSONL. Verify that `.txt`, `.min.txt`, and `metadata.json` are generated under VCC's private managed cache and that the source history directory is unchanged. Use `--cache-dir` only to override the cache location.

Repository maintainers should also run:

```bash
python -m py_compile skills/conversation-compiler/scripts/*.py
python -m unittest discover -s tests -v
```

The repository CI runs the boundary Python versions 3.10 and 3.13 on Linux, macOS, and Windows; local development also exercises intermediate Python versions when available. A release is not verified merely because one client discovers the skills; validate at least one representative JSONL from every advertised client and inspect `--diagnostics` for unknown event types.

## Uninstall

Delete the four VCC directories from the selected skill root. Managed cache entries are separate; remove them only when the user asks, because they may be referenced by ongoing work. Explicit `-o` exports are user-owned artifacts and are never part of automatic uninstall cleanup.
