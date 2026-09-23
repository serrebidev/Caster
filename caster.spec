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
        # Developer tools installed beside the runtime deps. Nothing imports
        # them at run time, but PyInstaller's hooks follow them in: jedi alone
        # added 5,536 files and 10 MB to the v0.5.23 zip.
        "jedi",
        "parso",
        "mypy",
        "black",
        "IPython",
        "pytest",
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

# macOS: app bundle (build.sh zips it). ffmpeg comes from the system (Homebrew).
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Caster.app",
        bundle_identifier="com.serrebidev.caster",
        info_plist={
            "NSHighResolutionCapable": True,
            "NSLocalNetworkUsageDescription": "Caster finds and casts to devices on your network.",
            "NSBonjourServices": ["_googlecast._tcp", "_airplay._tcp", "_raop._tcp"],
        },
    )
