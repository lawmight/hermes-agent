"""E2E tests for the cursor provider registration + runtime resolution chain.

Real imports against the isolated HERMES_HOME (no mocks on the chain itself,
per the AGENTS.md E2E rule): profile discovery → PROVIDER_REGISTRY auto-sync
→ CANONICAL_PROVIDERS auto-extend → provider_catalog → runtime resolution.
Behavior contracts only — no catalog snapshots (change-detector rule).
"""

from __future__ import annotations

import io
import json
from unittest import mock

import pytest


class TestProfileRegistration:
    def test_profile_registered_with_cursor_agent_mode(self):
        from providers import get_provider_profile

        profile = get_provider_profile("cursor")
        assert profile is not None
        assert profile.api_mode == "cursor_agent"
        assert profile.auth_type == "api_key"
        assert "CURSOR_API_KEY" in profile.env_vars
        assert profile.base_url.startswith("https://api.cursor.com")
        # No OpenAI-compat /models endpoint — doctor must not probe it.
        assert profile.supports_health_check is False
        assert len(profile.fallback_models) >= 1

    def test_aliases_resolve(self):
        from providers import get_provider_profile

        for alias in ("cursor-sdk", "cursor-agent", "composer"):
            resolved = get_provider_profile(alias)
            assert resolved is not None and resolved.name == "cursor"

    def test_shared_cli_provider_resolver_sees_plugin_profile(self):
        from hermes_cli.providers import determine_api_mode, resolve_provider_full

        for name in ("cursor", "cursor-sdk", "composer"):
            resolved = resolve_provider_full(name)
            assert resolved is not None
            assert resolved.id == "cursor"
            assert resolved.source == "plugin"
        assert determine_api_mode("cursor") == "cursor_agent"

    def test_registry_auto_sync(self):
        from hermes_cli.auth import PROVIDER_REGISTRY, resolve_provider

        pconfig = PROVIDER_REGISTRY.get("cursor")
        assert pconfig is not None
        assert pconfig.auth_type == "api_key"
        assert "CURSOR_API_KEY" in pconfig.api_key_env_vars
        assert resolve_provider("cursor") == "cursor"
        assert resolve_provider("composer") == "cursor"

    def test_canonical_picker_membership(self):
        from hermes_cli.models import CANONICAL_PROVIDERS

        assert "cursor" in {p.slug for p in CANONICAL_PROVIDERS}

    def test_provider_catalog_keys_tab(self):
        from hermes_cli.provider_catalog import provider_catalog

        descriptors = [d for d in provider_catalog() if d.slug == "cursor"]
        assert len(descriptors) == 1
        assert descriptors[0].tab == "keys"
        assert "CURSOR_API_KEY" in descriptors[0].api_key_env_vars

    def test_optional_env_var_metadata(self):
        from hermes_cli.config import OPTIONAL_ENV_VARS

        meta = OPTIONAL_ENV_VARS.get("CURSOR_API_KEY")
        assert meta is not None
        assert meta["password"] is True
        assert meta["category"] == "provider"

    def test_lazy_dep_allowlisted(self):
        from tools.lazy_deps import feature_specs

        specs = feature_specs("provider.cursor")
        assert any(s.startswith("cursor-sdk") for s in specs)

    def test_fallback_models_have_context_lengths(self):
        """Invariant: every curated cursor model resolves a context length."""
        from hermes_cli.models import _PROVIDER_MODELS
        from agent.model_metadata import DEFAULT_CONTEXT_LENGTHS

        keys = [k.lower() for k in DEFAULT_CONTEXT_LENGTHS]
        for model in _PROVIDER_MODELS["cursor"]:
            lowered = model.lower()
            assert any(key in lowered for key in keys), (
                f"{model} has no DEFAULT_CONTEXT_LENGTHS entry/family match"
            )


class TestRuntimeResolution:
    def test_resolves_cursor_agent_api_mode(self, monkeypatch):
        monkeypatch.setenv("CURSOR_API_KEY", "crsr_e2e_test")
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested="cursor")
        assert runtime["provider"] == "cursor"
        assert runtime["api_mode"] == "cursor_agent"
        assert runtime["base_url"].startswith("https://api.cursor.com")
        assert runtime["api_key"] == "crsr_e2e_test"

    def test_stale_persisted_api_mode_never_leaks(self, monkeypatch):
        """A stale model.api_mode from a previous provider must not override
        the cursor runtime — there is no other surface to speak."""
        monkeypatch.setenv("CURSOR_API_KEY", "crsr_e2e_test")
        from hermes_cli import runtime_provider as rp

        with mock.patch.object(
            rp, "_get_model_config",
            return_value={"provider": "cursor", "api_mode": "chat_completions",
                          "default": "composer-2.5"},
        ):
            runtime = rp.resolve_runtime_provider(requested="cursor")
        assert runtime["api_mode"] == "cursor_agent"

    def test_cursor_agent_in_valid_api_modes(self):
        from hermes_cli.runtime_provider import _VALID_API_MODES

        assert "cursor_agent" in _VALID_API_MODES


