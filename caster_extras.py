"""Caster extras: UPnP/DLNA renderer control and screen/app-window
capture sources. Kept free of caster imports to avoid a cycle;
caster imports THIS module."""

from __future__ import annotations

import functools
import http.server
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request as _urlreq


def _find_ffmpeg() -> str:
    if getattr(__import__("sys"), "frozen", False):
        bundled = os.path.join(
            os.path.dirname(__import__("sys").executable), "ffmpeg.exe")
        if os.path.exists(bundled):
            return bundled
    found = shutil.which("ffmpeg")
    if found:
        return found
    import glob
    for pattern in (
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\*FFmpeg*\**\bin\ffmpeg.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\*ffmpeg*\**\bin\ffmpeg.exe"),
    ):
        for hit in glob.glob(pattern, recursive=True):
            return hit
    raise FileNotFoundError("ffmpeg.exe not found")


def _no_window_kwargs() -> dict:
    import sys
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        return {"startupinfo": si,
                "creationflags": (subprocess.CREATE_NO_WINDOW
                                  | subprocess.CREATE_BREAKAWAY_FROM_JOB)}
    return {}


# Mirrored minimal HLS server (same wire format as caster.HlsRelay):
# CORS headers, correct MIME types, HTTP/1.1 keep-alive.

class ScreenHlsHandler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    _TYPES = {
        ".m3u8": "application/vnd.apple.mpegurl",
        ".ts": "video/mp2t",
        ".m4s": "video/iso.segment",
    }

    def guess_type(self, path):
        import os as _os
        return self._TYPES.get(_os.path.splitext(path)[1].lower(),
                               "application/octet-stream")

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format, *args):
        pass


# ---------------------------------------------------------------------------
# UPnP / DLNA renderer support (SSDP discovery + AVTransport control)
# ---------------------------------------------------------------------------

def upnp_discover(timeout: int = 6) -> list:
    """SSDP M-SEARCH for AVTransport media renderers.

    Returns [(friendly_name, control_url), ...].
    """
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 3\r\n"
        "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
        "\r\n"
    ).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout)
    try:
        sock.sendto(msg, ("239.255.255.250", 1900))
        seen = set()
        hits = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(65536)
            except socket.timeout:
                break
            text = data.decode("utf-8", "replace")
            loc = None
            for line in text.splitlines():
                if line.lower().startswith("location:"):
                    loc = line.split(":", 1)[1].strip()
                    break
            if loc and loc not in seen:
                seen.add(loc)
                dev = _upnp_fetch_control(loc)
                if dev:
                    hits.append(dev)
    finally:
        sock.close()
    return hits


def _upnp_fetch_control(location: str):
    """Fetch a UPnP device description XML and return its AVTransport
    control URL plus friendly name."""
    try:
        with _urlreq.urlopen(location, timeout=5) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    import html as _html
    name_m = re.search(r"<friendlyName>([^<]+)</friendlyName>", body)
    name = _html.unescape(name_m.group(1).strip()) if name_m else location
    for svc_m in re.finditer(r"<service>(.*?)</service>", body, re.S):
        svc = svc_m.group(1)
        if "AVTransport" not in svc:
            continue
        ctl = re.search(r"<controlURL>([^<]+)</controlURL>", svc)
        if not ctl:
            continue
        # Resolve per UPnP spec: relative to the description URL (handles
        # both root-absolute "/x" and relative "x" controlURLs).
        import urllib.parse as _up
        url = _up.urljoin(location, ctl.group(1))
        return (name, url)
    return None


def _soap(control_url: str, action: str, inner: str) -> None:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>" + inner + "</s:Body></s:Envelope>"
    )
    req = _urlreq.Request(
        control_url, data=body.encode("utf-8"), method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
        })
    with _urlreq.urlopen(req, timeout=10) as r:
        r.read()


