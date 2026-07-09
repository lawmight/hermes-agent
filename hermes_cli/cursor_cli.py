"""`hermes cursor` — Cursor cloud-agent + catalog management via cursor-sdk.

Footprint-ladder rung 2 (CLI command + skill): the agent drives Cursor's
cloud abilities — launch a cloud agent on a repo, follow its run, pull
artifacts, open PRs — through `hermes cursor <verb>` from the `terminal`
tool, with zero model-tool schema footprint.

Read-only verbs (`models`, `me`) use the Cloud Agents REST API over plain
HTTPS so they never trigger the ~48 MB lazy cursor-sdk install. Verbs that
manage agents (`launch`, `list`, `status`, `follow`, `send`, `cancel`,
`artifacts`, `archive`, `unarchive`, `delete`, `repos`) go through the
official SDK (lazy feature ``provider.cursor``).

Auth: CURSOR_API_KEY from ~/.hermes/.env or the environment
(Cursor Dashboard → Integrations → API Keys).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any, Optional

CURSOR_API_BASE_URL = "https://api.cursor.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fail(message: str) -> int:
    print(f"✗ {message}", file=sys.stderr)
    return 1


def _resolve_api_key() -> str:
    """CURSOR_API_KEY from ~/.hermes/.env (preferred) or the process env."""
    try:
        from hermes_cli.config import get_env_value_prefer_dotenv

        key = (get_env_value_prefer_dotenv("CURSOR_API_KEY") or "").strip()
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("CURSOR_API_KEY", "").strip()


def _require_api_key() -> Optional[str]:
    key = _resolve_api_key()
    if not key:
        _fail(
            "No CURSOR_API_KEY found. Create a key at "
            "https://cursor.com/dashboard?tab=integrations and add it to "
            "your .env (hermes setup), then retry."
        )
        return None
    return key


def _rest_get(path: str, api_key: str, timeout: float = 15.0) -> Any:
    req = urllib.request.Request(CURSOR_API_BASE_URL + path)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _get_sdk():
    """Lazy-import cursor_sdk (patchable seam for tests)."""
    from tools.lazy_deps import FeatureUnavailable, ensure

    try:
        ensure("provider.cursor")
    except FeatureUnavailable as exc:
        raise RuntimeError(
            f"cursor-sdk is not installed and could not be installed: {exc}\n"
            "Install manually with:  pip install cursor-sdk"
        ) from exc
    import cursor_sdk  # noqa: PLC0415 — lazy on purpose (48 MB wheel)

    return cursor_sdk


def _launch_client(sdk):
    client_cls = getattr(sdk, "CursorClient", None) or getattr(sdk, "Client")
    return client_cls.launch_bridge(workspace=os.getcwd())


def _attr(obj: Any, name: str, default: Any = "") -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _print_agent_row(info: Any) -> None:
    agent_id = _attr(info, "agent_id")
    status = _attr(info, "status") or "-"
    name = _attr(info, "name") or ""
    summary = (_attr(info, "summary") or "").strip().replace("\n", " ")
    if len(summary) > 60:
        summary = summary[:57] + "..."
    archived = " [archived]" if _attr(info, "archived", False) else ""
    print(f"  {agent_id}  {status:<9} {name}{archived}")
    if summary:
        print(f"      {summary}")


def _stream_run(run: Any) -> str:
    """Print a run's stream messages as they arrive; return terminal status."""
    try:
        for message in run.messages():
            msg_type = str(_attr(message, "type"))
            if msg_type == "assistant":
                inner = _attr(message, "message") or {}
                for block in _attr(inner, "content") or []:
                    if _attr(block, "type") == "text":
                        text = _attr(block, "text")
                        if text:
                            print(text, end="", flush=True)
            elif msg_type == "tool_call":
                status = _attr(message, "status")
                if status == "running":
                    print(f"\n[tool] {_attr(message, 'name')} ...", flush=True)
            elif msg_type == "status":
                status_text = _attr(message, "status")
                if status_text:
                    print(f"\n[status] {status_text}", flush=True)
    except KeyboardInterrupt:
        print("\n(stream detached — the cloud run keeps going; "
              "reattach with `hermes cursor follow <id>`)")
        return "detached"
    print()
    try:
        result = run.wait()
        return str(_attr(result, "status") or _attr(run, "status") or "finished")
    except Exception:
        return str(_attr(run, "status") or "unknown")


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------

def cmd_models(args) -> int:
    api_key = _require_api_key()
    if not api_key:
        return 1
    try:
        data = _rest_get("/v1/models", api_key)
    except Exception as exc:
        return _fail(f"model catalog fetch failed: {exc}")
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list) or not models:
        return _fail("no models returned (check your API key)")
    print(f"Cursor models ({len(models)} recommended — other ids may also work):")
    for item in models:
        if isinstance(item, str):
            print(f"  {item}")
            continue
        if not isinstance(item, dict):
            continue
        model_id = item.get("id", "?")
        display = item.get("displayName") or ""
        aliases = ", ".join(item.get("aliases") or [])
        line = f"  {model_id}"
        if display and display != model_id:
            line += f"  ({display})"
        if aliases:
            line += f"  aliases: {aliases}"
        print(line)
        for param in item.get("parameters") or []:
            values = "|".join(
                str(v.get("value")) for v in param.get("values") or []
                if isinstance(v, dict)
            )
            print(f"      param {param.get('id')}: {values}")
    print("\nUse with:  /model cursor:<id>   or   cursor.model_params in config.yaml")
    return 0


