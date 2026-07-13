---
title: Cursor Provider (cursor-sdk runtime)
sidebar_label: Cursor Provider
---

# Cursor Provider (cursor-sdk runtime)

Hermes can use **Cursor** as a primary model provider. Pick `Cursor` in `hermes model` (or run `/model cursor:composer-2.5`) and every conversation turn is driven through the official [`cursor-sdk`](https://cursor.com/docs/sdk/python) — the same agent that powers the Cursor IDE, CLI, and web app, billed against your Cursor subscription with a single API key.

Cursor sells Composer as an **agent harness, not a raw model** — there is no chat-completions endpoint to point a normal transport at. So this provider works like the [Codex App-Server Runtime](/user-guide/features/codex-app-server-runtime): the cursor agent owns the turn's tool loop (its own shell/read/edit tools, its own context window), and Hermes owns everything around it — sessions, memory and skill review, slash commands, the messaging gateway, cron — **and injects its own tool surface into cursor turns** via the SDK's custom tools.

Unlike the Codex runtime there is no toggle to flip: selecting the `cursor` provider *is* the opt-in. There is no other way to speak to Cursor.

## Why

- **One subscription, one key.** Composer plus the frontier models Cursor routes (Claude, GPT, Gemini, …) behind a single `CURSOR_API_KEY` — spend shows up in your Cursor dashboard under the SDK tag as "included" usage.
- **Cursor's agent harness does the coding.** Its shell/read/edit tools, planning, subagents, and self-managed 200K context window run the turn.
- **Hermes' tools come along.** `web_search`, `web_extract`, browser automation, `vision_analyze`, `image_generate`, skills, TTS, and kanban handoff are exposed *inside* the cursor turn as SDK custom tools — dispatched in-process through Hermes' normal tool pipeline (hooks + guardrails included).
- **Memory and skills keep working.** Cursor's stream is projected into Hermes' message shape, so session persistence, `session_search`, and the self-improvement loop see a normal transcript.
- **Cloud agents included.** `hermes cursor launch` runs repo-scale tasks on Cursor-hosted VMs with optional auto-PR — see [the CLI section](#cloud-agents-hermes-cursor) below.

## Prerequisites

1. A Cursor subscription and an API key from **Cursor Dashboard → Integrations → API Keys** (user keys and service-account keys both work; Team Admin keys do not).
2. Add the key: `hermes setup` (it prompts for `CURSOR_API_KEY`), or put `CURSOR_API_KEY=...` in your `.env` directly.
3. The `cursor-sdk` Python package installs lazily (~48 MB, bundles the SDK bridge) the first time a cursor turn runs. To pre-install: `pip install cursor-sdk`. Wheels ship for macOS, Linux, and Windows x64.

Verify with:

```bash
hermes cursor me        # validates the key
hermes cursor models    # lists models + per-model params
```

## Enabling

```bash
hermes model            # pick Cursor → pick a model
```

or set it directly in `config.yaml`:

```yaml
model:
  provider: cursor
  default: composer-2.5
```

Mid-session: `/model cursor:composer-2.5`. Model switches are per-run — the SDK makes the new selection sticky, matching Hermes' own `/model` semantics.

## What tools the model actually has

Two independent sources:

### 1. Cursor's built-in toolset (always on)

Cursor's own shell, file read/edit, and search tools run the turn — the same harness the Cursor IDE uses. Anything you'd do via terminal, cursor does natively, inside its own permission model (see [Security posture](#security-posture)).

### 2. Hermes tools via SDK custom tools (local runs, on by default)

The same curated set the Codex runtime exposes via MCP — `web_search`, `web_extract`, `browser_*`, `vision_analyze`, `image_generate`, `skill_view`, `skills_list`, `text_to_speech`, and the `kanban_*` handoff tools — is passed to the SDK as custom tools. Calls dispatch in-process through `model_tools.handle_function_call()`, i.e. the exact code path Hermes' default runtime uses, including pre/post tool hooks and guardrails. Disable with `cursor.expose_hermes_tools: false`.

**Not available** (same as the Codex runtime): the agent-loop tools — `delegate_task`, `memory`, `session_search`, `todo` — need mid-loop AIAgent state that a stateless callback can't reach. Memory still works at the *review* level (see below); cursor has its own plan tracker.

## Configuration

Everything lives under `cursor:` in `config.yaml` (defaults shown):

```yaml
cursor:
  runtime: local            # local | cloud — where the primary-model agent runs
  mode: agent               # agent | plan (plan = read-only exploration posture)
  expose_hermes_tools: true # Hermes tools inside cursor turns (local only)
  inherit_mcp: false        # pass Hermes' mcp_servers to cursor inline
  setting_sources: []       # project|user|team|mdm|plugins|all (SDK local runs)
  sandbox: {}               # pass-through SandboxOptions for local runs
  model_params: {}          # e.g. {composer-2.5: {fast: "true"}}
  agents: {}                # inline subagent definitions (description + prompt)
  timeout_seconds: 1800     # idle timeout; resets on every stream event
  cloud:
    repos: []               # [{url, ref}] cloned into the VM (runtime: cloud)
    auto_create_pr: false
    work_on_current_branch: false
    env: {}                 # {type: pool, name: ...} for self-hosted pools

compression:
  cursor_auto: native       # cursor manages its own window; Hermes never rewrites
```

- **`mode: plan`** keeps runs in explore/design posture — the practical read-only switch.
- **`setting_sources`** lets local runs pick up `.cursor/` project config (rules, file-based MCP, `.cursor/agents/*.md` subagents, hooks). Default is inline-only, matching the SDK.
- **`inherit_mcp: true`** translates your `mcp_servers:` config into inline SDK MCP definitions per run. Servers that need interactive OAuth are skipped (the SDK can't open a browser).
- **`model_params`** maps model ids to that model's parameters (discover ids with
  `hermes cursor models`). Legacy flat maps remain supported as global defaults.
- **`agents`** defines named subagents cursor can spawn via its `Agent` tool:

  ```yaml
  cursor:
    agents:
      code-reviewer:
        description: Expert reviewer for quality and security.
        prompt: Review code for bugs, security issues, and proven approaches.
  ```

## Sessions, resume, and interrupts

- One cursor-sdk agent per Hermes session, reused across turns — cursor keeps the conversation context on its side (bridge state for local, server-side for cloud).
- The cursor agent id is persisted per Hermes session (`cursor/sessions.json` under your Hermes home), so `hermes -r` / gateway session resume reattaches to the same cursor conversation across process restarts.
- `/stop` (gateway) and Ctrl+C (CLI) cancel the in-flight run via the SDK; partial output is kept and the transcript stays alternation-safe.
- Images work: photos sent via the gateway or attached in the TUI are forwarded natively as SDK image payloads.

## Self-improvement loop (memory + skill review)

Cursor's stream events — assistant text, thinking, tool calls — are projected into standard Hermes messages, so the memory nudge cadence and skill-usage counters tick exactly as on the default runtime. One caveat: the **background review fork** cannot inherit the cursor runtime (there is no chat-completions surface to replay the transcript against). Point it at any other provider:

```yaml
auxiliary:
  background_review:
    provider: openrouter
    model: google/gemini-3-flash-preview
```

Without that override, background review is skipped on cursor sessions (a log line tells you). Everything else in the loop — memory sync, skill nudges, `MEMORY.md` writes via the review — behaves normally once routed.

## Auxiliary tasks

Side-LLM work (title generation, compression summaries, vision fallback, embeddings) cannot run on cursor — the SDK is an agent runtime, not a completions API. The auxiliary chain automatically falls through to your configured fallbacks/aggregators (OpenRouter, Nous, any API-key provider). Pin them explicitly under `auxiliary:` if you want control.

## Context handling

Cursor manages its own context window (200K for Composer) and self-compacts when needed. Hermes' compression is **inert** on this runtime — it never rewrites the projected transcript (`compression.cursor_auto: native`). Billing counters use the SDK's run total; the context bar uses only the final internal model step so tool-loop aggregates are never displayed as window fill.

## Security posture

- Cursor-internal tool calls (shell, edits) are governed by **cursor's own permission model** — `cursor.mode: plan`, `cursor.sandbox` options, and file-based [hooks](https://cursor.com/docs/agent/hooks) (`.cursor/hooks.json`). The SDK exposes no programmatic approval callback, so **Hermes' approval prompts do not gate cursor-internal commands**. This mirrors the Codex runtime's sandbox note.
- Hermes-bridged custom tools DO run through Hermes' normal pipeline (plugin hooks, guardrails, redaction).
- The bridge subprocess receives Hermes' sanitized subprocess environment:
  gateway, provider, tool, browser-session, GitHub, and infrastructure secrets
  are stripped before Cursor's model-driven shell starts.
- `CURSOR_API_KEY` is a secret: `.env` only, never `config.yaml`.

## Cloud agents (`hermes cursor`)

The full Cloud Agents surface is available as a CLI (footprint-ladder rung 2 — the agent drives it via `terminal`, guided by the optional `cursor-cloud` skill):

```bash
hermes cursor models                 # catalog + per-model params/variants
hermes cursor me                     # key/account check
hermes cursor repos                  # repos connected for cloud agents
hermes cursor launch "task" --repo https://github.com/o/r --pr
hermes cursor list [--archived]
hermes cursor status <bc-id>
hermes cursor follow <bc-id>         # follow a run; detached handles wait for its result
hermes cursor send <bc-id> "more"    # follow-up prompt
hermes cursor cancel <bc-id>
hermes cursor artifacts <bc-id> --download ./out
hermes cursor archive|unarchive|delete <bc-id>
```

Install the skill so the agent knows the workflow: `hermes skills install official/autonomous-ai-agents/cursor-cloud`.

You can also run your **primary conversation** on a cloud VM: set `cursor.runtime: cloud` and configure `cursor.cloud.repos` — useful for driving repo work from the messaging gateway on a machine that doesn't have the checkout.

## Limitations

- **Agent runtime, not a raw model.** Cursor's loop runs the turn; Hermes' own tool loop is bypassed (`delegate_task`/`memory`/`session_search`/`todo` unavailable mid-turn).
- **No programmatic approvals** for cursor-internal commands (SDK limitation) — use `mode: plan`, sandbox options, and `.cursor/hooks.json`.
- **Auxiliary tasks and background review need another provider** (see above).
- **Custom tools are local-runtime only** (SDK limitation); cloud runs get capability via `inherit_mcp` instead.
- **Artifacts are cloud-only** (`hermes cursor artifacts`); local runs produce none.
- `cursor-sdk` is in **public beta** — the version is pinned and payload parsing is defensive, but Cursor may change surfaces between releases.

## Architecture

```
AIAgent.run_conversation()                     (provider: cursor → api_mode cursor_agent)
  └─ agent/cursor_runtime.py                   turn orchestration, usage accounting
       └─ agent/transports/cursor_sdk_session.py
            ├─ cursor-sdk bridge (subprocess)  Agent.create/resume, send, cancel
            ├─ agent/transports/cursor_event_projector.py
            │      stream → Hermes messages + tool progress
            └─ agent/transports/cursor_hermes_tools.py
                   Hermes tools → SDK custom_tools (in-process dispatch)
```