def upnp_play(control_url: str, media_url: str, title: str = "Caster",
              mime: str = "", upnp_class: str = "object.item.videoItem") -> None:
    """Send an AVTransport SetAVTransportURI + Play SOAP pair."""
    escaped = (media_url.replace("&", "&amp;")
               .replace("<", "&lt;").replace(">", "&gt;"))
    t = title.replace("&", "&amp;").replace("<", "&lt;")
    proto = mime or "*"
    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="" parentID="0" restricted="1">'
        f"<dc:title>{t}</dc:title>"
        f"<upnp:class>{upnp_class}</upnp:class>"
        f"<res protocolInfo=\"http-get:*:{proto}:DLNA.ORG_OP=01;DLNA.ORG_CI=0\">{escaped}</res></item></DIDL-Lite>"
    )
    didl_escaped = (didl.replace("&", "&amp;")
                    .replace("<", "&lt;").replace(">", "&gt;"))
    uri_args = (
        "<u:SetAVTransportURI "
        'xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        "<InstanceID>0</InstanceID>"
        f"<CurrentURI>{escaped}</CurrentURI>"
        f"<CurrentURIMetaData>{didl_escaped}</CurrentURIMetaData>"
        "</u:SetAVTransportURI>"
    )
    _soap(control_url, "SetAVTransportURI", uri_args)
    play_args = (
        "<u:Play xmlns:u=\"urn:schemas-upnp-org:service:AVTransport:1\">"
        "<InstanceID>0</InstanceID><Speed>1</Speed></u:Play>"
    )
    _soap(control_url, "Play", play_args)


def upnp_state(control_url: str) -> str:
    """Current TransportState (STOPPED/PLAYING/...)."""
    body = (
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        '<u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        "<InstanceID>0</InstanceID></u:GetTransportInfo>"
        "</s:Body></s:Envelope>"
    )
    req = _urlreq.Request(
        control_url, data=body.encode("utf-8"), method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#GetTransportInfo"',
        })
    with _urlreq.urlopen(req, timeout=10) as r:
        m = re.search(r"<CurrentTransportState>([^<]+)<", r.read().decode("utf-8", "replace"))
    return m.group(1) if m else "UNKNOWN"


def upnp_host(control_url: str) -> str:
    """Host part of a control URL (for MusicCast side-channel calls)."""
    import urllib.parse as _up
    return (_up.urlsplit(control_url).hostname or "")


def yxc_available(host: str) -> bool:
    """True if the device speaks MusicCast (YamahaExtendedControl)."""
    try:
        with _urlreq.urlopen(
                f"http://{host}/YamahaExtendedControl/v1/main/getStatus",
                timeout=4) as r:
            return b"response_code" in r.read(400)
    except Exception:
        return False


def yxc_set_input(host: str, yxc_input: str = "server") -> bool:
    """Switch a MusicCast device's input (upnp push needs input=server)."""
    try:
        with _urlreq.urlopen(
                f"http://{host}/YamahaExtendedControl/v1/main/setInput?input={yxc_input}",
                timeout=4) as r:
            return b'"response_code":0' in r.read(200)
    except Exception:
        return False


def upnp_stop(control_url: str) -> None:
    stop_args = (
        "<u:Stop xmlns:u=\"urn:schemas-upnp-org:service:AVTransport:1\">"
        "<InstanceID>0</InstanceID></u:Stop>"
    )
    try:
        _soap(control_url, "Stop", stop_args)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Local file server: serves one file with HTTP Range support so receivers
# can play/seek a local media file directly (bit-exact, no transcode).
# ---------------------------------------------------------------------------