def cmd_me(args) -> int:
    api_key = _require_api_key()
    if not api_key:
        return 1
    try:
        data = _rest_get("/v1/me", api_key)
    except Exception as exc:
        return _fail(f"auth check failed: {exc}")
    print("✓ CURSOR_API_KEY is valid")
    for key in ("email", "name", "teamName", "apiKeyName"):
        value = data.get(key) if isinstance(data, dict) else None
        if value:
            print(f"  {key}: {value}")
    return 0


def cmd_repos(args) -> int:
    api_key = _require_api_key()
    if not api_key:
        return 1
    try:
        sdk = _get_sdk()
        repos = sdk.Cursor.repositories.list(api_key=api_key)
    except Exception as exc:
        return _fail(f"repository list failed: {exc}")
    items = _attr(repos, "items", None)
    if items is None:
        items = list(repos or [])
    if not items:
        print("No connected repositories. Connect one at https://cursor.com/agents")
        return 0
    print(f"Connected repositories ({len(items)}):")
    for repo in items:
        url = _attr(repo, "url") or repo
        print(f"  {url}")
    return 0


def cmd_launch(args) -> int:
    api_key = _require_api_key()
    if not api_key:
        return 1
    prompt = (args.prompt or "").strip()
    if not prompt:
        return _fail("a prompt is required: hermes cursor launch \"<task>\" --repo <url>")

    cloud: dict[str, Any] = {}
    if args.repo:
        repo: dict[str, Any] = {"url": args.repo}
        if args.ref:
            repo["starting_ref"] = args.ref
        cloud["repos"] = [repo]
    if args.pr:
        cloud["auto_create_pr"] = True
    if args.branch_current:
        cloud["work_on_current_branch"] = True
    if args.pool:
        cloud["env"] = {"type": "pool", "name": args.pool}
    env_vars = {}
    for pair in args.env_var or []:
        if "=" not in pair:
            return _fail(f"--env-var takes KEY=VALUE, got: {pair}")
        key, _, value = pair.partition("=")
        env_vars[key] = value
    if env_vars:
        cloud["env_vars"] = env_vars

    try:
        sdk = _get_sdk()
        client = _launch_client(sdk)
        create_kwargs: dict[str, Any] = {"api_key": api_key, "cloud": cloud}
        if args.model:
            create_kwargs["model"] = args.model
        if args.name:
            create_kwargs["name"] = args.name
        agent = client.agents.create(**create_kwargs)
        run = agent.send(prompt)
    except Exception as exc:
        return _fail(f"cloud agent launch failed: {exc}")

    agent_id = _attr(agent, "agent_id")
    print(f"✓ cloud agent launched: {agent_id}")
    print(f"  follow:    hermes cursor follow {agent_id}")
    print(f"  status:    hermes cursor status {agent_id}")
    print(f"  artifacts: hermes cursor artifacts {agent_id}")
    if args.follow:
        status = _stream_run(run)
        print(f"run status: {status}")
    return 0


def cmd_list(args) -> int:
    api_key = _require_api_key()
    if not api_key:
        return 1
    try:
        sdk = _get_sdk()
        client = _launch_client(sdk)
        kwargs: dict[str, Any] = {"runtime": "cloud"}
        if args.archived:
            kwargs["include_archived"] = True
        page = client.agents.list(**kwargs)
    except Exception as exc:
        return _fail(f"agent list failed: {exc}")
    items = _attr(page, "items", None) or []
    if not items:
        print("No cloud agents. Launch one with: hermes cursor launch \"<task>\" --repo <url>")
        return 0
    print(f"Cloud agents ({len(items)}):")
    for info in items:
        _print_agent_row(info)
    return 0


def cmd_status(args) -> int:
    api_key = _require_api_key()
    if not api_key:
        return 1
    try:
        sdk = _get_sdk()
        client = _launch_client(sdk)
        info = client.agents.get(args.agent_id)
        runs = client.agents.list_runs(args.agent_id)
    except Exception as exc:
        return _fail(f"status lookup failed: {exc}")
    _print_agent_row(info)
    items = _attr(runs, "items", None) or []
    for run in items[:5]:
        run_id = _attr(run, "id")
        status = _attr(run, "status") or "-"
        created = _attr(run, "created_at") or ""
        print(f"    run {run_id}  {status}  {created}")
    return 0


