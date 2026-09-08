# Copyright (c) serrebidev and contributors
# This file is part of Caster
# SPDX-License-Identifier: MIT
"""GitHub-release update support for the portable Windows build.

Checking is deliberately manual.  A portable app must never download a large
archive or replace itself without the person using it asking first.  When an
update is accepted, a short-lived, hidden PowerShell helper waits for Caster
to exit, expands the verified release archive beside the running executable,
and starts the replacement.
"""

from __future__ import annotations

import json
import base64
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Optional

REPOSITORY = "serrebidev/Caster"
LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
ASSET_NAME = "Caster-portable.zip"
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024  # A release must never fill the disk.
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 10_000
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class Update:
    version: tuple[int, int, int]
    tag: str
    url: str
    size: int


def parse_version(value: str) -> Optional[tuple[int, int, int]]:
    """A three-part release version, or None for an unrelated Git tag."""
    match = VERSION_RE.fullmatch((value or "").strip())
    return tuple(map(int, match.groups())) if match else None


def latest_update(current: str, timeout: float = 8.0) -> Optional[Update]:
    """Return a newer stable portable release, without downloading it."""
    installed = parse_version(current)
    if installed is None:
        return None
    request = urllib.request.Request(
        LATEST_URL,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "Caster-update-check"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise RuntimeError("Could not check for updates. Check your internet connection and try again.") from exc
    if payload.get("draft") or payload.get("prerelease"):
        return None
    tag = str(payload.get("tag_name") or "")
    version = parse_version(tag)
    if version is None or version <= installed:
        return None
    for asset in payload.get("assets", []):
        if asset.get("name") != ASSET_NAME:
            continue
        url = str(asset.get("browser_download_url") or "")
        size = asset.get("size")
        if (not url.startswith("https://") or not isinstance(size, int)
                or size <= 0 or size > MAX_ARCHIVE_BYTES):
            return None
        return Update(version, tag, url, size)
    return None


def download(update: Update, destination_dir: str = "",
             timeout: float = 30.0) -> str:
    """Download and validate an update archive, returning its local path."""
    target_dir = destination_dir or os.path.join(
        os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "Caster", "updates")
    os.makedirs(target_dir, exist_ok=True)
    final = os.path.join(target_dir, f"Caster-{update.tag}.zip")
    partial = final + ".part"
    request = urllib.request.Request(
        update.url, headers={"User-Agent": "Caster-updater"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_ARCHIVE_BYTES:
                raise RuntimeError("update archive is too large")
            written = 0
            with open(partial, "wb") as handle:
                while chunk := response.read(1024 * 1024):
                    written += len(chunk)
                    if written > MAX_ARCHIVE_BYTES:
                        raise RuntimeError("update archive is too large")
                    handle.write(chunk)
        if written != update.size:
            raise RuntimeError("update download size does not match the release")
        _validate_archive(partial)
        os.replace(partial, final)
        return final
    except BaseException:
        try:
            os.unlink(partial)
        except OSError:
            pass
        raise


def _validate_archive(path: str) -> None:
    """Reject zip-slip archives and releases without the portable executable."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) > MAX_ARCHIVE_FILES:
            raise RuntimeError("update archive contains too many files")
        if "Caster.exe" not in names:
            raise RuntimeError("update archive does not contain Caster.exe")
        if sum(info.file_size for info in archive.infolist()) > MAX_EXTRACTED_BYTES:
            raise RuntimeError("update archive expands to too much data")
        for name in names:
            normalized = name.replace("\\", "/")
            if (normalized.startswith("/") or ".." in normalized.split("/")
                    or ":" in normalized.split("/")[0]):
                raise RuntimeError("update archive has an unsafe file path")
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"update archive is corrupt: {bad}")


def running_app_dir() -> str:
    """Directory to replace; source runs intentionally do not self-update."""
    if not getattr(sys, "frozen", False):
        return ""
    return os.path.dirname(os.path.abspath(sys.executable))


def launch_installer(archive: str, app_dir: str = "", pid: int = 0) -> None:
    """Start an invisible helper that updates after this process exits."""
    app_dir = app_dir or running_app_dir()
    if not app_dir:
        raise RuntimeError("updates can only be installed from Caster.exe")
    _validate_archive(archive)
    pid = pid or os.getpid()
    helper_dir = tempfile.mkdtemp(prefix="caster_update_")
    script = os.path.join(helper_dir, "install.ps1")
    values = {"pid": pid, "archive": os.path.abspath(archive),
              "app_dir": os.path.abspath(app_dir), "helper_dir": helper_dir}
    # Decode data, never interpolate filesystem paths as PowerShell syntax.
    # JSON's backslash escapes are NOT PowerShell escapes; double quotes also
    # expand dollar signs and backticks in otherwise valid Windows paths.
    encoded = base64.b64encode(json.dumps(values).encode("utf-8")).decode("ascii")
    content = """$ErrorActionPreference = 'Stop'
$config = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')) | ConvertFrom-Json
$pidToWait = $config.pid
$archive = $config.archive
$appDir = $config.app_dir
$helperDir = $config.helper_dir
$log = Join-Path (Split-Path -LiteralPath $archive) 'install.log'
$changed = [Collections.Generic.List[object]]::new()
$backupRoot = Join-Path $helperDir 'backup'
try {{
Add-Content -LiteralPath $log -Value 'Starting Caster update.'
while (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue) {{
    Start-Sleep -Milliseconds 250
}}
$stage = Join-Path $helperDir 'payload'
Expand-Archive -LiteralPath $archive -DestinationPath $stage -Force
if (-not (Test-Path -LiteralPath (Join-Path $stage 'Caster.exe'))) {{
    throw 'Update archive does not contain Caster.exe.'
}}
# Copy explicit literal filenames, including hidden files, into the matching
# relative destinations. Pipeline FileInfo paths can be wildcard-expanded and
# directory-copy semantics can produce nested _internal directories.
foreach ($file in Get-ChildItem -LiteralPath $stage -Recurse -File -Force) {{
    $relative = $file.FullName.Substring($stage.Length).TrimStart([char]92)
    $destination = Join-Path $appDir $relative
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
    $backup = $null
    if (Test-Path -LiteralPath $destination) {{
        $backup = Join-Path $backupRoot $relative
        [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($backup)) | Out-Null
        Copy-Item -LiteralPath $destination -Destination $backup -Force
    }}
    $changed.Add(@{{destination=$destination; backup=$backup}})
    for ($attempt = 0; ; $attempt++) {{
        try {{
            Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
            break
        }} catch {{
            if ($attempt -ge 29) {{ throw }}
            Start-Sleep -Milliseconds 500
        }}
    }}
}}
Start-Process -FilePath (Join-Path $appDir 'Caster.exe') -WorkingDirectory $appDir -WindowStyle Hidden
Add-Content -LiteralPath $log -Value 'Caster update installed and restarted.'
Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $helperDir -Recurse -Force -ErrorAction SilentlyContinue
}} catch {{
    Add-Content -LiteralPath $log -Value ($_ | Out-String)
    # Restore every touched file if copying or launching fails, so the old
    # portable app remains usable. Keep the archive and backups for recovery.
    foreach ($entry in $changed) {{
        try {{
            if ($entry.backup) {{
                Copy-Item -LiteralPath $entry.backup -Destination $entry.destination -Force
            }} else {{
                Remove-Item -LiteralPath $entry.destination -Force -ErrorAction SilentlyContinue
            }}
        }} catch {{ Add-Content -LiteralPath $log -Value ($_ | Out-String) }}
    }}
    exit 1
}}
""".format(encoded=encoded)
    with open(script, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
         "-File", script],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=flags,
    )


def discard(archive: str) -> None:
    """Remove a downloaded archive if the user cancels installation."""
    try:
        os.unlink(archive)
    except OSError:
        pass
