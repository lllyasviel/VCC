# Installation Guide for Agents

For the design, packaging, portability, and maintenance rules behind the four packages, see [SKILLS.md](SKILLS.md).

## Requirements

- Python 3.10+
- All four VCC skill directories installed together

## Choose an install location

Copy `conversation-compiler`, `readchat`, `recall`, and `searchchat` from this repository's `skills/` directory into one supported skill root:

| Client | Project scope | Personal scope |
|---|---|---|
| GitHub Copilot CLI | `.github/skills/`, `.agents/skills/`, or `.claude/skills/` | `${COPILOT_HOME:-$HOME/.copilot}/skills/` or `$HOME/.agents/skills/` |
| Codex | Use the project skill location supported by the active Codex surface | `${CODEX_HOME:-$HOME/.codex}/skills/` |
| Claude Code (CLI and local Claude Desktop Code tab) | `.claude/skills/` | `$HOME/.claude/skills/` |
| Claude Desktop Cowork | Upload each skill as a ZIP through Customize | Account-managed; does not read local `.claude/skills/` directories |

### Claude Desktop notes

- **Code tab:** local sessions use the same skill directories as the Claude Code CLI. SSH sessions read the remote host's directories instead; Cloud sessions do not read `$HOME/.claude/skills/` from the local computer, so commit project skills under `.claude/skills/` when remote execution needs them.
- **Cowork:** package and upload each of the four skill directories separately. Each ZIP must contain its named directory at the archive root, for example `readchat/SKILL.md`; preserve all supporting files. Enable all four uploads, grant the Cowork project access to the required history folders, then start a new session. Cowork may mount a folder at a different path, so run history discovery with `--root CLIENT=PATH` when the standard history path is unavailable. Account-managed Cowork skills are separate from Claude Code filesystem skills.

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

Then confirm skill discovery:

- GitHub Copilot CLI: run `/skills reload`, then `/skills info readchat`.
- Codex: start a new task and confirm the four skills are discoverable.
- Claude Code and the local Desktop Code tab: changes inside an existing skill directory are detected live. Start a new session if a skill was already invoked, or restart Code if the top-level skill directory was created after the session began.
- Claude Desktop Cowork: account skills sync at session start, so begin a new Cowork session after enabling or updating all four uploads.

Finally invoke `readchat` against a known session JSONL. Verify that `.txt`, `.min.txt`, and `metadata.json` are generated under VCC's private managed cache and that the source history directory is unchanged. Use `--cache-dir` only to override the cache location.

Repository maintainers should also run:

```bash
python -m py_compile skills/conversation-compiler/scripts/*.py
python -m unittest discover -s tests -v
```

The repository CI runs the boundary Python versions 3.10 and 3.13 on Linux, macOS, and Windows; local development also exercises intermediate Python versions when available. A release is not verified merely because one client discovers the skills; validate at least one representative JSONL from every advertised client and inspect `--diagnostics` for unknown event types.

## Uninstall

Delete the four VCC directories from the selected skill root. Managed cache entries are separate; remove them only when the user asks, because they may be referenced by ongoing work. Explicit `-o` exports are user-owned artifacts and are never part of automatic uninstall cleanup.
