---
name: cursor-cloud
description: Delegate repo work to Cursor cloud agents via hermes cursor.
version: 0.1.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [Coding-Agent, Cursor, Cloud-Agents, Delegation]
    related_skills: [openhands, hermes-agent-dev]
---

# Cursor Cloud Skill

Delegate repository-scale coding tasks to Cursor cloud agents through the
`hermes cursor` CLI, driven from the `terminal` tool. A cloud agent runs on a
Cursor-hosted VM with the repo cloned in, works autonomously, and can open a
PR when it finishes. This skill does NOT cover using Cursor models as the
primary chat model — that is `hermes model` → Cursor (the `cursor` provider).

## When to Use

- The user asks to run a coding task on a repo this machine does not have
  checked out, or wants many tasks in parallel on isolated VMs.
- Work must survive this process disconnecting (cloud runs persist
  server-side; reattach any time with `hermes cursor follow`).
- The user wants a PR opened automatically from the run (`--pr`).
- For local, Hermes-native subagents prefer `delegate_task`; for work inside
  the current working tree on the Cursor provider, the primary chat loop
  already runs Cursor when `model.provider: cursor`.

## Prerequisites

- `CURSOR_API_KEY` in the Hermes .env (Cursor Dashboard → Integrations →
  API Keys). Verify with `terminal`: `hermes cursor me`.
- The target repository connected to Cursor cloud agents
  (check with `hermes cursor repos`; connect at cursor.com/agents).

## How to Run

Always invoke through the `terminal` tool.

```
hermes cursor launch "Add structured logging to the auth middleware" \
  --repo https://github.com/org/repo --ref main --pr
```

Prints the `bc-...` agent id. Long runs: do NOT block on `--follow`; poll
with `hermes cursor status <id>` or run
`terminal(command="hermes cursor follow <id>", background=true, notify_on_complete=true)`.

## Quick Reference

| Action | Command |
| --- | --- |
| Check key/account | `hermes cursor me` |
| List models + params | `hermes cursor models` |
| List connected repos | `hermes cursor repos` |
| Launch on a repo | `hermes cursor launch "<task>" --repo <url> [--ref <ref>] [--pr]` |
| Launch on current branch | add `--branch-current` |
| Self-hosted pool | add `--pool <name>` |
| Secret for one session | add `--env-var KEY=VALUE` (repeatable) |
| List cloud agents | `hermes cursor list [--archived]` |
| Status + recent runs | `hermes cursor status <bc-id>` |
| Stream live output | `hermes cursor follow <bc-id>` |
| Follow-up prompt | `hermes cursor send <bc-id> "<prompt>" [--follow]` |
| Cancel active run | `hermes cursor cancel <bc-id>` |
| List/download artifacts | `hermes cursor artifacts <bc-id> [--download DIR]` |
| Archive / restore | `hermes cursor archive|unarchive <bc-id>` |
| Delete (permanent) | `hermes cursor delete <bc-id> --yes` |

## Procedure

1. Verify auth: `hermes cursor me`. If it fails, stop and tell the user to
   add `CURSOR_API_KEY` (do not guess a key).
2. Confirm the repo is connected: `hermes cursor repos`.
3. Launch with a specific, self-contained prompt — the cloud agent has no
   access to this conversation. Include acceptance criteria and file hints.
4. Poll `hermes cursor status <id>` (or background-follow) until the run
   reaches `finished` / `error`.
5. Collect results: the PR link from the final output when `--pr` was used,
   or `hermes cursor artifacts <id> --download <dir>` for produced files.
6. Report the outcome and the agent id so the user can reattach later.

## Pitfalls

- One active run per cloud agent: a second `send` while one is running
  fails with `agent_busy` — wait, `cancel`, or launch a separate agent.
- The launch prompt is the agent's ONLY context. Never write "as discussed
  above"; restate everything it needs.
- `delete` is permanent and makes the transcript unreadable — prefer
  `archive` unless the user explicitly asks for deletion.
- `--env-var` values land inside the VM; only pass secrets the user
  explicitly approved for that run.
- Detaching from `follow` (Ctrl+C / interrupt) does NOT cancel the run —
  use `cancel` for that.

## Verification

- `hermes cursor status <id>` shows the run in a terminal state.
- With `--pr`: the final output contains the PR URL; confirm it exists.
- Artifacts downloaded intact: file sizes match the `artifacts` listing.
