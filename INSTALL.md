# Installation Guide for Agents (Manual Install)

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

## Install

Clone this repo to any temporary location. Copy the four folders inside `skills/` (`conversation-compiler`, `readchat`, `recall`, `searchchat`) into your project's `.claude/skills/`. Delete the cloned repo.

## Update

Clone this repo to any temporary location. Delete the four folders (`conversation-compiler`, `readchat`, `recall`, `searchchat`) from your `.claude/skills/`, then copy the new versions from the cloned `skills/` into `.claude/skills/`. Delete the cloned repo.

## Verify

After install or update, ask the user to restart Claude Code, then run `/recall` to test that everything works.

## Uninstall

Delete `conversation-compiler`, `readchat`, `recall`, `searchchat` from your `.claude/skills/`. Ask the user to restart Claude Code.
