# Cursor provider → latest main port plan

Assessment date: **2026-07-30**.  
Legacy provider tip: `origin/cursor/bc-3845be2f-3cfa-4910-87cf-4fbcbc00fb27-6a3f` @ `f81b880d3`.  
Compared to: `origin/main` @ `bd41bce46` (~3,881 commits ahead of the legacy tip) and `upstream/main` @ `2d4049424`.  
Open draft (do not merge as-is): [lawmight/hermes-agent#1](https://github.com/lawmight/hermes-agent/pull/1).

## Verdict

**Portable with a major port** — not a rebase/merge of the old branch topology.

Concepts and several modules are reusable. Core wiring must be rebuilt against current extension points. Upstream has previously rejected in-tree vendor integrations; prefer a plugin + minimal core seam, deployed as a separate `HERMES_HOME` build.

## Current local posture (this machine)

| Install | Path | Cursor as model provider? |
|---|---|---|
| Stock Hermes | `~/.hermes` + `hermes-gateway.service` | No (Nous/OpenRouter; Cursor via skills/CLI/REST) |
| Isolated Cursor build | `~/.hermes-cursor` + `hermes-cursor` launcher | Yes (pinned legacy tip; gateway unit disabled) |

## Conflict / break map (merge into `origin/main`)

| File | Breakage |
|---|---|
| `agent/agent_init.py` | API-mode validation, client init, compression state |
| `agent/agent_runtime_helpers.py` | Transactional model switching + external-session cleanup |
| `agent/auxiliary_client.py` | Provider aliases / Cursor exclusion amid rewritten routing |
| `gateway/run.py` | Message path; `message_type` already exists on main |
| `hermes_cli/main.py` | Parser modularized; plugin CLI commands exist |
| `run_agent.py` | Reset/close lifecycle + full-turn runtime forwarder |

A 21-commit rebase onto main stops on the same clusters. Mechanical “ours/theirs” resolution is unsafe (drops large current-main regions).

## Integration inventory

### Still-valid port (after expectation updates)

- `plugins/model-providers/cursor/{__init__.py,plugin.yaml}`
- `agent/transports/cursor_event_projector.py`
- `agent/transports/cursor_hermes_tools.py`
- `hermes_cli/cursor_cli.py` business logic
- Cursor optional skill, Cursor-specific tests/docs (update assertions)

### Needs API adaptation

- `agent/cursor_runtime.py`
- `agent/transports/cursor_bridge.py`
- `agent/transports/cursor_sdk_session.py`
- Core touchpoints: `agent_init.py`, `conversation_loop.py`, `run_agent.py`, `agent_runtime_helpers.py`, `auxiliary_client.py`, compression/review/turn-context/metadata/usage
- `hermes_cli/config.py`, `runtime_provider.py`
- `tools/lazy_deps.py`, `tools/environments/local.py`
- Gateway/TUI lifecycle tests

### Likely dead / replaced by current main

- Direct Cursor registration in `hermes_cli/main.py`
- Cursor-specific picker/setup in `hermes_cli/models.py`, `model_setup_flows.py`, `providers.py`
- `providers/__init__.py` bundled-plugin workaround
- Old gateway `message_type` patch

Main already has profile-based API-key providers, live model fetching, generic setup flows, and plugin CLI registration.

## API drift map

| Area | Drift |
|---|---|
| Provider loading | `ProviderProfile.api_mode` still usable; refresh schema on register |
| CLI | Use `PluginContext.register_cli_command()` — do not edit `_BUILTIN_SUBCOMMANDS` |
| Model picker/setup | Generic profile consumers call `fetch_models()`; drop custom picker patches |
| Agent runtime | Codex full-turn precedent exists; **no** generic plugin hook for external agent harnesses yet → small core dispatch/lifecycle patch still required |
| Transport registry | `register_transport()` exists but does not cover “SDK owns the whole turn” |
| Gateway | Drop obsolete `message_type` patch |
| Auxiliary client | Keep “Cursor cannot serve auxiliary calls” guard; do not port whole old module |
| Lifecycle | No generic `_close_external_runtime_sessions()` — add narrow Cursor hook or propose vendor-neutral interface |

Static AST checks on the eight new production modules showed no missing current-main internal imports. Blockers are behavioral/API integration.

## `cursor-sdk` drift

| Item | Value |
|---|---|
| Legacy pin | `cursor-sdk==0.1.9` |
| PyPI at assessment | `1.0.26` |
| Sync surface | Bridge client, `agents.create` / `agents.resume`, send/stream — broadly compatible |
| 1.x additions | Usage APIs/types (`get_usage`, `UsageCost`, `RunUsage`, `AgentUsage`) |
| Highest risk | `cursor_bridge.py` patches private `_bridge._bridge_subprocess_env` — retest or replace before upgrading |

Keep an exact pin until local/cloud, cancel, resume, and credential-sanitization tests pass on 1.x.

## Phased fix plan

1. **New branch from current `main`** — do not preserve the old 22-commit topology or reopen #1 as a rebase.
2. **Extract plugin-owned pieces** — provider profile, model catalog, CLI command, skill, docs.
3. **Port runtime** around current Codex full-turn patterns — dispatch, session ownership, reset/close, model switch, interrupt.
4. **Delete obsolete patches** — picker/setup, central parser edits, gateway `message_type`.
5. **Upgrade and pin SDK** to `1.0.26` after bridge env / credential stripping validation.
6. **Test matrix**
   - Local/cloud create, resume, interrupted turns
   - Tool projection + event ordering
   - Model switch, reset, close, wedged-session retirement
   - Catalog success/fallback + CLI commands
   - No SDK import/install when Cursor unused
   - Secret-free bridge environment
   - Stock chat-completions / Anthropic / Codex regressions
7. **Deploy** as separate install + `HERMES_HOME` (continue `hermes-cursor` pattern). Upstream only a vendor-neutral external-runtime extension seam if desired.

Estimated clusters: one high-risk runtime cluster; medium provider/SDK + lifecycle; low-risk CLI/docs/tests.

## Recommended end state

- Normal Cursor provider/CLI **plugin**
- Minimal separate **core patch** for full-turn runtime dispatch (or a future generic hook)
- Separate Cursor Hermes installation (`hermes-cursor` / `~/.hermes-cursor`)
- Do **not** merge the vendor implementation into stock daily Hermes

## User vs agent

**User**

- Choose: keep pinned legacy build vs fund the main-based port.
- Approve live `CURSOR_API_KEY` testing / billable cloud calls.
- Confirm whether 1.x-only SDK support is acceptable.

**Agent (after approval)**

- Implement clean main-based port in an isolated worktree.
- Build plugin + minimal runtime patch; run test matrix.
- Refresh the separate Hermes Cursor installation without touching stock `~/.hermes`.

## Related Odysseus work

Odysseus Cursor integration is git-current on `upstream/dev` and only needs a controlled `cursor-sdk` 1.x cutover. See `docs/CURSOR_INTEGRATION_CURRENCY.md` on `lawmight/odysseus`.
