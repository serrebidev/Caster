"""One-command release for Caster.

Bumps the version (patch by default), commits all pending changes,
tags vX.Y.Z, builds the portable exe with PyInstaller, bundles
ffmpeg.exe, zips to dist/Caster-portable.zip, and commits the
version bump.

Usage:
    py release.py            # patch bump: 1.0.0 -> 1.0.1
    py release.py minor      # 1.0.0 -> 1.1.0
    py release.py major      # 1.0.0 -> 2.0.0
    py release.py --no-bump  # rebuild current version, no commit/tag
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCE_FILE = os.path.join(ROOT, "caster.py")
VERSION_RE = re.compile(r'APP_VERSION = "(\d+)\.(\d+)\.(\d+)"')


def run(cmd: list, **kw) -> str:
    print("$", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=ROOT, text=True,
                       capture_output=True, **kw)
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        raise SystemExit(f"command failed: {' '.join(cmd)}")
    return r.stdout


def read_version() -> tuple[int, int, int]:
    src = open(SOURCE_FILE, encoding="utf-8").read()
    m = VERSION_RE.search(src)
    if not m:
        raise SystemExit("APP_VERSION not found in caster.py")
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def write_version(ver: tuple[int, int, int]) -> None:
    src = open(SOURCE_FILE, encoding="utf-8").read()
    new = 'APP_VERSION = "{}.{}.{}"'.format(*ver)
    out = VERSION_RE.sub(new, src, count=1)
    if new not in out:
        raise SystemExit("failed to rewrite APP_VERSION")
    open(SOURCE_FILE, "w", encoding="utf-8", newline="").write(out)


def git_clean() -> bool:
    r = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                       text=True, capture_output=True)
    if r.returncode != 0:
        raise SystemExit("not a git repository (or git unavailable)")
    return r.stdout.strip() == ""


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "patch"
    if mode not in ("patch", "minor", "major", "--no-bump"):
        raise SystemExit(__doc__)

    if not git_clean() and mode != "--no-bump":
        raise SystemExit(
            "working tree has uncommitted changes; commit or stash first "
            "(release commits the version bump, not your WIP)")

    old = read_version()
    if mode == "--no-bump":
        ver = old
        print(f"rebuilding v{ver[0]}.{ver[1]}.{ver[2]} (no bump)")
    else:
        idx = {"patch": 2, "minor": 1, "major": 0}[mode]
        ver = list(old)
        ver[idx] += 1
        for i in range(idx + 1, 3):
            ver[i] = 0
        ver = tuple(ver)
        print(f"version bump: {old[0]}.{old[1]}.{old[2]} -> "
              f"{ver[0]}.{ver[1]}.{ver[2]}")
        write_version(ver)
        run(["git", "add", SOURCE_FILE])

    vtag = f"v{ver[0]}.{ver[1]}.{ver[2]}"

    # Tag before building so the build embeds/reflects the tagged state.
    if mode != "--no-bump":
        run(["git", "commit", "-m", f"Release {vtag}"])
        run(["git", "tag", "-f", vtag])

    # Build.
    run([sys.executable, "-m", "PyInstaller", "caster.spec",
         "--noconfirm", "--distpath", "dist"])

    # Bundle ffmpeg next to the exe.
    ff = subprocess.run(["where", "ffmpeg"], cwd=ROOT, text=True,
                        capture_output=True).stdout.splitlines()
    if not ff:
        raise SystemExit("ffmpeg.exe not found on PATH")
    run(["cp", ff[0], os.path.join("dist", "Caster", "ffmpeg.exe")])

    # Zip the portable folder.
    zp = os.path.join(ROOT, "dist", "Caster-portable.zip")
    if os.path.exists(zp):
        os.remove(zp)
    count = 0
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        base = os.path.join(ROOT, "dist", "Caster")
        for root, _, files in os.walk(base):
            for fn in files:
                p = os.path.join(root, fn)
                z.write(p, os.path.relpath(p, base))
                count += 1
    print(f"{vtag}: {count} files -> dist/Caster-portable.zip "
          f"({os.path.getsize(zp) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