class TestModelCatalogFetch:
    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def test_fetch_models_parses_v1_shape(self):
        from providers import get_provider_profile

        profile = get_provider_profile("cursor")
        payload = {"models": [
            {"id": "composer-2.5", "parameters": [{"id": "fast"}]},
            {"id": "gpt-5.3-codex-high"},
        ]}
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self._Response(json.dumps(payload).encode()),
        ):
            ids = profile.fetch_models(api_key="k")
        assert ids == ["composer-2.5", "gpt-5.3-codex-high"]

    def test_fetch_models_parses_items_shape(self):
        from providers import get_provider_profile

        profile = get_provider_profile("cursor")
        payload = {"items": [
            {"id": "composer-2.5", "displayName": "Composer 2.5"},
            {"id": "grok-4.5"},
        ]}
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self._Response(json.dumps(payload).encode()),
        ):
            ids = profile.fetch_models(api_key="k")
        assert ids == ["composer-2.5", "grok-4.5"]

    def test_model_validation_uses_cursor_catalog_and_aliases(self):
        from hermes_cli.models import validate_requested_model

        payload = {"items": [
            {"id": "composer-2.5", "aliases": ["composer"]},
            {"id": "claude-fable-5", "aliases": ["fable"]},
        ]}
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self._Response(json.dumps(payload).encode()),
        ):
            exact = validate_requested_model(
                "composer-2.5",
                "cursor",
                api_key="k",
                base_url="https://api.cursor.com",
            )
            alias = validate_requested_model(
                "fable",
                "cursor",
                api_key="k",
                base_url="https://api.cursor.com",
            )

        assert exact["accepted"] is True
        assert exact["recognized"] is True
        assert alias["accepted"] is True
        assert alias["corrected_model"] == "claude-fable-5"

    def test_fetch_models_tolerates_legacy_v0_shape(self):
        from providers import get_provider_profile

        profile = get_provider_profile("cursor")
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self._Response(json.dumps({"models": ["a", "b"]}).encode()),
        ):
            assert profile.fetch_models(api_key="k") == ["a", "b"]

    def test_fetch_models_prefers_nonempty_models_over_empty_items(self):
        from providers import get_provider_profile

        profile = get_provider_profile("cursor")
        payload = {"items": [], "models": [{"id": "composer-2.5"}]}
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self._Response(json.dumps(payload).encode()),
        ):
            assert profile.fetch_models(api_key="k") == ["composer-2.5"]

    def test_fetch_models_failure_returns_none(self):
        from providers import get_provider_profile

        profile = get_provider_profile("cursor")
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            assert profile.fetch_models(api_key="k") is None
        assert profile.fetch_models(api_key=None) is None

    def test_live_first_picker_merge(self):
        """Cursor's live catalog is authoritative → live-first merge."""
        from hermes_cli.models import _LIVE_FIRST_PICKER_PROVIDERS

        assert "cursor" in _LIVE_FIRST_PICKER_PROVIDERS

    def test_profile_description_copy(self):
        from providers import get_provider_profile

        profile = get_provider_profile("cursor")
        assert "First-party + API models" in profile.description
        assert "Composer + frontier" not in profile.description


class TestModelSetupFlow:
    def test_cursor_skips_base_url_prompt(self, monkeypatch, capsys):
        from hermes_cli.main import _model_flow_api_key_provider
        from hermes_cli.config import load_config

        monkeypatch.setenv("CURSOR_API_KEY", "crsr_test_key")
        inputs: list[str] = []

        def fake_input(prompt=""):
            inputs.append(prompt)
            raise AssertionError(f"unexpected input prompt: {prompt!r}")

        monkeypatch.setattr("builtins.input", fake_input)
        monkeypatch.setattr(
            "hermes_cli.main._prompt_api_key",
            lambda pconfig, existing_key, provider_id=None: (existing_key, False),
        )
        monkeypatch.setattr(
            "hermes_cli.auth._prompt_model_selection",
            lambda *a, **k: None,
        )
        from providers import get_provider_profile

        profile = get_provider_profile("cursor")
        monkeypatch.setattr(
            profile,
            "fetch_models",
            lambda **kw: ["composer-2.5", "grok-4.5"],
        )

        _model_flow_api_key_provider(load_config(), "cursor", "composer-2.5")

        assert not any("Base URL" in p for p in inputs)
        out = capsys.readouterr().out
        assert "Endpoint: https://api.cursor.com" in out
        assert "Found 2 model(s) from Cursor API" in out


class TestBillingRoute:
    def test_cursor_is_subscription_included(self):
        from agent.usage_pricing import resolve_billing_route

        route = resolve_billing_route("composer-2.5", provider="cursor")
        assert route.billing_mode == "subscription_included"

    def test_hostname_match_without_provider(self):
        from agent.usage_pricing import resolve_billing_route

        route = resolve_billing_route(
            "composer-2.5", base_url="https://api.cursor.com"
        )
        assert route.billing_mode == "subscription_included"

    def test_lookalike_hostname_rejected(self):
        from agent.usage_pricing import resolve_billing_route

        route = resolve_billing_route(
            "composer-2.5", base_url="https://api.cursor.com.attacker.test"
        )
        assert route.billing_mode != "subscription_included"
