#!/bin/bash
# Build the Linux release for one tag on the Linux release host, the way
# cloud-release.yml's ubuntu-24.04 job does, in a throwaway ubuntu:24.04
# container. tools/release_other_platforms.py pipes this over SSH:
#
#   ssh root@serrebiradio.com bash -s -- vX.Y.Z < tools/build_linux_remote.sh
#
# The last line of output is the release asset's path on the host.
set -euo pipefail

tag="$1"
work="$(mktemp -d /tmp/caster-linux-XXXXXX)"
GIT_LFS_SKIP_SMUDGE=1 git clone --quiet --depth 1 --branch "$tag" https://github.com/serrebidev/Caster.git "$work/src" >&2

# Current wxPython has no Linux pip wheel; provide its source-build libraries.
docker run --rm -v "$work/src:/src" -w /src ubuntu:24.04 bash -c '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends python3-venv python3-pip python3-wxgtk4.0 libpython3.12 git binutils xvfb xauth ffmpeg >/dev/null
  apt-get install -y -qq --no-install-recommends build-essential python3-dev libgtk-3-dev libwebkit2gtk-4.1-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libnotify-dev libsdl2-dev libjpeg-dev libpng-dev libtiff-dev libsm-dev libxtst-dev >/dev/null
  python3 -m venv --system-site-packages /venv
  PYTHON=/venv/bin/python ./build.sh
  set +e
  HOME=/tmp timeout 15 xvfb-run -a dist/Caster/Caster
  rc=$?
  set -e
  [ $rc = 124 ] || { echo "Caster exited before 15 s (rc $rc)"; exit 1; }
' >&2

out="$work/Caster-linux-x86_64.tar.gz"
mv "$work/src/dist/Caster-linux-x86_64.tar.gz" "$out"
echo "$out"