class FileServer:
    """Serve a single local file over HTTP with Range support.

    The receiver fetches the file itself and decodes it natively, so audio
    stays bit-exact and seeking works. Nothing runs when stopped.
    """

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        if not os.path.isfile(self.path):
            raise FileNotFoundError(self.path)
        import mimetypes
        self.mime = mimetypes.guess_type(self.path)[0] or "application/octet-stream"
        self._name = os.path.basename(self.path)
        self.httpd = None
        self.port = 0

    def start(self) -> str:
        fs = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self._serve(include_body=True)

            def do_HEAD(self):
                self._serve(include_body=False)

            def _serve(self, include_body: bool) -> None:
                size = os.path.getsize(fs.path)
                start, end = 0, size - 1
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    try:
                        spec = rng[6:].split(",")[0].strip()
                        a, _, b = spec.partition("-")
                        if a:
                            start = int(a)
                            end = int(b) if b else size - 1
                        elif b:  # suffix range: last N bytes
                            start = max(0, size - int(b))
                        end = min(end, size - 1)
                    except ValueError:
                        pass
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                length = end - start + 1
                self.send_response(206 if rng else 200)
                self.send_header("Content-Type", fs.mime)
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if rng:
                    self.send_header("Content-Range",
                                     f"bytes {start}-{end}/{size}")
                self.end_headers()
                if not include_body:
                    return
                with open(fs.path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        try:
                            self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            return
                        remaining -= len(chunk)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True,
                         name="caster-file").start()
        return f"http://{_lan_ip()}:{self.port}/{urllib.parse.quote(self._name)}"

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


# ---------------------------------------------------------------------------
# Screen / app-window casting: ddagrab (desktop duplication) + WASAPI loopback
# piped into ffmpeg for HLS output served to the chosen receiver.
# ---------------------------------------------------------------------------

