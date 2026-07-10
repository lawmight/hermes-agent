"""Security contracts for cursor-sdk bridge process launch."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

from agent.transports.cursor_bridge import launch_cursor_bridge
from tools.environments.local import hermes_subprocess_env


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
    assert seen["kwargs"]["allow_api_key_env_fallback"] is False
    assert bridge_module._bridge_subprocess_env is original_builder


def test_cursor_browser_session_is_always_stripped(monkeypatch):
    monkeypatch.setenv("WorkosCursorSessionToken", "browser-session")

    sanitized = hermes_subprocess_env(inherit_credentials=True)

    assert "WorkosCursorSessionToken" not in sanitized
