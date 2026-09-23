#!/usr/bin/env bash
# macOS/Linux build. Windows uses release.py. Screen, window and system-audio
# capture are Windows-only; ffmpeg must be installed on the system.
#   macOS: dist/Caster-macos.zip (Caster.app)
#   Linux: dist/Caster-linux-x86_64.tar.gz (Caster/ folder)
set -euo pipefail
cd "$(dirname "$0")"
PYTHON=${PYTHON:-python3}
"$PYTHON" -m pip install pyinstaller -r requirements.txt
"$PYTHON" -m PyInstaller caster.spec --noconfirm --clean
case "$(uname -s)" in
  Darwin)
    codesign --force --deep --sign - dist/Caster.app
    (cd dist && ditto -c -k --sequesterRsrc --keepParent Caster.app Caster-macos.zip)
    ;;
  Linux)
    tar -C dist -czf dist/Caster-linux-x86_64.tar.gz Caster
    ;;
  *) echo "build.sh is for macOS and Linux; use release.py on Windows." >&2; exit 1 ;;
esac
ls -l dist
