"""Tests for the cursor-sdk → Hermes messages projector.

The projector is the piece that keeps memory review, session persistence,
and message-alternation invariants working under the cursor runtime, so
these tests focus on the shape contract: every completed tool call yields
exactly one assistant tool_call + one tool result, stranded calls are
closed, and malformed/unknown payloads never raise.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent.transports.cursor_event_projector import (
    CursorEventProjector,
    map_cursor_tool_name,
)


def _assert_alternation_safe(messages):
    """Every assistant tool_call must be immediately followed by its tool
    result; no dangling tool messages."""
    idx = 0
    while idx < len(messages):
        msg = messages[idx]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            assert idx + 1 < len(messages), "dangling assistant tool_call"
            nxt = messages[idx + 1]
            assert nxt.get("role") == "tool"
            assert nxt.get("tool_call_id") == msg["tool_calls"][0]["id"]
            idx += 2
            continue
        assert msg.get("role") != "tool", "orphan tool message"
        idx += 1


class TestAssistantAndThinking:
    def test_assistant_text_blocks_accumulate(self):
        projector = CursorEventProjector()
        result = projector.project({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "world"},
            ]},
        })
        assert result.final_text == "Hello world"
        assert result.messages == [{"role": "assistant", "content": "Hello world"}]

    def test_thinking_stashed_as_reasoning_on_next_assistant(self):
        projector = CursorEventProjector()
        assert projector.project({"type": "thinking", "text": "hmm"}).messages == []
        result = projector.project({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "done"}]},
        })
        assert result.messages[0]["reasoning"] == "hmm"

    def test_dataclass_like_objects_supported(self):
        """The SDK yields frozen dataclasses — attribute access must work."""
        projector = CursorEventProjector()
        message = SimpleNamespace(
            type="assistant",
            message=SimpleNamespace(
                content=[SimpleNamespace(type="text", text="typed")]
            ),
        )
        result = projector.project(message)
        assert result.final_text == "typed"

    def test_string_content_tolerated(self):
        projector = CursorEventProjector()
        result = projector.project({"type": "assistant", "message": {"content": "plain"}})
        assert result.final_text == "plain"


class TestToolCalls:
    def test_running_then_completed_materializes_pair(self):
        projector = CursorEventProjector()
        running = projector.project({
            "type": "tool_call", "call_id": "c1", "name": "shell",
            "status": "running", "args": {"command": "ls -la"},
        })
        assert running.messages == []
        assert running.tool_started == ("exec_command", "ls -la", {"command": "ls -la"})

        done = projector.project({
            "type": "tool_call", "call_id": "c1", "name": "shell",
            "status": "completed", "args": {"command": "ls -la"},
            "result": "total 0",
        })
        assert done.is_tool_iteration
        assert len(done.messages) == 2
        assistant, tool = done.messages
        assert assistant["tool_calls"][0]["function"]["name"] == "exec_command"
        assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {
            "command": "ls -la"
        }
        assert tool["role"] == "tool"
        assert tool["content"] == "total 0"
        assert tool["tool_call_id"] == assistant["tool_calls"][0]["id"]
        _assert_alternation_safe(done.messages)

    def test_error_status_marks_result(self):
        projector = CursorEventProjector()
        done = projector.project({
            "type": "tool_call", "call_id": "c9", "name": "edit",
            "status": "error", "args": {"path": "a.py"}, "result": "boom",
        })
        assert done.messages[1]["content"].startswith("[error]")

    def test_completed_without_running_still_materializes(self):
        projector = CursorEventProjector()
        done = projector.project({
            "type": "tool_call", "call_id": "c2", "name": "read",
            "status": "completed", "args": {"path": "x"}, "result": "data",
        })
        assert len(done.messages) == 2

    def test_untyped_args_and_results_are_tolerated(self):
        """Cursor documents args/result as unstable — never raise."""
        projector = CursorEventProjector()
        done = projector.project({
            "type": "tool_call", "call_id": "c3", "name": "mystery",
            "status": "completed", "args": ["not", "a", "dict"],
            "result": {"nested": {"deep": True}},
        })
        assert len(done.messages) == 2
        parsed = json.loads(done.messages[0]["tool_calls"][0]["function"]["arguments"])
        assert parsed == {"arguments": ["not", "a", "dict"]}

    def test_finalize_closes_stranded_running_calls(self):
        projector = CursorEventProjector()
        projector.project({
            "type": "tool_call", "call_id": "c4", "name": "shell",
            "status": "running", "args": {"command": "sleep 100"},
        })
        finalize = projector.finalize()
        assert len(finalize.messages) == 2
        assert finalize.messages[1]["content"].startswith("[error]")
        _assert_alternation_safe(finalize.messages)
        # Second finalize is a no-op.
        assert projector.finalize().messages == []

    def test_reasoning_attaches_to_tool_call_assistant(self):
        projector = CursorEventProjector()
        projector.project({"type": "thinking", "text": "plan the command"})
        done = projector.project({
            "type": "tool_call", "call_id": "c5", "name": "shell",
            "status": "completed", "args": {}, "result": "",
        })
        assert done.messages[0]["reasoning"] == "plan the command"


class TestOtherEvents:
    def test_usage_event_captured(self):
        projector = CursorEventProjector()
        result = projector.project({
            "type": "usage",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        })
        assert result.usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        assert result.messages == []

    def test_status_task_system_user_ignored(self):
        projector = CursorEventProjector()
        for msg_type in ("status", "task", "system", "user", "request"):
            result = projector.project({"type": msg_type})
            assert result.messages == []

    def test_unknown_and_malformed_never_raise(self):
        projector = CursorEventProjector()
        for junk in (None, {}, {"type": "???"}, "text", 42, {"type": "tool_call"}):
            result = projector.project(junk)
            assert result.messages == [] or len(result.messages) == 2

    def test_final_text_accumulates_across_run(self):
        projector = CursorEventProjector()
        projector.project({"type": "assistant", "message": {"content": [{"type": "text", "text": "one"}]}})
        projector.project({"type": "assistant", "message": {"content": [{"type": "text", "text": "two"}]}})
        assert projector.final_text == "one\n\ntwo"


class TestToolNameMapping:
    def test_known_cursor_tools_map_to_hermes_names(self):
        assert map_cursor_tool_name("shell") == "exec_command"
        assert map_cursor_tool_name("Edit") == "apply_patch"
        assert map_cursor_tool_name("read") == "read_file"
        assert map_cursor_tool_name("grep") == "search_files"

    def test_unknown_names_pass_through(self):
        assert map_cursor_tool_name("future_tool_v9") == "future_tool_v9"
        assert map_cursor_tool_name("") == "unknown"
