# Copyright (c) serrebidev and contributors
# This file is part of Caster
# SPDX-License-Identifier: MIT
"""Offline tests for the opt-in portable-build updater."""
from __future__ import annotations

import io
import json
import os
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
    assert "Get-ChildItem -LiteralPath $stage | Copy-Item" in text
    assert "Start-Process -FilePath" in text