def cmd_follow(args) -> int:
    api_key = _require_api_key()
    if not api_key:
        return 1
    try:
        sdk = _get_sdk()
        client = _launch_client(sdk)
        runs = client.agents.list_runs(args.agent_id)
        items = _attr(runs, "items", None) or []
        if not items:
            return _fail(f"no runs found for {args.agent_id}")
        run = client.agents.get_run(_attr(items[0], "id"))
    except Exception as exc:
        return _fail(f"follow failed: {exc}")
    print(f"following {args.agent_id} (Ctrl+C detaches without cancelling)")
    observe = getattr(run, "observe", None)
    try:
        if callable(observe):
            for event in observe():
                kind = _attr(event, "kind") or _attr(event, "type") or ""
                if kind:
                    print(f"[{kind}]", flush=True)
            status = str(_attr(run, "status") or "finished")
        else:
            status = _stream_run(run)
    except KeyboardInterrupt:
        print("\n(detached — the cloud run keeps going)")
        return 0
    except Exception as exc:
        return _fail(f"stream failed: {exc}")
    print(f"run status: {status}")
    return 0


def cmd_send(args) -> int:
    api_key = _require_api_key()
    if not api_key:
        return 1
    try:
        sdk = _get_sdk()
        client = _launch_client(sdk)
        agent = client.agents.resume(args.agent_id, {"api_key": api_key})
        run = agent.send(args.prompt)
    except Exception as exc:
        return _fail(f"send failed: {exc}")
    if args.follow:
        status = _stream_run(run)
        print(f"run status: {status}")
    else:
        print(f"✓ follow-up sent to {args.agent_id} (run {_attr(run, 'id')})")
        print(f"  follow: hermes cursor follow {args.agent_id}")
    return 0


def cmd_cancel(args) -> int:
    api_key = _require_api_key()
    if not api_key:
        return 1
    try:
        sdk = _get_sdk()
        client = _launch_client(sdk)
        runs = client.agents.list_runs(args.agent_id)
        items = _attr(runs, "items", None) or []
        active = next(
            (r for r in items if str(_attr(r, "status")).lower() == "running"), None
        )
        if active is None:
            print("no running run to cancel")
            return 0
        run = client.agents.get_run(_attr(active, "id"))
        run.cancel()
    except Exception as exc:
        return _fail(f"cancel failed: {exc}")
    print(f"✓ cancelled run {_attr(active, 'id')} on {args.agent_id}")
    return 0


def cmd_artifacts(args) -> int:
    api_key = _require_api_key()
    if not api_key:
        return 1
    try:
        sdk = _get_sdk()
        client = _launch_client(sdk)
        agent = client.agents.resume(args.agent_id, {"api_key": api_key})
        artifacts = agent.list_artifacts()
    except Exception as exc:
        return _fail(f"artifact list failed: {exc}")
    if not artifacts:
        print("no artifacts (local agents and repos-only runs produce none)")
        return 0
    print(f"Artifacts on {args.agent_id}:")
    for artifact in artifacts:
        path = _attr(artifact, "path")
        size = _attr(artifact, "size_bytes", 0)
        print(f"  {path}  ({size} bytes)")
    if args.download:
        dest_root = Path(args.download).expanduser()
        dest_root.mkdir(parents=True, exist_ok=True)
        for artifact in artifacts:
            path = str(_attr(artifact, "path"))
            try:
                content = agent.download_artifact(path)
            except Exception as exc:
                print(f"  ✗ {path}: {exc}")
                continue
            target = dest_root / path.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            print(f"  ✓ downloaded {path} → {target}")
    return 0


def _lifecycle(args, verb: str) -> int:
    api_key = _require_api_key()
    if not api_key:
        return 1
    try:
        sdk = _get_sdk()
        client = _launch_client(sdk)
        getattr(client.agents, verb)(args.agent_id)
    except Exception as exc:
        return _fail(f"{verb} failed: {exc}")
    print(f"✓ {verb}d {args.agent_id}" if not verb.endswith("e") else f"✓ {verb}d {args.agent_id}")
    return 0


def cmd_archive(args) -> int:
    return _lifecycle(args, "archive")


def cmd_unarchive(args) -> int:
    return _lifecycle(args, "unarchive")


def cmd_delete(args) -> int:
    if not args.yes:
        return _fail(
            "delete is permanent — the transcript becomes unreadable. "
            "Re-run with --yes to confirm (archive is the reversible option)."
        )
    return _lifecycle(args, "delete")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_DISPATCH = {
    "models": cmd_models,
    "me": cmd_me,
    "repos": cmd_repos,
    "launch": cmd_launch,
    "list": cmd_list,
    "ls": cmd_list,
    "status": cmd_status,
    "follow": cmd_follow,
    "send": cmd_send,
    "cancel": cmd_cancel,
    "artifacts": cmd_artifacts,
    "archive": cmd_archive,
    "unarchive": cmd_unarchive,
    "delete": cmd_delete,
}


def cursor_command(args) -> int:
    """Dispatch `hermes cursor <verb>`. Returns a process exit code."""
    action = getattr(args, "cursor_action", None)
    if not action:
        print(
            "usage: hermes cursor "
            "{models|me|repos|launch|list|status|follow|send|cancel|"
            "artifacts|archive|unarchive|delete}"
        )
        return 2
    handler = _DISPATCH.get(action)
    if handler is None:
        return _fail(f"unknown cursor action: {action}")
    try:
        return int(handler(args) or 0)
    except RuntimeError as exc:
        return _fail(str(exc))
    except KeyboardInterrupt:
        print()
        return 130
