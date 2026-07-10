"""Tests for CursorSDKSession — drive turns through a fake cursor_sdk.

No network, no cursor-sdk install: a fake module object is injected via the
``sdk_module=`` parameter, exercising create/resume, send options, busy and
rate-limit recovery, interrupt→cancel, idle timeout, and the
session-id→agent-id persistence sidecar.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import agent.transports.cursor_sdk_session as session_mod
from agent.transports.cursor_sdk_session import (
    CursorSDKSession,
    build_model_selection,
    clear_persisted_agent_record,
    load_persisted_agent_record,
    persist_agent_record,
    translate_hermes_mcp_servers,
)


# ---------------------------------------------------------------------------
# Fake cursor_sdk surface
# ---------------------------------------------------------------------------

class FakeCursorAgentError(Exception):
    def __init__(self, message="", *, retry_after=None, is_retryable=False,
                 request_id=None):
        super().__init__(message)
        self.retry_after = retry_after
        self.is_retryable = is_retryable
        self.request_id = request_id


class FakeAgentBusyError(FakeCursorAgentError):
    pass


class FakeRateLimitError(FakeCursorAgentError):
    pass


class FakeAuthenticationError(FakeCursorAgentError):
    pass


class FakeSendOptions:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeRun:
    def __init__(self, messages=(), *, status="finished", result="final text",
                 model="composer-2.5", usage=None, block_until_cancel=False):
        self.id = "run-0001"
        self._messages = list(messages)
        self._final_status = status
        self._result = result
        self._model = SimpleNamespace(id=model) if model else None
        self._usage = usage
        self._block_until_cancel = block_until_cancel
        self._cancel_event = threading.Event()
        self.status = "running"
        self.cancel_calls = 0

    def messages(self):
        for message in self._messages:
            yield message
        if self._block_until_cancel:
            # Simulate a run that keeps working until cancelled.
            self._cancel_event.wait(timeout=30)
            self.status = "cancelled"
            return
        self.status = self._final_status

    def cancel(self):
        self.cancel_calls += 1
        self._final_status = "cancelled"
        self.status = "cancelled"
        self._cancel_event.set()

    def wait(self):
        return SimpleNamespace(
            status=self.status if self.status != "running" else self._final_status,
            result=self._result if self.status != "cancelled" else "",
            model=self._model,
            usage=self._usage,
            duration_ms=10,
        )


class FakeAgent:
    def __init__(self, agent_id="agent-0001"):
        self.agent_id = agent_id
        self.send_calls: list[tuple[Any, Optional[dict]]] = []
        self.send_errors: list[Exception] = []  # popped once per send
        self.runs: list[FakeRun] = []
        self.closed = False

    def queue_run(self, run: FakeRun):
        self.runs.append(run)

    def send(self, message, options=None):
        self.send_calls.append((message, options))
        if self.send_errors:
            raise self.send_errors.pop(0)
        if self.runs:
            return self.runs.pop(0)
        return FakeRun([
            {"type": "assistant",
             "message": {"content": [{"type": "text", "text": "ok"}]}},
        ])

    def close(self):
        self.closed = True


class FakeAgentsAPI:
    def __init__(self, client):
        self._client = client
        self.create_kwargs: list[dict] = []
        self.resume_calls: list[tuple[str, Any]] = []
        self.resume_error: Optional[Exception] = None
        self.next_agent = FakeAgent()
        self.listed_runs: list[Any] = []

    def create(self, **kwargs):
        self.create_kwargs.append(kwargs)
        return self.next_agent

    def resume(self, agent_id, options=None):
        self.resume_calls.append((agent_id, options))
        if self.resume_error is not None:
            raise self.resume_error
        agent = FakeAgent(agent_id=agent_id)
        return agent

    def list_runs(self, agent_id):
        return SimpleNamespace(items=self.listed_runs)


class FakeClient:
    launch_calls: list[dict] = []
    reject_env_kwarg = False

    def __init__(self):
        self.agents = FakeAgentsAPI(self)
        self.closed = False

    @classmethod
    def launch_bridge(cls, **kwargs):
        if cls.reject_env_kwarg and "env" in kwargs:
            raise TypeError("launch_bridge() got an unexpected keyword argument 'env'")
        cls.launch_calls.append(kwargs)
        return cls()

    def close(self):
        self.closed = True


def make_fake_sdk():
    FakeClient.launch_calls = []
    FakeClient.reject_env_kwarg = False
    return SimpleNamespace(
        CursorClient=FakeClient,
        CursorAgentError=FakeCursorAgentError,
        AgentBusyError=FakeAgentBusyError,
        RateLimitError=FakeRateLimitError,
        AuthenticationError=FakeAuthenticationError,
        SendOptions=FakeSendOptions,
    )


def make_session(sdk=None, *, config=None, session_id=None, model="composer-2.5",
                 custom_tools_builder=None, **kwargs):
    sdk = sdk or make_fake_sdk()
    session = CursorSDKSession(
        cwd="/tmp/proj",
        api_key="crsr_test",
        model=model,
        cursor_config=config or {},
        session_id=session_id,
        sdk_module=sdk,
        custom_tools_builder=custom_tools_builder or (lambda *a, **k: {}),
        **kwargs,
    )
    return session, sdk


# ---------------------------------------------------------------------------
# Option builders
# ---------------------------------------------------------------------------

class TestBuildModelSelection:
    def test_bare_id_without_params(self):
        assert build_model_selection("composer-2.5") == "composer-2.5"
        assert build_model_selection("composer-2.5", {}) == "composer-2.5"

    def test_params_map_to_selection(self):
        selection = build_model_selection("composer-2.5", {"fast": True})
        assert selection == {
            "id": "composer-2.5",
            "params": [{"id": "fast", "value": "True"}],
        }

    def test_model_scoped_params_do_not_leak_after_switch(self):
        params = {
            "composer-2.5": {"fast": "true"},
            "claude-fable-5": {"thinking": "high"},
        }

        assert build_model_selection("composer-2.5", params) == {
            "id": "composer-2.5",
            "params": [{"id": "fast", "value": "true"}],
        }
        assert build_model_selection("grok-4.5", params) == "grok-4.5"

    def test_empty_model_passes_through(self):
        assert build_model_selection("", {"fast": "true"}) == ""


class TestTranslateMcpServers:
    def test_stdio_and_http_shapes(self):
        translated = translate_hermes_mcp_servers({
            "fs": {"command": "npx", "args": ["-y", "server"], "env": {"A": "1"}},
            "docs": {"url": "https://example.com/mcp", "headers": {"X": "y"}},
        })
        assert translated["fs"] == {
            "type": "stdio", "command": "npx", "args": ["-y", "server"],
            "env": {"A": "1"},
        }
        assert translated["docs"] == {
            "type": "http", "url": "https://example.com/mcp", "headers": {"X": "y"},
        }

    def test_disabled_oauth_and_malformed_skipped(self):
        translated = translate_hermes_mcp_servers({
            "off": {"command": "x", "enabled": False},
            "dis": {"command": "x", "disabled": True},
            "oauth": {"url": "https://a", "oauth": {"client_id": "c"}},
            "empty": {},
            "junk": "not-a-dict",
        })
        assert translated == {}

    def test_non_dict_input(self):
        assert translate_hermes_mcp_servers(None) == {}
        assert translate_hermes_mcp_servers([1, 2]) == {}


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestEnsureStarted:
    def test_creates_local_agent_with_options(self):
        session, sdk = make_session(
            config={
                "mode": "plan",
                "model_params": {"fast": "true"},
                "setting_sources": ["project"],
                "sandbox": {"network": "off"},
                "agents": {
                    "reviewer": {"description": "reviews", "prompt": "review it"},
                    "broken": {"description": "no prompt"},
                },
            },
            session_title="My session",
            custom_tools_builder=lambda *a, **k: {"web_search": {"execute": lambda **kw: ""}},
        )
        agent_id = session.ensure_started()
        assert agent_id == "agent-0001"
        client = session._client
        kwargs = client.agents.create_kwargs[0]
        assert kwargs["model"] == {
            "id": "composer-2.5", "params": [{"id": "fast", "value": "true"}],
        }
        assert kwargs["api_key"] == "crsr_test"
        assert kwargs["name"] == "My session"
        # mode/agents are AgentOptions fields — must ride in ``options``,
        # not as top-level create() kwargs (SDK 0.1.9 rejects those).
        assert kwargs["options"]["mode"] == "plan"
        assert kwargs["options"]["agents"] == {
            "reviewer": {"description": "reviews", "prompt": "review it"},
        }
        local = kwargs["local"]
        assert local["cwd"] == "/tmp/proj"
        assert local["setting_sources"] == ["project"]
        assert local["sandbox_options"] == {"network": "off"}
        assert "web_search" in local["custom_tools"]
        # idempotent
        assert session.ensure_started() == "agent-0001"
        assert len(client.agents.create_kwargs) == 1

    def test_expose_hermes_tools_disabled(self):
        session, _ = make_session(
            config={"expose_hermes_tools": False},
            custom_tools_builder=lambda *a, **k: {"web_search": {}},
        )
        session.ensure_started()
        local = session._client.agents.create_kwargs[0]["local"]
        assert "custom_tools" not in local

    def test_cloud_runtime_options(self):
        session, _ = make_session(config={
            "runtime": "cloud",
            "cloud": {
                "repos": [{"url": "https://github.com/o/r", "ref": "main"},
                          "https://github.com/o/r2"],
                "auto_create_pr": True,
                "work_on_current_branch": True,
                "env": {"type": "pool", "name": "my-pool"},
            },
        })
        session.ensure_started()
        kwargs = session._client.agents.create_kwargs[0]
        assert "local" not in kwargs
        cloud = kwargs["cloud"]
        assert cloud["repos"] == [
            {"url": "https://github.com/o/r", "starting_ref": "main"},
            {"url": "https://github.com/o/r2"},
        ]
        assert cloud["auto_create_pr"] is True
        assert cloud["work_on_current_branch"] is True
        assert cloud["env"] == {"type": "pool", "name": "my-pool"}

    def test_inherit_mcp_passthrough(self):
        session, _ = make_session(
            config={"inherit_mcp": True},
        )
        session._hermes_mcp_servers = {"fs": {"command": "npx"}}
        session.ensure_started()
        kwargs = session._client.agents.create_kwargs[0]
        assert kwargs["options"]["mcp_servers"] == {
            "fs": {"type": "stdio", "command": "npx"},
        }

    def test_env_kwarg_fallback_when_unsupported(self):
        sdk = make_fake_sdk()
        sdk.CursorClient.reject_env_kwarg = True
        session, _ = make_session(sdk)
        session.ensure_started()
        # Fell back to a plain launch (no env kwarg recorded).
        assert all("env" not in call for call in sdk.CursorClient.launch_calls)

    def test_close_disposes_agent_and_client(self):
        session, _ = make_session()
        session.ensure_started()
        agent, client = session._agent, session._client
        session.close()
        assert agent.closed and client.closed
        assert session._agent is None and session._client is None


class TestResume:
    def test_resume_uses_persisted_record(self, tmp_path):
        persist_agent_record("sess-1", {
            "agent_id": "agent-prev", "runtime": "local", "cwd": "/tmp/proj",
            "model": "composer-2.5",
        })
        session, _ = make_session(session_id="sess-1")
        agent_id = session.ensure_started()
        assert agent_id == "agent-prev"
        assert session._client.agents.resume_calls[0][0] == "agent-prev"
        assert session._client.agents.create_kwargs == []

    def test_resume_failure_falls_back_to_create(self):
        persist_agent_record("sess-2", {
            "agent_id": "agent-gone", "runtime": "local", "cwd": "/tmp/proj",
        })
        session, _ = make_session(session_id="sess-2")
        # resume raises → fresh create, stale record cleared
        # (patch the API after the client exists — build it first)
        sdk = session._sdk

        class RejectingClient(FakeClient):
            @classmethod
            def launch_bridge(cls, **kwargs):
                client = super().launch_bridge(**kwargs)
                client.agents.resume_error = FakeCursorAgentError("not found")
                return client

        sdk.CursorClient = RejectingClient
        agent_id = session.ensure_started()
        assert agent_id == "agent-0001"
        assert load_persisted_agent_record("sess-2")["agent_id"] == "agent-0001"

    def test_cwd_mismatch_skips_resume_for_local(self):
        persist_agent_record("sess-3", {
            "agent_id": "agent-elsewhere", "runtime": "local", "cwd": "/other/dir",
        })
        session, _ = make_session(session_id="sess-3")
        session.ensure_started()
        assert session._client.agents.resume_calls == []
        assert len(session._client.agents.create_kwargs) == 1

    def test_persistence_roundtrip(self):
        persist_agent_record("sess-r", {"agent_id": "a1", "runtime": "local"})
        assert load_persisted_agent_record("sess-r")["agent_id"] == "a1"
        clear_persisted_agent_record("sess-r")
        assert load_persisted_agent_record("sess-r") is None
        assert load_persisted_agent_record(None) is None


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------

class TestRunTurn:
    def test_simple_turn_projects_messages_and_usage(self):
        session, _ = make_session()
        session.ensure_started()
        agent = session._agent
        agent.queue_run(FakeRun(
            [
                {"type": "thinking", "text": "let me look"},
                {"type": "tool_call", "call_id": "c1", "name": "shell",
                 "status": "running", "args": {"command": "ls"}},
                {"type": "tool_call", "call_id": "c1", "name": "shell",
                 "status": "completed", "args": {"command": "ls"}, "result": "a.txt"},
                {"type": "assistant",
                 "message": {"content": [{"type": "text", "text": "Found a.txt"}]}},
                {"type": "usage", "usage": {"input_tokens": 100, "output_tokens": 20,
                                            "total_tokens": 120}},
            ],
            result="Found a.txt",
            usage=SimpleNamespace(input_tokens=100, output_tokens=20,
                                  cache_read_tokens=0, cache_write_tokens=0,
                                  total_tokens=120, reasoning_tokens=None),
        ))
        result = session.run_turn("what files are here?")
        assert result.error is None
        assert result.final_text == "Found a.txt"
        assert result.tool_iterations == 1
        assert result.status == "finished"
        assert result.model_used == "composer-2.5"
        assert result.token_usage_last == {"input_tokens": 100, "output_tokens": 20,
                                           "total_tokens": 120}
        assert result.token_usage_total["total_tokens"] == 120
        roles = [m["role"] for m in result.projected_messages]
        assert roles == ["assistant", "tool", "assistant"]
        assert result.agent_id == "agent-0001"
        assert result.run_id == "run-0001"

    def test_total_only_usage_does_not_masquerade_as_last_step(self):
        session, _ = make_session()
        session.ensure_started()
        session._agent.queue_run(FakeRun(
            [{"type": "assistant",
              "message": {"content": [{"type": "text", "text": "done"}]}}],
            usage=SimpleNamespace(
                input_tokens=300,
                output_tokens=30,
                cache_read_tokens=2700,
                cache_write_tokens=0,
                total_tokens=3030,
                reasoning_tokens=0,
            ),
        ))

        result = session.run_turn("work")

        assert result.token_usage_last is None
        assert result.token_usage_total["total_tokens"] == 3030

    def test_typed_send_callbacks_forward_text_reasoning_and_steps(self):
        text_deltas = []
        reasoning_deltas = []
        steps = []
        session, _ = make_session(
            on_text_delta=text_deltas.append,
            on_reasoning_delta=reasoning_deltas.append,
            on_step=steps.append,
        )
        session.ensure_started()

        session.run_turn("stream")
        options = session._agent.send_calls[0][1]
        options.on_delta(SimpleNamespace(type="text-delta", text="hello"))
        options.on_delta(SimpleNamespace(type="thinking-delta", text="hmm"))
        options.on_step({"type": "assistantMessage"})

        assert text_deltas == ["hello"]
        assert reasoning_deltas == ["hmm"]
        assert steps == [{"type": "assistantMessage"}]

    def test_tool_progress_bridged(self):
        seen = []
        session, _ = make_session(on_tool_event=lambda n, p, a: seen.append((n, p)))
        session.ensure_started()
        session._agent.queue_run(FakeRun([
            {"type": "tool_call", "call_id": "c1", "name": "shell",
             "status": "running", "args": {"command": "pwd"}},
            {"type": "tool_call", "call_id": "c1", "name": "shell",
             "status": "completed", "args": {}, "result": "/tmp"},
        ]))
        session.run_turn("where am I?")
        assert seen == [("exec_command", "pwd")]

    def test_model_override_sent_once_and_sticky(self):
        session, _ = make_session(model="composer-2.5")
        session.ensure_started()
        agent = session._agent

        session.run_turn("hi")
        assert agent.send_calls[0][1] is None

        session.run_turn("hi again", model="gpt-5.3-codex-high")
        assert agent.send_calls[1][1]["model"] == "gpt-5.3-codex-high"

        session.run_turn("third", model="gpt-5.3-codex-high")
        assert agent.send_calls[2][1] is None  # sticky — no re-override

    def test_mode_override(self):
        session, _ = make_session()
        session.ensure_started()
        session.run_turn("plan it", mode="plan")
        assert session._agent.send_calls[0][1]["mode"] == "plan"

    def test_images_build_structured_message(self):
        session, _ = make_session()
        session.ensure_started()
        session.run_turn("look", images=[{"data": "aGk=", "mime_type": "image/png"}])
        message, _ = session._agent.send_calls[0]
        assert message == {"text": "look",
                           "images": [{"data": "aGk=", "mime_type": "image/png"}]}

    def test_stranded_tool_call_closed_on_finalize(self):
        session, _ = make_session()
        session.ensure_started()
        session._agent.queue_run(FakeRun(
            [
                {"type": "tool_call", "call_id": "c1", "name": "edit",
                 "status": "running", "args": {"path": "a.py"}},
            ],
            status="error", result="",
        ))
        result = session.run_turn("edit it")
        roles = [m["role"] for m in result.projected_messages]
        assert roles == ["assistant", "tool"]
        assert result.projected_messages[1]["content"].startswith("[error]")
        assert result.status == "error"
        assert result.error  # error status surfaced

    def test_startup_failure_returns_error_result(self):
        sdk = make_fake_sdk()

        class BrokenClient(FakeClient):
            @classmethod
            def launch_bridge(cls, **kwargs):
                raise RuntimeError("bridge exploded")

        sdk.CursorClient = BrokenClient
        session, _ = make_session(sdk)
        result = session.run_turn("hello")
        assert "bridge exploded" in result.error
        assert result.should_retire is True


class TestSendRecovery:
    def test_agent_busy_cancels_active_run_and_retries(self):
        session, _ = make_session()
        session.ensure_started()
        agent = session._agent
        agent.send_errors.append(FakeAgentBusyError("agent_busy"))
        active = FakeRun()
        active.status = "running"
        session._client.agents.listed_runs = [active]

        result = session.run_turn("go")
        assert result.error is None
        assert active.cancel_calls == 1
        assert len(agent.send_calls) == 2

    def test_rate_limit_retries_with_retry_after(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(session_mod.time, "sleep", lambda s: sleeps.append(s))
        session, _ = make_session()
        session.ensure_started()
        session._agent.send_errors.append(
            FakeRateLimitError("slow down", retry_after="3"))
        result = session.run_turn("go")
        assert result.error is None
        assert sleeps == [3.0]

    def test_local_stuck_run_retries_with_force(self):
        session, _ = make_session()
        session.ensure_started()
        agent = session._agent
        agent.send_errors.append(
            FakeCursorAgentError("agent already has an active run"))
        result = session.run_turn("go")
        assert result.error is None
        assert agent.send_calls[1][1]["local"]["force"] is True

    def test_non_retryable_error_surfaces(self):
        session, _ = make_session()
        session.ensure_started()
        session._agent.send_errors.append(
            FakeCursorAgentError("invalid model", request_id="req-9"))
        result = session.run_turn("go")
        assert "invalid model" in result.error
        assert "req-9" in result.error

    def test_auth_failure_marks_retire(self):
        session, _ = make_session()
        session.ensure_started()
        session._agent.send_errors.append(FakeAuthenticationError("bad api key"))
        # AuthenticationError isn't in the recovery set → surfaces as error.
        result = session.run_turn("go")
        assert result.error and result.should_retire is True


class TestInterruptAndTimeout:
    def test_stale_session_interrupt_is_cleared_before_next_turn(self):
        session, _ = make_session()
        session.ensure_started()
        session.request_interrupt()

        result = session.run_turn("next turn")

        assert result.interrupted is False
        assert result.status == "finished"

    def test_interrupt_cancels_blocking_run(self):
        session, _ = make_session()
        session.ensure_started()
        run = FakeRun(
            [{"type": "assistant",
              "message": {"content": [{"type": "text", "text": "partial"}]}}],
            block_until_cancel=True,
        )
        session._agent.queue_run(run)

        timer = threading.Timer(0.4, session.request_interrupt)
        timer.start()
        try:
            result = session.run_turn("long task")
        finally:
            timer.cancel()
        assert result.interrupted is True
        assert run.cancel_calls >= 1
        assert result.status == "cancelled"
        # Partial output survives.
        assert "partial" in result.final_text

    def test_interrupt_check_callable_polled(self):
        flag = {"stop": False}
        session, _ = make_session(interrupt_check=lambda: flag["stop"])
        session.ensure_started()
        run = FakeRun([], block_until_cancel=True)
        session._agent.queue_run(run)
        timer = threading.Timer(0.4, lambda: flag.update(stop=True))
        timer.start()
        try:
            result = session.run_turn("long")
        finally:
            timer.cancel()
        assert result.interrupted is True

    def test_idle_timeout_cancels_and_retires(self, monkeypatch):
        session, _ = make_session(config={"timeout_seconds": 1})
        monkeypatch.setattr(session_mod, "_CANCEL_DRAIN_SECONDS", 0.5)
        # Force a sub-minimum timeout for test speed.
        monkeypatch.setattr(
            CursorSDKSession, "timeout_seconds",
            property(lambda self: 0.6),
        )
        session.ensure_started()
        run = FakeRun([], block_until_cancel=True)
        session._agent.queue_run(run)
        start = time.monotonic()
        result = session.run_turn("never answers")
        assert time.monotonic() - start < 10
        assert result.should_retire is True
        assert "idle timeout" in (result.error or "")
        assert run.cancel_calls >= 1

    def test_non_draining_cancel_never_enters_unbounded_wait(self, monkeypatch):
        class NonDrainingRun(FakeRun):
            def __init__(self):
                super().__init__()
                self.wait_calls = 0
                self.cancel_calls = 0
                self.status = "running"

            def messages(self):
                threading.Event().wait()
                yield None

            def cancel(self):
                self.cancel_calls += 1

            def wait(self):
                self.wait_calls += 1
                threading.Event().wait()

        session, _ = make_session(config={"timeout_seconds": 1})
        monkeypatch.setattr(session_mod, "_CANCEL_DRAIN_SECONDS", 0.1)
        monkeypatch.setattr(session_mod, "_POLL_SECONDS", 0.01)
        monkeypatch.setattr(
            CursorSDKSession, "timeout_seconds",
            property(lambda self: 0.05),
        )
        session.ensure_started()
        run = NonDrainingRun()
        session._agent.queue_run(run)

        start = time.monotonic()
        result = session.run_turn("never drains")

        assert time.monotonic() - start < 2
        assert result.should_retire is True
        assert "idle timeout" in (result.error or "")
        assert run.cancel_calls == 1
        assert run.wait_calls == 0
