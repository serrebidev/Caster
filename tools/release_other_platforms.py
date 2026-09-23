"""Linux and macOS half of a release from the Windows release host.

Run once the Windows release is published: dispatches macos-release.yml on the
tag, builds Linux over SSH with tools/build_linux_remote.sh, uploads it, and
waits for the macOS asset. cloud-release.yml (Muse and cloud agents) has its
own jobs for both, so this does nothing on GitHub Actions.

    python tools/release_other_platforms.py vX.Y.Z
"""
import json
import os
import posixpath
import subprocess
import sys
import time

HOST = os.environ.get("LINUX_BUILD_HOST", "root@serrebiradio.com")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(*cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, check=True, **kw)


def main(tag):
    if os.environ.get("GITHUB_ACTIONS"):
        return
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30))
    run("gh", "workflow", "run", "macos-release.yml", "-f", f"release_tag={tag}")

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
    run("gh", "release", "upload", tag, local, "--clobber")

    run_id = ""
    for _ in range(12):
        runs = json.loads(run("gh", "run", "list", "--workflow", "macos-release.yml",
                              "--event", "workflow_dispatch", "--limit", "5",
                              "--json", "databaseId,createdAt",
                              stdout=subprocess.PIPE, text=True).stdout or "[]")
        run_id = next((str(r["databaseId"]) for r in runs if r["createdAt"] >= started), "")
        if run_id:
            break
        time.sleep(10)
    if not run_id:
        sys.exit(f"macos-release.yml never started for {tag}; run: gh workflow run macos-release.yml -f release_tag={tag}")
    print(f"Waiting for the macOS build (run {run_id})...", flush=True)
    run("gh", "run", "watch", run_id, "--exit-status", "--interval", "30")
    print(f"{tag}: Linux and macOS assets attached.")


if __name__ == "__main__":
    main(sys.argv[1])
