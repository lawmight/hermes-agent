from pathlib import Path
import sys
from unittest.mock import patch

import pytest

from tools.environments.local import LocalEnvironment


class TestLocalTempDir:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX TMPDIR + /tmp layout is exercised on POSIX CI",
    )
    def test_uses_os_tmpdir_for_session_artifacts(self, monkeypatch):
        monkeypatch.setenv("TMPDIR", "/data/data/com.termux/files/usr/tmp")
        monkeypatch.delenv("TMP", raising=False)
        monkeypatch.delenv("TEMP", raising=False)

        with patch.object(LocalEnvironment, "init_session", autospec=True, return_value=None):
            env = LocalEnvironment(cwd=".", timeout=10)

        assert env.get_temp_dir() == "/data/data/com.termux/files/usr/tmp"
        assert env._snapshot_path == f"/data/data/com.termux/files/usr/tmp/hermes-snap-{env._session_id}.sh"
        assert env._cwd_file == f"/data/data/com.termux/files/usr/tmp/hermes-cwd-{env._session_id}.txt"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX TMPDIR + /tmp layout is exercised on POSIX CI",
    )
    def test_prefers_backend_env_tmpdir_override(self, monkeypatch):
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.delenv("TMP", raising=False)
        monkeypatch.delenv("TEMP", raising=False)

        with patch.object(LocalEnvironment, "init_session", autospec=True, return_value=None):
            env = LocalEnvironment(
                cwd=".",
                timeout=10,
                env={"TMPDIR": "/data/data/com.termux/files/home/.cache/hermes-tmp/"},
            )

        assert env.get_temp_dir() == "/data/data/com.termux/files/home/.cache/hermes-tmp"
        assert env._snapshot_path == (
            f"/data/data/com.termux/files/home/.cache/hermes-tmp/hermes-snap-{env._session_id}.sh"
        )
        assert env._cwd_file == (
            f"/data/data/com.termux/files/home/.cache/hermes-tmp/hermes-cwd-{env._session_id}.txt"
        )

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX TMPDIR + /tmp layout is exercised on POSIX CI",
    )
    def test_falls_back_to_tempfile_when_tmp_missing(self, monkeypatch):
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.delenv("TMP", raising=False)
        monkeypatch.delenv("TEMP", raising=False)

        with patch("tools.environments.local.os.path.isdir", return_value=False), \
             patch("tools.environments.local.os.access", return_value=False), \
             patch("tools.environments.local.tempfile.gettempdir", return_value="/cache/tmp"), \
             patch.object(LocalEnvironment, "init_session", autospec=True, return_value=None):
            env = LocalEnvironment(cwd=".", timeout=10)
            assert env.get_temp_dir() == "/cache/tmp"
            assert env._snapshot_path == f"/cache/tmp/hermes-snap-{env._session_id}.sh"
            assert env._cwd_file == f"/cache/tmp/hermes-cwd-{env._session_id}.txt"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows native temp resolution")
    def test_windows_uses_temp_env_when_set(self, monkeypatch, tmp_path):
        """Windows prefers TMP/TEMP; snapshot paths stay under that root."""
        fake = tmp_path / "win_tmp"
        fake.mkdir()
        for k in ("TMPDIR", "TMP"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("TEMP", str(fake))
        with patch.object(LocalEnvironment, "init_session", autospec=True, return_value=None):
            env = LocalEnvironment(cwd=".", timeout=10)
        expected = str(Path(fake).resolve()).replace("\\", "/")
        assert env.get_temp_dir().replace("\\", "/") == expected
        assert env.get_temp_dir() != "/tmp"
        snap = env._snapshot_path.replace("\\", "/")
        assert snap == f"{expected}/hermes-snap-{env._session_id}.sh"
