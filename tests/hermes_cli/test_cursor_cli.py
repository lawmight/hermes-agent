"""Tests for `hermes cursor` — argparse wiring + verb behavior with a fake SDK."""

from __future__ import annotations

import argparse
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import hermes_cli.cursor_cli as cursor_cli


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeRun:
    def __init__(self, run_id="run-1", status="running", supports_stream=True):
        self.id = run_id
        self.status = status
        self.result = ""
        self.cancelled = False
        self.supports_stream = supports_stream

    def messages(self):
        yield {"type": "assistant",
               "message": {"content": [{"type": "text", "text": "done!"}]}}

    def wait(self):
        return SimpleNamespace(status="finished", result="done!")

    def cancel(self):
        self.cancelled = True

    def supports(self, operation):
        return operation == "stream" and self.supports_stream


class FakeAgent:
    def __init__(self, agent_id="bc-123"):
        self.agent_id = agent_id
        self.sent = []
        self.artifacts = []
        self.downloads = {}

    def send(self, prompt, options=None):
        self.sent.append(prompt)
        return FakeRun()

    def list_artifacts(self):
        return self.artifacts

    def download_artifact(self, path):
        return self.downloads[path]


class FakeAgentsAPI:
    def __init__(self):
        self.created = []
        self.agent = FakeAgent()
        self.infos = []
        self.runs = []
        self.lifecycle_calls = []
        self.list_kwargs = []
        self.get_calls = []
        self.list_runs_calls = []
        self.get_run_calls = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return self.agent

    def resume(self, agent_id, options=None):
        return self.agent

    def list(self, **kwargs):
        self.list_kwargs.append(kwargs)
        return SimpleNamespace(items=self.infos)

    def get(self, agent_id, **kwargs):
        self.get_calls.append((agent_id, kwargs))
        return self.infos[0] if self.infos else SimpleNamespace(
            agent_id=agent_id, status="finished", name="", summary="", archived=False)

    def list_runs(self, agent_id, **kwargs):
        self.list_runs_calls.append((agent_id, kwargs))
        return SimpleNamespace(items=self.runs)

    def get_run(self, run_id, options=None):
        self.get_run_calls.append((run_id, options))
        return next(r for r in self.runs if r.id == run_id)

    def archive(self, agent_id, options=None):
        self.lifecycle_calls.append(("archive", agent_id))

    def unarchive(self, agent_id, options=None):
        self.lifecycle_calls.append(("unarchive", agent_id))

    def delete(self, agent_id, options=None):
        self.lifecycle_calls.append(("delete", agent_id))


class FakeClient:
    def __init__(self):
        self.agents = FakeAgentsAPI()
        self.closed = False

    @classmethod
    def launch_bridge(cls, **kwargs):
        return _CURRENT_CLIENT

    def close(self):
        self.closed = True


_CURRENT_CLIENT = FakeClient()


def make_fake_sdk(client=None):
    global _CURRENT_CLIENT
    _CURRENT_CLIENT = client or FakeClient()
    repos = SimpleNamespace(items=[SimpleNamespace(url="https://github.com/o/r")])
    return SimpleNamespace(
        CursorClient=FakeClient,
        Cursor=SimpleNamespace(
            repositories=SimpleNamespace(list=lambda api_key=None: repos)
        ),
    ), _CURRENT_CLIENT


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "crsr_test")


def _args(**kwargs):
    defaults = dict(cursor_action=None)
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class _FakeRestResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# Parser wiring
# ---------------------------------------------------------------------------

class TestParserWiring:
    def _parse(self, argv):
        from hermes_cli.subcommands.cursor import build_cursor_parser

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        build_cursor_parser(sub, cmd_cursor=lambda a: None)
        return parser.parse_args(argv)

    def test_launch_flags(self):
        args = self._parse([
            "cursor", "launch", "fix the bug", "--repo", "https://github.com/o/r",
            "--ref", "main", "--model", "composer-2.5", "--pr",
            "--branch-current", "--pool", "p1", "--env-var", "A=1",
            "--env-var", "B=2", "--follow", "--name", "fixer",
        ])
        assert args.cursor_action == "launch"
        assert args.prompt == "fix the bug"
        assert args.repo == "https://github.com/o/r"
        assert args.ref == "main"
        assert args.pr and args.branch_current and args.follow
        assert args.env_var == ["A=1", "B=2"]
        assert args.pool == "p1"

    def test_all_verbs_parse(self):
        for argv in (
            ["cursor", "models"],
            ["cursor", "me"],
            ["cursor", "repos"],
            ["cursor", "list", "--archived"],
            ["cursor", "ls"],
            ["cursor", "status", "bc-1"],
            ["cursor", "follow", "bc-1"],
            ["cursor", "send", "bc-1", "more"],
            ["cursor", "cancel", "bc-1"],
            ["cursor", "artifacts", "bc-1", "--download", "/tmp/x"],
            ["cursor", "archive", "bc-1"],
            ["cursor", "unarchive", "bc-1"],
            ["cursor", "delete", "bc-1", "--yes"],
        ):
            args = self._parse(argv)
            assert args.cursor_action in (argv[1] if argv[1] != "ls" else "ls",
                                          argv[1])

    def test_main_wires_cursor_subcommand(self):
        # The dispatcher must know "cursor" is a builtin (plugin scan skip).
        from hermes_cli.main import _BUILTIN_SUBCOMMANDS

        assert "cursor" in _BUILTIN_SUBCOMMANDS


