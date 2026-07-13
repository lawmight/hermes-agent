"""Tests for the Hermes-tools → cursor custom_tools bridge."""

from __future__ import annotations

import json
from unittest.mock import patch

from agent.transports.cursor_hermes_tools import (
    _MAX_RESULT_CHARS,
    build_cursor_custom_tools,
)
from agent.transports.hermes_tools_mcp_server import EXPOSED_TOOLS


def _fake_defs(names):
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} does things",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        }
        for name in names
    ]


class TestBuildCursorCustomTools:
    def test_exposes_only_registered_exposed_tools(self):
        registered = ["web_search", "vision_analyze", "terminal", "delegate_task"]
        with patch("model_tools.get_tool_definitions", return_value=_fake_defs(registered)), \
             patch("model_tools.handle_function_call", return_value="{}"):
            tools = build_cursor_custom_tools("task-1")
        # Intersection of EXPOSED_TOOLS with what's registered:
        assert set(tools) == {"web_search", "vision_analyze"}
        # NEVER terminal/file tools (cursor's own) nor agent-loop tools.
        assert "terminal" not in tools
        assert "delegate_task" not in tools
        spec = tools["web_search"]
        assert spec["description"] == "web_search does things"
        assert spec["input_schema"]["properties"]["query"]["type"] == "string"
        assert callable(spec["execute"])

    def test_policy_is_shared_with_codex_mcp_sidecar(self):
        """One curated list, one policy — the bridge must not fork it."""
        with patch("model_tools.get_tool_definitions",
                   return_value=_fake_defs(list(EXPOSED_TOOLS))), \
             patch("model_tools.handle_function_call", return_value="{}"):
            tools = build_cursor_custom_tools(None)
        assert set(tools) == set(EXPOSED_TOOLS)

    def test_session_toolset_filters_are_forwarded(self):
        with patch(
            "model_tools.get_tool_definitions",
            return_value=_fake_defs(["web_search"]),
        ) as get_definitions, patch(
            "model_tools.handle_function_call",
            return_value="{}",
        ):
            build_cursor_custom_tools(
                None,
                enabled_toolsets=["search"],
                disabled_toolsets=["browser"],
            )

        get_definitions.assert_called_once_with(
            enabled_toolsets=["search"],
            disabled_toolsets=["browser"],
            quiet_mode=True,
        )

    def test_executor_dispatches_through_handle_function_call(self):
        calls = []

        def fake_handle(name, args, task_id=None, session_id=None, **kw):
            calls.append((name, args, task_id, session_id))
            return json.dumps({"ok": True})

        with patch("model_tools.get_tool_definitions",
                   return_value=_fake_defs(["web_search"])), \
             patch("model_tools.handle_function_call", side_effect=fake_handle):
            tools = build_cursor_custom_tools("task-9", session_id="sess-9")
        out = tools["web_search"]["execute"]({"query": "hermes agent"}, None)
        assert json.loads(out) == {"ok": True}
        assert calls == [("web_search", {"query": "hermes agent"}, "task-9", "sess-9")]

    def test_executor_tolerates_string_args_and_exceptions(self):
        def fake_handle(name, args, **kw):
            if args.get("value") == "boom":
                raise RuntimeError("kaput")
            return "fine"

        with patch("model_tools.get_tool_definitions",
                   return_value=_fake_defs(["web_search"])), \
             patch("model_tools.handle_function_call", side_effect=fake_handle):
            tools = build_cursor_custom_tools(None)
        execute = tools["web_search"]["execute"]
        assert execute('{"query": "q"}', None) == "fine"
        error_out = json.loads(execute('"boom"', None))
        assert error_out["error"] == "kaput"
        assert error_out["tool"] == "web_search"

    def test_oversized_results_truncated(self):
        big = "x" * (_MAX_RESULT_CHARS + 500)
        with patch("model_tools.get_tool_definitions",
                   return_value=_fake_defs(["web_search"])), \
             patch("model_tools.handle_function_call", return_value=big):
            tools = build_cursor_custom_tools(None)
        out = tools["web_search"]["execute"]({}, None)
        assert len(out) < len(big)
        assert "[truncated" in out

    def test_tool_event_hook_fires(self):
        seen = []
        with patch("model_tools.get_tool_definitions",
                   return_value=_fake_defs(["web_search"])), \
             patch("model_tools.handle_function_call", return_value="ok"):
            tools = build_cursor_custom_tools(
                None, on_tool_event=lambda n, p, a: seen.append((n, p)))
        tools["web_search"]["execute"]({"query": "look this up"}, None)
        assert seen == [("web_search", "look this up")]
