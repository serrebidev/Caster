"""Linux and macOS half of a release from the Windows release host.

Run once the Windows release is published on Forgejo: dispatches
macos-release.yml on the Forgejo Mac runner, builds Linux over SSH with
tools/build_linux_remote.sh, uploads it, and waits for the macOS asset.
cloud-release.yml (Muse and cloud agents) has its own jobs for both, so this
does nothing on Actions.

    python tools/release_other_platforms.py vX.Y.Z
"""
import os
import posixpath
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import forgejo_release  # noqa: E402

HOST = os.environ.get("LINUX_BUILD_HOST", "wsl-build")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAC_ASSET = "Caster-macos.zip"


def run(*cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, check=True, **kw)


def main(tag):
    if os.environ.get("GITHUB_ACTIONS"):
        return
    forgejo_release._api("/actions/workflows/macos-release.yml/dispatches", method="POST",
                         body={"ref": "master", "inputs": {"release_tag": tag}})

    print(f"Building Linux for {tag} on {HOST}...", flush=True)
    with open(os.path.join(ROOT, "tools", "build_linux_remote.sh"), "rb") as script:
        out = run("ssh", "-o", "BatchMode=yes", HOST, "bash", "-s", "--", tag,
                  stdin=script, stdout=subprocess.PIPE).stdout
    remote = out.decode().strip().splitlines()[-1]
    local = os.path.join(ROOT, "dist", posixpath.basename(remote))
    os.makedirs(os.path.dirname(local), exist_ok=True)
    run("scp", "-o", "BatchMode=yes", f"{HOST}:{remote}", local)
    if remote.startswith("/tmp/"):
        run("ssh", "-o", "BatchMode=yes", HOST, "rm -rf -- " + posixpath.dirname(remote))
    run(sys.executable, os.path.join("tools", "forgejo_release.py"), "upload", tag, local)

    print(f"Waiting for the macOS asset ({MAC_ASSET})...", flush=True)
    deadline = time.time() + 60 * 60
    while time.time() < deadline:
        release = forgejo_release._get_release_by_tag(tag)
        if any(a.get("name") == MAC_ASSET for a in release.get("assets") or []):
            print(f"{tag}: Linux and macOS assets attached.")
            return
        time.sleep(30)
    sys.exit(f"{tag}: macOS asset never arrived; check macos-release.yml on Forgejo and "
             f"redispatch with release_tag={tag}.")


if __name__ == "__main__":
    main(sys.argv[1])