# ---------------------------------------------------------------------------
# REST verbs (no SDK)
# ---------------------------------------------------------------------------

class TestRestVerbs:
    def test_models_prints_catalog(self, capsys):
        payload = {"items": [
            {"id": "composer-2.5", "displayName": "Composer 2.5",
             "aliases": ["composer-latest"],
             "parameters": [{"id": "fast", "values": [{"value": "true"},
                                                      {"value": "false"}]}]},
            "legacy-id",
        ]}
        with patch.object(cursor_cli.urllib.request, "urlopen",
                          return_value=_FakeRestResponse(json.dumps(payload).encode())):
            rc = cursor_cli.cmd_models(_args(cursor_action="models"))
        out = capsys.readouterr().out
        assert rc == 0
        assert "composer-2.5" in out
        assert "param fast: true|false" in out
        assert "legacy-id" in out

    def test_models_accepts_legacy_models_key(self, capsys):
        payload = {"models": [{"id": "composer-2.5"}]}
        with patch.object(cursor_cli.urllib.request, "urlopen",
                          return_value=_FakeRestResponse(json.dumps(payload).encode())):
            rc = cursor_cli.cmd_models(_args(cursor_action="models"))
        assert rc == 0
        assert "composer-2.5" in capsys.readouterr().out

    def test_models_requires_key(self, monkeypatch, capsys):
        monkeypatch.delenv("CURSOR_API_KEY", raising=False)
        rc = cursor_cli.cmd_models(_args(cursor_action="models"))
        assert rc == 1
        assert "CURSOR_API_KEY" in capsys.readouterr().err

    def test_me_valid(self, capsys):
        payload = {"userEmail": "u@example.com", "apiKeyName": "hermes"}
        with patch.object(cursor_cli.urllib.request, "urlopen",
                          return_value=_FakeRestResponse(json.dumps(payload).encode())):
            rc = cursor_cli.cmd_me(_args(cursor_action="me"))
        out = capsys.readouterr().out
        assert rc == 0
        assert "valid" in out and "u@example.com" in out


# ---------------------------------------------------------------------------
# SDK verbs
# ---------------------------------------------------------------------------

