"""Security contracts for cursor-sdk bridge process launch."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.transports.cursor_bridge import launch_cursor_bridge
from tools.environments.local import (
    _make_run_env,
    _sanitize_subprocess_env,
    hermes_subprocess_env,
)


def test_bridge_launch_uses_sanitized_environment_and_restores_sdk_hook():
    original_builder = lambda: {"UNSAFE": "secret"}
    bridge_module = SimpleNamespace(_bridge_subprocess_env=original_builder)
    seen = {}

    class FakeClient:
        @classmethod
        def launch_bridge(cls, **kwargs):
            seen["env"] = bridge_module._bridge_subprocess_env()
            seen["kwargs"] = kwargs
            return object()

    sdk = SimpleNamespace(
        __name__="fake_cursor_sdk",
        CursorClient=FakeClient,
    )

    with patch.dict(
        sys.modules,
        {"fake_cursor_sdk._bridge": bridge_module},
    ), patch(
        "agent.transports.cursor_bridge.hermes_subprocess_env",
        return_value={"SAFE": "1"},
    ):
        launch_cursor_bridge(sdk, workspace="/tmp/project")

    assert seen["env"] == {
        "SAFE": "1",
        "CURSOR_SDK_CLIENT_LANGUAGE": "python",
    }
    assert seen["kwargs"]["allow_api_key_env_fallback"] is True
    assert bridge_module._bridge_subprocess_env is original_builder


def test_cursor_browser_session_is_always_stripped(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-key")
    monkeypatch.setenv("WorkosCursorSessionToken", "browser-session")

    sanitized = hermes_subprocess_env(inherit_credentials=True)
    terminal_env = _make_run_env({})
    background_env = _sanitize_subprocess_env(
        {
            "CURSOR_API_KEY": "cursor-key",
            "WorkosCursorSessionToken": "browser-session",
        }
    )

    assert "CURSOR_API_KEY" not in sanitized
    assert "WorkosCursorSessionToken" not in sanitized
    assert "CURSOR_API_KEY" not in terminal_env
    assert "WorkosCursorSessionToken" not in terminal_env
    assert "CURSOR_API_KEY" not in background_env
    assert "WorkosCursorSessionToken" not in background_env


def test_real_sdk_name_fails_closed_without_environment_hook():
    sdk = SimpleNamespace(
        __name__="cursor_sdk",
        CursorClient=SimpleNamespace(),
    )
    with patch.dict(
        sys.modules,
        {"cursor_sdk._bridge": SimpleNamespace()},
    ), pytest.raises(RuntimeError, match="refusing to launch"):
        launch_cursor_bridge(sdk, workspace="/tmp/project")
