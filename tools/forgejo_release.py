"""Forgejo releases for Caster.

Replaces the `gh release ...` steps from when releases lived on GitHub.
Token comes from the FORGEJO_TOKEN environment variable; nothing here reads
or stores the token file.

    python tools/forgejo_release.py create vX.Y.Z --title "Caster vX.Y.Z" --notes-file notes.md --asset a.exe --asset a.zip --asset sums.txt
    python tools/forgejo_release.py upload vX.Y.Z file.exe
    python tools/forgejo_release.py delete-drafts --keep vX.Y.Z
"""
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HOST = os.environ.get("FORGEJO_HOST", "git.serrebiradio.com")
REPO = os.environ.get("FORGEJO_REPO", "serrebi/Caster")
USER_AGENT = "caster-release/1.0"


def _token():
    token = (os.environ.get("FORGEJO_TOKEN") or "").strip()
    if not token:
        sys.exit("FORGEJO_TOKEN is not set.")
    return token


def _api(path, method="GET", body=None, headers=None):
    req = urllib.request.Request(
        f"https://{HOST}/api/v1/repos/{REPO}{path}",
        method=method,
        headers={
            "Authorization": f"token {_token()}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            **(headers or {}),
        },
    )
    # BOM-less UTF-8: plain json.dumps bytes, never utf-8-sig.
    data = None if body is None else json.dumps(body).encode("utf-8")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        sys.exit(f"Forgejo {method} {path} failed ({exc.code}): {detail}")


def _get_release_by_tag(tag):
    status, release = _api(f"/releases/tags/{urllib.parse.quote(tag)}")
    if status != 200 or not release:
        sys.exit(f"No Forgejo release found for {tag}.")
    return release


def cmd_create(tag, title, notes_file, assets):
    with open(notes_file, "r", encoding="utf-8") as handle:
        notes = handle.read()
    _api(
        "/releases",
        method="POST",
        body={
            "tag_name": tag,
            "name": title,
            "body": notes,
            "draft": False,
            "prerelease": False,
        },
    )
    release = _get_release_by_tag(tag)
    if release.get("draft"):
        sys.exit(f"{tag} is still a draft at {release.get('html_url')}.")
    print(f"{tag} is published: {release.get('html_url')}", flush=True)
    for asset in assets:
        cmd_upload(tag, asset, release_id=release["id"])


def cmd_upload(tag, path, release_id=None):
    if release_id is None:
        release_id = _get_release_by_tag(tag)["id"]
    filename = os.path.basename(path)
    with open(path, "rb") as handle:
        payload = handle.read()
    boundary = "------------------------tonegenforgejo"
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="attachment"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {ctype}\r\n\r\n".encode(),
        payload,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    data = b"".join(parts)
    req = urllib.request.Request(
        f"https://{HOST}/api/v1/repos/{REPO}/releases/{release_id}/assets"
        f"?name={urllib.parse.quote(filename)}",
        method="POST",
        headers={
            "Authorization": f"token {_token()}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, data=data, timeout=600) as resp:
            uploaded = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        sys.exit(f"Asset upload failed ({exc.code}): {detail}")
    print(f"Uploaded {uploaded.get('name')} ({uploaded.get('size')} bytes).", flush=True)


def cmd_delete_drafts(keep):
    _, releases = _api("/releases?limit=100")
    for release in releases or []:
        if release.get("draft") and release.get("tag_name") != keep:
            tag = release.get("tag_name")
            print(f"Deleting draft release {tag}...", flush=True)
            _api(f"/releases/{release['id']}", method="DELETE")


def main(argv):
    if len(argv) < 2 or argv[1] not in ("create", "upload", "delete-drafts"):
        sys.exit("usage: forgejo_release.py create TAG --title T --notes-file N --asset F... | upload TAG FILE | delete-drafts --keep TAG")
    if argv[1] == "create":
        tag, title, notes_file, assets = argv[2], "", "", []
        args = argv[3:]
        while args:
            flag, args = args[0], args[1:]
            if flag == "--title":
                title, args = args[0], args[1:]
            elif flag == "--notes-file":
                notes_file, args = args[0], args[1:]
            elif flag == "--asset":
                assets.append(args[0])
                args = args[1:]
            else:
                sys.exit(f"Unknown flag: {flag}")
        if not title or not notes_file or not assets:
            sys.exit("create needs --title, --notes-file, and at least one --asset.")
        cmd_create(tag, title, notes_file, assets)
    elif argv[1] == "upload":
        cmd_upload(argv[2], argv[3])
    else:
        keep = ""
        args = argv[2:]
        while args:
            flag, args = args[0], args[1:]
            if flag == "--keep":
                keep, args = args[0], args[1:]
            else:
                sys.exit(f"Unknown flag: {flag}")
        if not keep:
            sys.exit("delete-drafts needs --keep TAG.")
        cmd_delete_drafts(keep)


if __name__ == "__main__":
    main(sys.argv)
