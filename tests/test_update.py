# Copyright (c) serrebidev and contributors
# This file is part of Caster
# SPDX-License-Identifier: MIT
"""Offline tests for the opt-in portable-build updater."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import shutil
import uuid
from pathlib import Path
import zipfile

import pytest

import caster_update as update


class _Response(io.BytesIO):
    def __init__(self, body: bytes, headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _release(version="v0.5.7", assets=None):
    return json.dumps({
        "tag_name": version,
        "draft": False,
        "prerelease": False,
        "assets": assets or [{
            "name": update.ASSET_NAME,
            "browser_download_url": "https://example.invalid/Caster-portable.zip",
            "size": 1234,
        }],
    }).encode()


def test_latest_update_accepts_only_newer_stable_portable_release(monkeypatch):
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        lambda *args, **kwargs: _Response(_release()))
    found = update.latest_update("0.5.6")
    assert found is not None
    assert found.tag == "v0.5.7"
    assert found.version == (0, 5, 7)


@pytest.mark.parametrize("payload", [
    {"tag_name": "v0.5.6", "draft": False, "prerelease": False, "assets": []},
    {"tag_name": "v0.5.7", "draft": True, "prerelease": False, "assets": []},
    {"tag_name": "not-a-version", "draft": False, "prerelease": False, "assets": []},
])
def test_latest_update_rejects_noninstallable_releases(monkeypatch, payload):
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        lambda *args, **kwargs: _Response(json.dumps(payload).encode()))
    assert update.latest_update("0.5.6") is None


def _archive(path, names):
    with zipfile.ZipFile(path, "w") as z:
        for name in names:
            z.writestr(name, b"placeholder")


def test_download_validates_the_portable_archive(tmp_path, monkeypatch):
    source = tmp_path / "source.zip"
    _archive(source, ["Caster.exe", "ffmpeg.exe"])
    body = source.read_bytes()
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        lambda *args, **kwargs: _Response(
                            body, {"Content-Length": str(len(body))}))
    result = update.download(
        update.Update((0, 5, 7), "v0.5.7", "https://example.invalid/a.zip", len(body)),
        str(tmp_path / "updates"))
    assert os.path.exists(result)
    with zipfile.ZipFile(result) as z:
        assert z.namelist() == ["Caster.exe", "ffmpeg.exe"]


def test_download_rejects_zip_slip_archive(tmp_path, monkeypatch):
    source = tmp_path / "unsafe.zip"
    _archive(source, ["Caster.exe", "../outside.txt"])
    body = source.read_bytes()
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        lambda *args, **kwargs: _Response(body))
    item = update.Update((0, 5, 7), "v0.5.7", "https://example.invalid/a.zip", len(body))
    with pytest.raises(RuntimeError, match="unsafe"):
        update.download(item, str(tmp_path / "updates"))


def test_installer_is_hidden_and_noninteractive(tmp_path, monkeypatch):
    archive = tmp_path / "Caster.zip"
    _archive(archive, ["Caster.exe"])
    calls = []
    monkeypatch.setattr(update.subprocess, "Popen",
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    update.launch_installer(str(archive), str(tmp_path / "app"), pid=1234)
    args, kwargs = calls[0]
    command = args[0]
    assert command[:4] == ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive"]
    assert "Hidden" in command
    assert kwargs["stdin"] is update.subprocess.DEVNULL
    script = command[-1]
    text = open(script, encoding="utf-8").read()
    assert "Copy-Item -LiteralPath $file.FullName" in text
    assert "Start-Process -FilePath" in text


def test_update_check_reports_network_failure(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("offline")
    monkeypatch.setattr(update.urllib.request, "urlopen", fail)
    with pytest.raises(RuntimeError, match="Could not check"):
        update.latest_update("0.5.8")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer integration")
@pytest.mark.parametrize("fail_restart", [False, True])
def test_real_powershell_installs_nested_payload_with_literal_paths(tmp_path, monkeypatch, fail_restart):
    # Spaces, Unicode, apostrophes, dollar signs and wildcard brackets are
    # valid portable-install paths. Exercise Windows PowerShell, not a mock
    # of its quoting/copy behavior. Only restarting the GUI is substituted.
    app = tmp_path / "Caster user's $cash [portable] café"
    (app / "_internal").mkdir(parents=True)
    (app / "Caster.exe").write_bytes(b"old executable")
    (app / "_internal" / "library.dll").write_bytes(b"old library")
    (app / "personal.txt").write_text("keep me")
    archive = tmp_path / "update [new].zip"
    _archive(archive, ["Caster.exe", "_internal/library.dll", "_internal/new/data.txt"])
    calls = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(update.subprocess, "Popen",
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    update.launch_installer(str(archive), str(app), pid=2147483647)
    monkeypatch.setattr(update.subprocess, "Popen", real_popen)
    command = calls[0][0][0]
    script = Path(command[-1])
    content = script.read_text(encoding="utf-8")
    # Exercise termination with two real, isolated processes. Never target
    # the user's Caster instances from automated tests.
    process_name = 'CasterTest' + uuid.uuid4().hex[:8]
    executable = tmp_path / (process_name + '.exe')
    shutil.copy2(Path(os.environ['SystemRoot']) / 'System32' / 'ping.exe', executable)
    children = [subprocess.Popen([str(executable), '-t', '127.0.0.1'],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                for _ in range(2)]
    content = content.replace("-Name 'Caster'", "-Name '" + process_name + "'")
    content = content.replace(
        "Start-Process -FilePath (Join-Path $appDir 'Caster.exe') -WorkingDirectory $appDir -WindowStyle Hidden",
        "throw 'Simulated restart failure'" if fail_restart else
        "Set-Content -LiteralPath (Join-Path $appDir 'restarted.txt') -Value 'yes'")
    script.write_text(content, encoding="utf-8")
    try:
        assert all(child.poll() is None for child in children)
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=40)
        assert all(child.poll() is not None for child in children)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
    log = (tmp_path / "install.log").read_text() if (tmp_path / "install.log").exists() else ""
    if fail_restart:
        assert result.returncode == 1
        assert "Simulated restart failure" in log
        assert (app / "Caster.exe").read_bytes() == b"old executable"
        assert (app / "_internal" / "library.dll").read_bytes() == b"old library"
        assert not (app / "_internal" / "new" / "data.txt").exists()
        assert (app / "personal.txt").read_text() == "keep me"
        assert archive.exists()
        return
    assert result.returncode == 0, (result.stderr, log)
    assert (app / "Caster.exe").read_bytes() == b"placeholder"
    assert (app / "_internal" / "library.dll").read_bytes() == b"placeholder"
    assert (app / "_internal" / "new" / "data.txt").read_bytes() == b"placeholder"
    assert (app / "personal.txt").read_text() == "keep me"
    assert (app / "restarted.txt").exists()
    assert not archive.exists()
    assert not script.parent.exists()
