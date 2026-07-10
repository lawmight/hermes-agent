"""Integration test for the cursor_agent runtime path through AIAgent.

Verifies that:
  - provider='cursor' / api_mode='cursor_agent' is accepted on construction
  - run_conversation() takes the early-return path and never enters the
    chat completions loop
  - Projected messages from a fake cursor session land in the messages list
  - tool_iterations tick the skill nudge counter; usage lands in session
    accounting with the "included" subscription cost route
  - Wedged sessions (should_retire) are dropped so the next turn respawns
  - The returned dict has the same shape as the chat_completions path
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import run_agent
from agent.transports.cursor_sdk_session import CursorSDKSession, CursorTurnResult


@pytest.fixture
def fake_session(monkeypatch):
    """Replace CursorSDKSession with a stub returning a fixed CursorTurnResult
    so we can drive AIAgent without the cursor-sdk or any network."""

    def fake_run_turn(self, user_input, **kwargs):
        return CursorTurnResult(
            final_text=f"echo: {user_input}",
            projected_messages=[
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "cursor_c1", "type": "function",
                                 "function": {"name": "exec_command",
                                              "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "cursor_c1", "content": "ok"},
                {"role": "assistant", "content": f"echo: {user_input}"},
            ],
            tool_iterations=1,
            interrupted=False,
            error=None,
            run_id="run-stub-1",
            agent_id="agent-stub-1",
            status="finished",
        )

    monkeypatch.setattr(CursorSDKSession, "run_turn", fake_run_turn)
    monkeypatch.setattr(
        CursorSDKSession, "ensure_started", lambda self: "agent-stub-1"
    )


def _make_cursor_agent(**kwargs):
    """Construct an AIAgent on the cursor runtime without any real provider."""
    return run_agent.AIAgent(
        api_key="crsr_stub",
        base_url="https://api.cursor.com",
        provider="cursor",
        model="composer-2.5",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        **kwargs,
    )


class TestConstruction:
    def test_provider_cursor_derives_cursor_agent_mode(self):
        agent = _make_cursor_agent()
        assert agent.api_mode == "cursor_agent"
        assert agent.client is None
        assert agent.api_key == "crsr_stub"

    def test_env_key_fallback(self, monkeypatch):
        monkeypatch.setenv("CURSOR_API_KEY", "crsr_env")
        agent = run_agent.AIAgent(
            provider="cursor", model="composer-2.5", quiet_mode=True,
            skip_context_files=True, skip_memory=True,
        )
        assert agent.api_key == "crsr_env"
        assert agent.base_url == "https://api.cursor.com"

    def test_missing_key_raises_clear_error(self, monkeypatch):
        monkeypatch.delenv("CURSOR_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="CURSOR_API_KEY"):
            run_agent.AIAgent(
                provider="cursor", model="composer-2.5", quiet_mode=True,
                skip_context_files=True, skip_memory=True,
            )


class TestRunConversationCursorPath:
    def test_run_conversation_returns_cursor_shape(self, fake_session):
        agent = _make_cursor_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hello there")
        assert result["final_response"] == "echo: hello there"
        assert result["completed"] is True
        assert result["partial"] is False
        assert result["error"] is None
        assert result["api_calls"] == 1
        assert result["cursor_agent_id"] == "agent-stub-1"
        assert result["cursor_run_id"] == "run-stub-1"
        assert result["agent_persisted"] is True

    def test_projected_messages_land_in_history(self, fake_session):
        agent = _make_cursor_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("do the thing")
        roles = [m.get("role") for m in result["messages"]]
        # system prompt, user, then projected assistant/tool/assistant
        assert roles[-3:] == ["assistant", "tool", "assistant"]
        # Alternation: the tool message immediately follows its call.
        tool_msg = result["messages"][-2]
        assert tool_msg["tool_call_id"] == "cursor_c1"

    def test_skill_nudge_counter_ticks(self, fake_session):
        agent = _make_cursor_agent()
        agent._iters_since_skill = 0
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("hello")
        assert agent._iters_since_skill == 1

    def test_usage_updates_session_accounting(self, monkeypatch):
        def fake_run_turn(self, user_input, **kwargs):
            return CursorTurnResult(
                final_text="done",
                projected_messages=[{"role": "assistant", "content": "done"}],
                run_id="run-u1",
                agent_id="agent-u1",
                status="finished",
                token_usage_last={
                    "input_tokens": 80,
                    "output_tokens": 25,
                    "cache_read_tokens": 20,
                    "cache_write_tokens": 5,
                    "total_tokens": 130,
                    "reasoning_tokens": 5,
                },
            )

        monkeypatch.setattr(CursorSDKSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(CursorSDKSession, "ensure_started", lambda self: "agent-u1")
        agent = _make_cursor_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hello")

        assert result["api_calls"] == 1
        # prompt bucket = input + cache_read + cache_write
        assert result["prompt_tokens"] == 105
        assert result["completion_tokens"] == 25
        assert result["total_tokens"] == 130
        assert result["cache_read_tokens"] == 20
        assert result["cache_write_tokens"] == 5
        assert result["reasoning_tokens"] == 5
        # Subscription-billed: cost is "included", not estimated.
        assert result["cost_status"] == "included"
        assert result["estimated_cost_usd"] == 0.0

        assert agent.session_api_calls == 1
        assert agent.session_prompt_tokens == 105
        assert agent.session_completion_tokens == 25
        assert agent.session_total_tokens == 130

    def test_context_meter_uses_last_step_while_accounting_uses_run_total(
        self, monkeypatch
    ):
        def fake_run_turn(self, user_input, **kwargs):
            return CursorTurnResult(
                final_text="done",
                projected_messages=[{"role": "assistant", "content": "done"}],
                token_usage_last={
                    "input_tokens": 10_000,
                    "output_tokens": 1_000,
                    "cache_read_tokens": 90_000,
                    "cache_write_tokens": 0,
                    "total_tokens": 101_000,
                    "reasoning_tokens": 0,
                },
                token_usage_total={
                    "input_tokens": 30_000,
                    "output_tokens": 3_000,
                    "cache_read_tokens": 270_000,
                    "cache_write_tokens": 0,
                    "total_tokens": 303_000,
                    "reasoning_tokens": 0,
                },
            )

        monkeypatch.setattr(CursorSDKSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(CursorSDKSession, "ensure_started", lambda self: "a")
        agent = _make_cursor_agent()

        result = agent.run_conversation("tool-heavy task")

        assert result["prompt_tokens"] == 300_000
        assert result["last_prompt_tokens"] == 100_000
        assert agent.session_prompt_tokens == 300_000
        assert agent.context_compressor.last_prompt_tokens == 100_000

    def test_total_only_usage_does_not_overwrite_context_meter(self, monkeypatch):
        def fake_run_turn(self, user_input, **kwargs):
            return CursorTurnResult(
                final_text="done",
                projected_messages=[{"role": "assistant", "content": "done"}],
                token_usage_total={
                    "input_tokens": 300_000,
                    "output_tokens": 3_000,
                    "cache_read_tokens": 2_700_000,
                    "cache_write_tokens": 0,
                    "total_tokens": 3_003_000,
                    "reasoning_tokens": 0,
                },
            )

        monkeypatch.setattr(CursorSDKSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(CursorSDKSession, "ensure_started", lambda self: "a")
        agent = _make_cursor_agent()
        agent.context_compressor.last_prompt_tokens = 77

        result = agent.run_conversation("tool-heavy task")

        assert result["prompt_tokens"] == 3_000_000
        assert "last_prompt_tokens" not in result
        assert agent.context_compressor.last_prompt_tokens == 77

    def test_error_turn_reports_partial(self, monkeypatch):
        def fake_run_turn(self, user_input, **kwargs):
            return CursorTurnResult(
                final_text="", error="cursor run ended in error: boom",
                status="error",
            )

        monkeypatch.setattr(CursorSDKSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(CursorSDKSession, "ensure_started", lambda self: "a")
        agent = _make_cursor_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hello")
        assert result["completed"] is False
        assert result["partial"] is True
        assert "boom" in result["error"]

    def test_interrupted_turn_acknowledges_and_clears_interrupt(self, monkeypatch):
        def fake_run_turn(self, user_input, **kwargs):
            return CursorTurnResult(
                final_text="",
                interrupted=True,
                status="cancelled",
            )

        monkeypatch.setattr(CursorSDKSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(CursorSDKSession, "ensure_started", lambda self: "a")
        agent = _make_cursor_agent()
        agent._interrupt_requested = True
        agent._interrupt_message = "reply exactly NEXT"

        result = agent.run_conversation("long task")

        assert result["interrupted"] is True
        assert result["partial"] is True
        assert result["interrupt_message"] == "reply exactly NEXT"
        assert agent._interrupt_requested is False
        assert agent._interrupt_message is None

    def test_should_retire_drops_session(self, monkeypatch):
        def fake_run_turn(self, user_input, **kwargs):
            return CursorTurnResult(
                final_text="partial", error="idle timeout", should_retire=True,
            )

        monkeypatch.setattr(CursorSDKSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(CursorSDKSession, "ensure_started", lambda self: "a")
        agent = _make_cursor_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("hello")
        assert agent._cursor_session is None

    def test_session_reused_across_turns(self, fake_session):
        agent = _make_cursor_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("turn one")
            first_session = agent._cursor_session
            agent.run_conversation("turn two")
        assert agent._cursor_session is first_session


class TestCompressionInert:
    def test_hermes_compression_never_rewrites_cursor_transcript(self, fake_session):
        from agent.conversation_compression import compress_context

        agent = _make_cursor_agent()
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
        ]
        compressed, system_prompt = compress_context(
            agent, list(messages), "sys", approx_tokens=10_000_000, force=True
        )
        assert compressed == messages
        assert system_prompt == "sys"


class TestBackgroundReviewGuard:
    def test_review_runtime_skips_on_cursor_parent(self, fake_session):
        from agent.background_review import _resolve_review_runtime

        agent = _make_cursor_agent()
        runtime = _resolve_review_runtime(agent)
        assert runtime.get("skip") is True
        assert "cursor" in runtime.get("skip_reason", "")


class TestAuxiliaryGuard:
    def test_aux_client_refuses_cursor_provider(self):
        from agent.auxiliary_client import resolve_provider_client

        client, model = resolve_provider_client("cursor", "composer-2.5")
        assert client is None and model is None

    def test_aux_alias_normalization(self):
        from agent.auxiliary_client import _normalize_aux_provider

        assert _normalize_aux_provider("cursor-sdk") == "cursor"
        assert _normalize_aux_provider("composer") == "cursor"
