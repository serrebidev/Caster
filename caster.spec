# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: portable onedir build of Caster.
# Build:  py -m PyInstaller caster.spec --noconfirm

import sys

block_cipher = None

a = Analysis(
    ["caster.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        "wx",
        "pyatv",
        "pychromecast",
        "zeroconf",
        "yt_dlp",
        "miniaudio",
        "aiohttp",
        "pyaudiowpatch",
        "caster_extras",
        "caster_config",
        "caster_devices",
        "caster_ui",
        "wx.adv",
        # Sonos; optional at runtime, but bundle it when it is installed.
        "soco",
        "soco.discovery",
        "soco.services",
        # pyatv protocols are loaded dynamically via entry points
        "pyatv.protocols.raop",
        "pyatv.protocols.airplay",
        "pyatv.protocols.companion",
        # yt-dlp plugin machinery
        "yt_dlp.extractor",
        # zeroconf backends
        "asyncio",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "PIL",
        "test",
        "unittest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Caster",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # GUI app: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="Caster",
)