class ScreenSource:
    """Captures this PC's screen (or one app window) plus system audio and
    serves it as HLS via the same HlsRelay infrastructure.

    Video: ddagrab captures the desktop; a window is isolated by cropping
    to the window rectangle. Hardware H.264 encodes it.
    Audio: WASAPI loopback (pyaudiowpatch) written to a temp WAV that
    ffmpeg mixes in.
    """

    def __init__(self, hwnd: int = 0) -> None:
        self.hwnd = hwnd
        self._proc = None
        self._wav_path = None
        self._wav_stop = None
        self.relay = None
        self.hls_url = ""
        self.rect = None
        if hwnd:
            self.rect = _window_rect(hwnd)

    def start(self) -> None:
        # 1. System-audio loopback -> temp WAV file.
        self._wav_path = os.path.join(
            tempfile.mkdtemp(prefix="caster_scr_"), "loop.wav")
        self._wav_stop = threading.Event()
        threading.Thread(target=self._capture_audio, daemon=True).start()
        time.sleep(1.0)  # let the WAV get some data
        # 2. HLS server (self-contained: no trailing-edge trimming needed
        # for a live capture — the encoder IS the live edge).
        self.root = os.path.dirname(self._wav_path)
        handler = functools.partial(ScreenHlsHandler, directory=self.root)
        self.httpd = http.server.ThreadingHTTPServer(("0.0.0.0", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        m3u8 = os.path.join(self.root, "live.m3u8")
        cmd = self._ffmpeg_cmd(m3u8)
        err_path = os.path.join(self.root, "ffmpeg_err.txt")
        err_fh = open(err_path, "wb")
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=err_fh,
            stdin=subprocess.DEVNULL, **_no_window_kwargs())
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if os.path.exists(m3u8) and sum(
                    1 for f in os.listdir(self.root)
                    if f.endswith(".ts")) >= 3:
                break
            if self._proc.poll() is not None:
                err_fh.close()
                tail = open(err_path, "rb").read().decode("utf-8", "replace")[-800:]
                raise RuntimeError("screen capture ffmpeg exited early: " + tail)
            time.sleep(0.25)
        err_fh.close()
        self.hls_url = f"http://{_lan_ip()}:{self.port}/live.m3u8"

    def _ffmpeg_cmd(self, m3u8: str) -> list:
        # Video: gdigrab. ddagrab (Desktop Duplication) hangs on some
        # driver/GPU combos — gdigrab is slower (~15fps) but universally
        # works. A window is captured natively by title.
        from caster import pick_h264_encoder  # deferred: no cycle
        if self.hwnd:
            title = _window_title(self.hwnd)
            vid_in = ["-f", "gdigrab", "-framerate", "15",
                      "-i", f"title={title}"]
        else:
            vid_in = ["-f", "gdigrab", "-framerate", "15",
                      "-i", "desktop"]
        return [
            _find_ffmpeg(), "-hide_banner", "-loglevel", "error",
            *vid_in,
            "-f", "wav", "-i", self._wav_path,
            "-c:v", pick_h264_encoder(), "-b:v", "4000k",
            # HLS needs a keyframe per segment (~1s): nvenc's default GOP
            # (250 frames) would keep the muxer waiting forever.
            "-g", "15", "-keyint_min", "15",
            "-force_key_frames", "expr:gte(t,n_forced*1)",
            "-c:a", "aac", "-b:a", "128k",
            "-f", "hls",
            "-hls_time", "1",
            "-hls_list_size", "10",
            "-hls_flags", "delete_segments",
            "-hls_segment_filename", os.path.join(self.root, "seg%05d.ts"),
            m3u8,
        ]

    def _capture_audio(self) -> None:
        """Write WASAPI loopback PCM to a WAV file until stopped."""
        try:
            import pyaudiowpatch as pw
            import wave as wavemod
            with pw.PyAudio() as pa:
                wasapi = pa.get_host_api_info_by_type(pw.paWASAPI)
                spk = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
                if not spk.get("isLoopbackDevice"):
                    for lb in pa.get_loopback_device_info_generator():
                        if spk["name"] in lb["name"]:
                            spk = lb
                            break
                ch = int(spk["maxInputChannels"]) or 2
                rate = int(spk["defaultSampleRate"])
                frames = int(rate * 0.2)
                stream = pa.open(
                    format=pw.paInt16, channels=ch, rate=rate, input=True,
                    input_device_index=spk["index"],
                    frames_per_buffer=frames)
                wf = wavemod.open(self._wav_path, "wb")
                wf.setnchannels(ch)
                wf.setsampwidth(2)
                wf.setframerate(rate)
                while not self._wav_stop.is_set():
                    wf.writeframes(
                        stream.read(frames, exception_on_overflow=False))
                wf.close()
                stream.close()
        except Exception:
            traceback.print_exc()

    def stop(self) -> None:
        if self._wav_stop:
            self._wav_stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        self._proc = None
        if getattr(self, "httpd", None):
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        import shutil
        if getattr(self, "root", None):
            shutil.rmtree(self.root, ignore_errors=True)
            self.root = None
        self.hls_url = ""


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Window enumeration
# ---------------------------------------------------------------------------

def _window_title(hwnd: int) -> str:
    import ctypes
    n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _window_rect(hwnd: int):
    """GetWindowRect for a top-level hwnd -> (l, t, r, b) in pixels."""
    import ctypes
    from ctypes import wintypes
    r = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def list_windows() -> list:
    """Visible top-level windows with titles: [(hwnd, title), ...]."""
    import ctypes
    from ctypes import wintypes
    result = []
    user32 = ctypes.windll.user32
    IsWindowVisible = user32.IsWindowVisible
    GetWindowTextW = user32.GetWindowTextW
    GetWindowTextLengthW = user32.GetWindowTextLengthW
    GetWindowRect = user32.GetWindowRect

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, lparam):
        if IsWindowVisible(hwnd):
            n = GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                GetWindowTextW(hwnd, buf, n + 1)
                title = buf.value.strip()
                r = wintypes.RECT()
                GetWindowRect(hwnd, ctypes.byref(r))
                if title and (r.right - r.left) > 50 and (r.bottom - r.top) > 50:
                    result.append((hwnd, title))
        return True

    user32.EnumWindows(cb, 0)
    return result