class TestSdkVerbs:
    def test_launch_builds_cloud_options(self, capsys):
        sdk, client = make_fake_sdk()
        with patch.object(cursor_cli, "_get_sdk", return_value=sdk):
            rc = cursor_cli.cmd_launch(_args(
                cursor_action="launch", prompt="fix it",
                repo="https://github.com/o/r", ref="main", model="composer-2.5",
                name="fixer", pr=True, branch_current=True, pool="p1",
                env_var=["TOKEN=t1"], follow=False,
            ))
        assert rc == 0
        created = client.agents.created[0]
        assert created["model"] == "composer-2.5"
        assert created["name"] == "fixer"
        cloud = created["cloud"]
        assert cloud["repos"] == [{"url": "https://github.com/o/r",
                                   "starting_ref": "main"}]
        assert cloud["auto_create_pr"] is True
        assert cloud["work_on_current_branch"] is True
        assert cloud["env"] == {"type": "pool", "name": "p1"}
        assert cloud["env_vars"] == {"TOKEN": "t1"}
        assert client.agents.agent.sent == ["fix it"]
        assert "bc-123" in capsys.readouterr().out

    def test_launch_rejects_bad_env_var(self, capsys):
        sdk, _ = make_fake_sdk()
        with patch.object(cursor_cli, "_get_sdk", return_value=sdk):
            rc = cursor_cli.cmd_launch(_args(
                cursor_action="launch", prompt="x", repo="", ref="", model="",
                name="", pr=False, branch_current=False, pool="",
                env_var=["NOEQUALS"], follow=False,
            ))
        assert rc == 1

    def test_list_and_status(self, capsys):
        sdk, client = make_fake_sdk()
        client.agents.infos = [SimpleNamespace(
            agent_id="bc-9", status="running", name="worker",
            summary="doing things", archived=False)]
        client.agents.runs = [FakeRun("run-9", status="running")]
        with patch.object(cursor_cli, "_get_sdk", return_value=sdk):
            assert cursor_cli.cmd_list(_args(cursor_action="list", archived=False)) == 0
            assert cursor_cli.cmd_status(_args(cursor_action="status", agent_id="bc-9")) == 0
        out = capsys.readouterr().out
        assert "bc-9" in out and "running" in out and "run-9" in out
        assert client.agents.list_kwargs[0] == {
            "runtime": "cloud",
            "api_key": "crsr_test",
        }
        assert client.agents.get_calls[-1] == (
            "bc-9",
            {"api_key": "crsr_test"},
        )
        assert client.agents.list_runs_calls[-1] == (
            "bc-9",
            {"runtime": "cloud", "api_key": "crsr_test"},
        )
        assert client.closed is True

    def test_follow_detached_run_waits_and_prints_result(self, capsys):
        sdk, client = make_fake_sdk()
        run = FakeRun("run-detached", status="running", supports_stream=False)
        client.agents.runs = [run]

        with patch.object(cursor_cli, "_get_sdk", return_value=sdk):
            rc = cursor_cli.cmd_follow(
                _args(cursor_action="follow", agent_id="bc-9")
            )

        out = capsys.readouterr().out
        assert rc == 0
        assert "live event replay is unavailable" in out
        assert "done!" in out
        assert "run status: finished" in out
        assert client.agents.get_run_calls == [
            (
                "run-detached",
                {
                    "runtime": "cloud",
                    "agent_id": "bc-9",
                    "api_key": "crsr_test",
                },
            )
        ]

    def test_send_and_cancel(self, capsys):
        sdk, client = make_fake_sdk()
        active = FakeRun("run-a", status="running")
        client.agents.runs = [active]
        with patch.object(cursor_cli, "_get_sdk", return_value=sdk):
            assert cursor_cli.cmd_send(_args(
                cursor_action="send", agent_id="bc-1", prompt="more",
                follow=False)) == 0
            assert cursor_cli.cmd_cancel(_args(
                cursor_action="cancel", agent_id="bc-1")) == 0
        assert client.agents.agent.sent == ["more"]
        assert active.cancelled is True
        assert client.agents.list_runs_calls[-1] == (
            "bc-1",
            {"runtime": "cloud", "api_key": "crsr_test"},
        )

    def test_artifacts_download(self, tmp_path, capsys):
        sdk, client = make_fake_sdk()
        client.agents.agent.artifacts = [
            SimpleNamespace(path="review.md", size_bytes=4)]
        client.agents.agent.downloads = {"review.md": b"data"}
        with patch.object(cursor_cli, "_get_sdk", return_value=sdk):
            rc = cursor_cli.cmd_artifacts(_args(
                cursor_action="artifacts", agent_id="bc-1",
                download=str(tmp_path / "out")))
        assert rc == 0
        assert (tmp_path / "out" / "review.md").read_bytes() == b"data"

    def test_delete_requires_yes(self, capsys):
        sdk, client = make_fake_sdk()
        with patch.object(cursor_cli, "_get_sdk", return_value=sdk):
            assert cursor_cli.cmd_delete(_args(
                cursor_action="delete", agent_id="bc-1", yes=False)) == 1
            assert client.agents.lifecycle_calls == []
            assert cursor_cli.cmd_delete(_args(
                cursor_action="delete", agent_id="bc-1", yes=True)) == 0
            assert client.agents.lifecycle_calls == [("delete", "bc-1")]

    def test_archive_unarchive(self):
        sdk, client = make_fake_sdk()
        with patch.object(cursor_cli, "_get_sdk", return_value=sdk):
            assert cursor_cli.cmd_archive(_args(
                cursor_action="archive", agent_id="bc-1")) == 0
            assert cursor_cli.cmd_unarchive(_args(
                cursor_action="unarchive", agent_id="bc-1")) == 0
        assert client.agents.lifecycle_calls == [
            ("archive", "bc-1"), ("unarchive", "bc-1")]

    def test_repos(self, capsys):
        sdk, _ = make_fake_sdk()
        with patch.object(cursor_cli, "_get_sdk", return_value=sdk):
            assert cursor_cli.cmd_repos(_args(cursor_action="repos")) == 0
        assert "github.com/o/r" in capsys.readouterr().out


class TestDispatch:
    def test_unknown_action(self, capsys):
        assert cursor_cli.cursor_command(_args(cursor_action="bogus")) == 1

    def test_no_action_prints_usage(self, capsys):
        assert cursor_cli.cursor_command(_args(cursor_action=None)) == 2
        assert "usage" in capsys.readouterr().out

    def test_sdk_unavailable_message(self, capsys, monkeypatch):
        def raise_unavailable():
            raise RuntimeError("cursor-sdk is not installed")

        monkeypatch.setattr(cursor_cli, "_get_sdk", raise_unavailable)
        rc = cursor_cli.cursor_command(_args(
            cursor_action="list", archived=False))
        assert rc == 1
        assert "cursor-sdk" in capsys.readouterr().err
