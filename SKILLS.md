# VCC Skill Guide

VCC is distributed as four companion Agent Skills. This guide records the stable packaging, routing, portability, and maintenance rules behind those skills. For installation commands and client-specific locations, see [INSTALL.md](INSTALL.md).

## Skill set

Install and update these directories as one unit:

| Skill | Role |
|---|---|
| `conversation-compiler` | Canonical runtime and direct compilation/search interface |
| `readchat` | Evidence-based review of one known session |
| `searchchat` | Discovery across local session history |
| `recall` | Recovery of actionable prior context followed by current-state verification |

The three entry skills resolve `../conversation-compiler/scripts/VCC.py`. Installing an entry skill without `conversation-compiler`, renaming a directory, or updating only one member can break that contract.

## Description policy

A skill description is routing metadata, not a support matrix. Claude uses it to decide when to load the full `SKILL.md`.

VCC descriptions therefore:

- state the capability and the situations that should trigger it;
- retain high-value anchors such as `/readchat`, `events.jsonl`, rollout logs, continuation summaries, and compaction recovery;
- refer to supported agent sessions instead of enumerating every client;
- keep client support details in the READMEs and runtime tests;
- remain at or below 200 characters so the same skill can be uploaded through Claude.ai and Cowork, even though the general Agent Skills specification permits longer descriptions.

When adding a client, update parser support, fixtures, diagnostics, and the README support table. Do not expand all four descriptions with another product name.

## Package layout

Each skill is a directory whose name matches its frontmatter `name`:

```text
skill-name/
├── SKILL.md
├── scripts/       # optional executable support
├── references/    # optional detailed guidance
├── assets/        # optional templates or data
└── agents/        # optional platform metadata
```

VCC uses `scripts/` in `conversation-compiler` and `agents/` in all four packages. Preserve both when copying or packaging the skills.

For Claude.ai or Cowork upload, create one ZIP per skill with the named directory at the archive root:

```text
readchat.zip
└── readchat/
    ├── SKILL.md
    └── agents/
```

Do not place `SKILL.md` directly at the ZIP root. Upload and enable all four packages together.

## Discovery boundaries

The same skill source can be reused across clients, but discovery and execution are platform-specific:

- Claude Code CLI and local sessions in the Claude Desktop Code tab read project `.claude/skills/` and personal `$HOME/.claude/skills/`.
- SSH sessions read skill directories on the remote host. Cloud Code sessions do not read the local computer's personal skill directory; commit required project skills under `.claude/skills/`.
- Cowork does not scan Claude Code's local skill directories. It loads account-enabled skills and plugins from Customize at session start.
- Cowork must be granted access to the local history folders VCC will search. If a mounted path differs from the standard client path, pass `--root CLIENT=PATH` to `history-search`.

A skill installed for one surface is not automatically installed for another.

## Portability rules

Treat the Agent Skill directory as the reusable core and each client's installation or plugin format as an outer shell.

Within shared skill instructions:

- use relative paths inside the skill package;
- avoid hard-coded home directories and client-specific installation roots;
- describe capabilities rather than naming a host-specific tool when a portable equivalent exists;
- do not assume identical shells, Python versions, dependencies, network access, permissions, or tool schemas;
- keep platform-specific instructions in separate references when they cannot be expressed portably;
- never hard-code credentials in `SKILL.md`, scripts, references, or assets.

VCC requires Python 3.10 or newer. A skill being discoverable does not prove its scripts can run in that environment; runtime verification is mandatory.

## Loading and updates

- Claude Code detects edits within existing skill directories during a session. If a skill has already been invoked, its rendered instructions remain in that conversation, so use a new session to test updated instructions.
- If the top-level skill directory did not exist when a Code session began, restart Code so it can watch the new directory.
- Cowork account skills sync at session start; start a new Cowork session after enabling or updating the four packages.
- Replace all four VCC skill directories together when updating.

## Release checklist

1. Verify all four directories and their required supporting files are present.
2. Confirm every frontmatter `name` matches its directory.
3. Keep each `description` at or below 200 characters and test representative trigger prompts.
4. Run:

   ```bash
   python -m py_compile skills/conversation-compiler/scripts/*.py
   python -m unittest discover -s tests -v
   ```

5. Build upload ZIPs without caches, `.DS_Store`, generated views, or credentials.
6. Inspect each archive root and run `VCC.py --version` from an extracted package.
7. Verify at least one representative session from every advertised client and inspect `--diagnostics` for unknown event types.

## References

- [Agent Skills specification](https://agentskills.io/specification)
- [Creating custom skills](https://claude.com/docs/skills/how-to)
- [Cowork overview](https://claude.com/docs/cowork/overview)
- [Install Cowork plugins](https://claude.com/docs/cowork/guide/plugins)
- [Claude Code skills](https://code.claude.com/docs/en/skills)
- [Claude Code on desktop](https://code.claude.com/docs/en/desktop)
