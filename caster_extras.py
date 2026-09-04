"""Caster extras: UPnP/DLNA renderer control, local file serving, and
low-latency screen / app-window / system-audio capture sources.

Kept free of caster imports to avoid a cycle; caster imports THIS module.
"""

from __future__ import annotations

import functools
import http.server
import io
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
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
# Live system-audio tap: one WASAPI loopback capture, many live consumers
# ---------------------------------------------------------------------------

class AudioTap:
    """WASAPI loopback capture of everything this PC is playing.

    One capture thread feeds any number of subscribers. Each subscriber gets a
    bounded queue, and when a consumer falls behind the oldest audio is dropped
    rather than queued. Latency is the whole point of casting a live screen, so
    a slow consumer is allowed to lose audio but never to accumulate delay.
    """

    #: Capture period. Short enough to be inaudible as delay, long enough that
    #: the Python loop is not the bottleneck.
    PERIOD = 0.02
    #: ~400 ms of slack per subscriber before the oldest audio is dropped.
    QUEUE_CHUNKS = 20

    def __init__(self) -> None:
        self.rate = 48000
        self.channels = 2
        self._subs: list = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._ready = threading.Event()
        self._error = None

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="caster-audiotap")
        self._thread.start()
        # ffmpeg's command line needs the device's real rate and channel count,
        # and those are only known once the loopback stream is open.
        if not self._ready.wait(10):
            raise RuntimeError("system audio capture did not start")
        if self._error:
            raise RuntimeError(f"system audio capture failed: {self._error}")

    def subscribe(self) -> "queue.Queue":
        q: queue.Queue = queue.Queue(maxsize=self.QUEUE_CHUNKS)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)
        try:
            q.put_nowait(None)      # wake a reader blocked in get()
        except queue.Full:
            pass

    def _publish(self, data: bytes) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                # Drop the oldest chunk to make room: stay at the live edge
                # instead of building a backlog the listener would hear as lag.
                try:
                    q.get_nowait()
                    q.put_nowait(data)
                except (queue.Empty, queue.Full):
                    pass

    def _run(self) -> None:
        pa = stream = None
        try:
            import pyaudiowpatch as pw
            pa = pw.PyAudio()
            api = pa.get_host_api_info_by_type(pw.paWASAPI)
            dev = pa.get_device_info_by_index(api["defaultOutputDevice"])
            if not dev.get("isLoopbackDevice"):
                for lb in pa.get_loopback_device_info_generator():
                    if dev["name"] in lb["name"]:
                        dev = lb
                        break
            if not dev.get("isLoopbackDevice"):
                raise RuntimeError(
                    f"no loopback capture for output device {dev['name']!r}")
            self.channels = int(dev["maxInputChannels"]) or 2
            self.rate = int(dev["defaultSampleRate"])
            frames = max(1, int(self.rate * self.PERIOD))
            stream = pa.open(format=pw.paInt16, channels=self.channels,
                             rate=self.rate, input=True,
                             input_device_index=dev["index"],
                             frames_per_buffer=frames)
            self._ready.set()
            while not self._stop.is_set():
                # Loopback keeps delivering silence when nothing is playing,
                # which is what keeps the receiver's clock running.
                self._publish(stream.read(frames, exception_on_overflow=False))
        except Exception as exc:      # surfaced by start()
            self._error = exc
            self._ready.set()
        finally:
            for close in (getattr(stream, "close", None),
                          getattr(pa, "terminate", None)):
                if close:
                    try:
                        close()
                    except Exception:
                        pass

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread:
            thread.join(timeout=2)
        with self._lock:
            subs, self._subs = list(self._subs), []
        for q in subs:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass


def wav_header(rate: int, channels: int, data_bytes: int) -> bytes:
    """A 44-byte canonical PCM WAV header declaring `data_bytes` of samples."""
    block = channels * 2
    return b"".join((
        b"RIFF", struct.pack("<I", min(data_bytes + 36, 0xFFFFFFFF)), b"WAVE",
        b"fmt ", struct.pack("<IHHIIHH", 16, 1, channels, rate,
                             rate * block, block, 16),
        b"data", struct.pack("<I", min(data_bytes, 0xFFFFFFFF)),
    ))


#: Declared payload size of an endless WAV stream.
#:
#: Receivers want *a* length -- zero makes several of them stop before the
#: first sample -- and a live capture has none, so a streaming WAV declares
#: the largest size its 32-bit header fields can express and simply keeps
#: going. The Content-Length must be told the same story, or renderers that
#: trust one over the other disagree about where the stream ends. At 48 kHz
#: stereo this caps a single session at a little over six hours.
ENDLESS_WAV_BYTES = 0xFFFFFFFF - 36


class LiveWavReader(io.BufferedIOBase):
    """Blocking, endless WAV stream off an AudioTap subscription.

    AirPlay/RAOP wants a file-like object rather than a URL, and pyatv's
    decoder needs a WAV header before it sees any samples. Reads block until
    the next capture period, which is exactly the pacing the receiver wants.
    """

    def __init__(self, tap: AudioTap) -> None:
        super().__init__()
        self._tap = tap
        self._q = tap.subscribe()
        self._buf = bytearray(
            wav_header(tap.rate, tap.channels, ENDLESS_WAV_BYTES))
        self._eof = False

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        want = 65536 if size in (-1, None) else max(int(size), 1)
        while not self._eof and len(self._buf) < want:
            try:
                chunk = self._q.get(timeout=5)
            except queue.Empty:
                break               # capture stalled; hand back what we have
            if chunk is None:       # unsubscribed
                self._eof = True
                break
            self._buf += chunk
        data, self._buf = bytes(self._buf[:want]), self._buf[want:]
        return data

    def close(self) -> None:
        if not self._eof:
            self._eof = True
            self._tap.unsubscribe(self._q)
        super().close()


# ---------------------------------------------------------------------------
# Screen / app-window capture, served as a progressive live HTTP stream
# ---------------------------------------------------------------------------

_grabber_cache: str = ""


def pick_screen_grabber(timeout: float = 10.0) -> str:
    """"ddagrab" when the Desktop Duplication API works here, else "gdigrab".

    ddagrab is the GPU path: full frame rate at a fraction of gdigrab's CPU.
    It also *hangs* rather than failing on some driver, GPU and session
    combinations (RDP and headless VMs in particular), so it is probed once
    behind a hard kill and the answer cached for the process.
    """
    global _grabber_cache
    if _grabber_cache:
        return _grabber_cache
    cmd = [_find_ffmpeg(), "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "ddagrab=output_idx=0:framerate=30",
           "-frames:v", "10", "-vf", "hwdownload,format=bgra",
           "-f", "null", "-"]
    _grabber_cache = "gdigrab"
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, **_no_window_kwargs())
    except OSError:
        return _grabber_cache
    try:
        if proc.wait(timeout=timeout) == 0:
            _grabber_cache = "ddagrab"
    except subprocess.TimeoutExpired:
        pass                        # hung: gdigrab it is
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
    return _grabber_cache


#: Per-encoder flags that trade compression efficiency for latency. Without
#: these the encoder holds several frames of lookahead, which is exactly the
#: delay screen casting cannot afford.
_LOW_LATENCY_ENCODER_ARGS = {
    "h264_nvenc": ["-preset", "p1", "-tune", "ll", "-zerolatency", "1",
                   "-rc", "cbr", "-delay", "0"],
    "h264_qsv": ["-preset", "veryfast", "-async_depth", "1",
                 "-low_delay_brc", "1"],
    "h264_amf": ["-usage", "lowlatency", "-quality", "speed", "-rc", "cbr"],
    "h264_mf": ["-rate_control", "cbr", "-scenario", "display_remoting"],
    "libx264": ["-preset", "ultrafast", "-tune", "zerolatency"],
}

#: container -> (url suffix, MIME type, audio only?)
CONTAINERS = {
    "mp4": ("live.mp4", "video/mp4", False),
    "mpegts": ("live.ts", "video/mpeg", False),
    "wav": ("live.wav", "audio/wav", True),
}


class ScreenSource:
    """This PC's screen (or one app window) plus its system audio, served as
    a progressive live HTTP stream.

    Everything here exists to keep the delay down. Nothing is written to disk
    and there are no media segments: ffmpeg is spawned per connection and its
    output goes straight to the receiver's socket, so the receiver joins at the
    live edge rather than at the start of a playlist. ``container`` picks the
    wire format the chosen receiver actually understands:

    * ``mp4``    fragmented MP4, for Chromecast
    * ``mpegts`` MPEG-TS, for DLNA renderers and TVs
    * ``wav``    system audio only, LPCM, usually with no encoder in the path
                 at all: the lowest latency this can go, for audio receivers
                 such as a MusicCast amplifier

    Audio-only casting skips video capture and encoding entirely, which is why
    it is effectively realtime rather than merely low latency.
    """

    #: Cap on encoded frame size. A 4K desktop at 30fps buries any encoder and
    #: no receiver here benefits from more than 1080p.
    MAX_WIDTH = 1920
    MAX_HEIGHT = 1080
    #: How often to emit a keyframe. This is the floor on how long a receiver
    #: waits before it can show a picture.
    KEYFRAME_SECONDS = 0.5

    def __init__(self, hwnd: int = 0, container: str = "mp4",
                 fps: int = 30, bitrate: str = "6M") -> None:
        if container not in CONTAINERS:
            raise ValueError(f"unknown container {container!r}")
        self.hwnd = hwnd
        self.container = container
        self.fps = fps
        self.bitrate = bitrate
        self.path, self.mime, self.audio_only = CONTAINERS[container]
        self.tap = AudioTap()
        self.url = ""
        self.httpd = None
        self._procs: set = set()
        self._procs_lock = threading.Lock()
        self._stopped = False
        self.last_error = ""
        #: Index into _window_specs(); start() falls forward on failure.
        self._window_spec = 0

    # ---- lifecycle ----

    def start(self, verify: bool = True) -> str:
        """Start capture and serving; return the URL to hand the receiver.

        With ``verify``, the whole chain is exercised over a local connection
        before returning, so a broken encoder or a window that has closed is
        reported here rather than as a receiver that silently plays nothing.
        """
        self._open()
        if verify:
            self._verify_with_fallbacks()
        return self.url

    def _verify_with_fallbacks(self) -> None:
        """Verify, trying each way of naming the window before giving up."""
        attempts = len(self._window_specs()) if self.hwnd else 1
        for attempt in range(attempts):
            self._window_spec = attempt
            try:
                self._verify()
                return
            except RuntimeError:
                if attempt == attempts - 1:
                    raise
                # _verify() tore the source down on the way out; put it
                # back so the next naming gets a fair try.
                self._open()

    def _open(self) -> None:
        """Start the audio tap and the HTTP server, and publish the URL."""
        self._stopped = False
        self.tap.start()
        handler = functools.partial(_LiveStreamHandler, self)
        self.httpd = http.server.ThreadingHTTPServer(("0.0.0.0", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True,
                         name="caster-live-http").start()
        self.url = (f"http://{_lan_ip()}:{self.httpd.server_address[1]}"
                    f"/{self.path}")

    #: Enough bytes to prove the encoder is producing real media, not just
    #: a container header it wrote before dying.
    VERIFY_BYTES = 32768

    def _verify(self) -> None:
        """Pull from our own URL until real media bytes come back."""
        deadline = time.monotonic() + 30
        got = 0
        try:
            with _urlreq.urlopen(self.url, timeout=30) as r:
                while got < self.VERIFY_BYTES and time.monotonic() < deadline:
                    chunk = r.read(8192)
                    if not chunk:
                        break
                    got += len(chunk)
        except Exception as exc:
            self.stop()
            raise RuntimeError(
                f"capture failed: {self.last_error or exc}") from exc
        if got < self.VERIFY_BYTES:
            self.stop()
            raise RuntimeError(
                "capture produced no data: "
                + (self.last_error or "the encoder stopped immediately"))

    def stop(self) -> None:
        self._stopped = True
        with self._procs_lock:
            procs, self._procs = list(self._procs), set()
        for proc in procs:
            _terminate(proc)
        httpd, self.httpd = self.httpd, None
        if httpd:
            # shutdown() waits for serve_forever to notice, and this is called
            # from the UI thread.
            threading.Thread(target=httpd.shutdown, daemon=True).start()
            httpd.server_close()
        self.tap.stop()
        self.url = ""

    # ---- one live connection ----

    def open_wav_reader(self) -> LiveWavReader:
        """A blocking WAV stream of the system audio, for AirPlay/RAOP."""
        return LiveWavReader(self.tap)

    def pump(self, write) -> None:
        """Stream this source to ``write`` until the client goes away."""
        try:
            if self.container == "wav" and self.pcm_is_directly_usable():
                self._pump_pcm(write)
            else:
                self._pump_ffmpeg(write)
        except Exception as exc:
            # This runs on a connection thread, so an exception here would
            # otherwise vanish and look like a receiver that plays nothing.
            self.last_error = str(exc)
            raise

    def pcm_is_directly_usable(self) -> bool:
        """True when the loopback PCM can go on the wire untouched.

        Skipping ffmpeg removes a process, a copy and a few tens of
        milliseconds. Surround or an exotic sample rate still needs
        normalising, because receivers reject it.
        """
        return self.tap.channels <= 2 and self.tap.rate in (44100, 48000)

    def wire_rate(self) -> int:
        return self.tap.rate if self.pcm_is_directly_usable() else 48000

    def wire_channels(self) -> int:
        return min(self.tap.channels, 2)

    def _pump_pcm(self, write) -> None:
        tap = self.tap
        q = tap.subscribe()
        try:
            write(wav_header(tap.rate, tap.channels, ENDLESS_WAV_BYTES))
            while not self._stopped:
                try:
                    chunk = q.get(timeout=5)
                except queue.Empty:
                    continue
                if chunk is None:
                    break
                write(chunk)
        finally:
            tap.unsubscribe(q)

    def _pump_ffmpeg(self, write) -> None:
        err_r, err_w = os.pipe()
        try:
            proc = subprocess.Popen(
                self.ffmpeg_cmd(), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=err_w, **_no_window_kwargs())
        except BaseException:
            os.close(err_r)
            raise
        finally:
            os.close(err_w)
        errors: list = []
        threading.Thread(target=self._drain_stderr, args=(err_r, errors),
                         daemon=True).start()
        with self._procs_lock:
            self._procs.add(proc)
        q = self.tap.subscribe()
        feeder = threading.Thread(target=self._feed_audio, args=(proc, q),
                                  daemon=True, name="caster-live-audio")
        feeder.start()
        try:
            while not self._stopped:
                # read1, not read: read() would sit on the encoder's output
                # until a full buffer had accumulated, which on a mostly still
                # screen is a fraction of a second of pure added delay.
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                write(chunk)
        finally:
            self.tap.unsubscribe(q)
            with self._procs_lock:
                self._procs.discard(proc)
            _terminate(proc)
            feeder.join(timeout=2)
            if errors:
                self.last_error = errors[-1]

    @staticmethod
    def _drain_stderr(fd: int, errors: list) -> None:
        """Keep the last few ffmpeg errors so a failure can be explained."""
        with os.fdopen(fd, "rb") as handle:
            for line in handle:
                text = line.decode("utf-8", "replace").strip()
                if text:
                    errors.append(text)
                    del errors[:-8]

    def _feed_audio(self, proc, q) -> None:
        """Write captured PCM into ffmpeg's stdin for as long as it wants it."""
        try:
            while not self._stopped and proc.poll() is None:
                try:
                    chunk = q.get(timeout=1)
                except queue.Empty:
                    continue
                if chunk is None:
                    break
                proc.stdin.write(chunk)
                proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass                     # ffmpeg exited, or the client hung up
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    # ---- ffmpeg ----

    def ffmpeg_cmd(self) -> list:
        from caster import pick_h264_encoder   # deferred: avoids an import cycle
        cmd = [_find_ffmpeg(), "-hide_banner", "-loglevel", "error",
               "-fflags", "+nobuffer", "-flags", "+low_delay"]
        if not self.audio_only:
            cmd += self._video_input()
        cmd += ["-thread_queue_size", "1024",
                "-f", "s16le", "-ar", str(self.tap.rate),
                "-ac", str(self.tap.channels), "-i", "pipe:0"]
        if self.audio_only:
            cmd += ["-map", "0:a"]
        else:
            cmd += ["-map", "0:v", "-map", "1:a"]
            encoder = pick_h264_encoder()
            cmd += ["-vf", self._video_filter(),
                    "-c:v", encoder,
                    *_LOW_LATENCY_ENCODER_ARGS.get(encoder, []),
                    "-b:v", self.bitrate, "-maxrate", self.bitrate,
                    "-bufsize", self.bitrate,
                    # A keyframe every half second, so a receiver that joins
                    # mid-stream or loses a fragment recovers in half a second
                    # instead of waiting out a default 250-frame GOP. Screen
                    # grabbers routinely deliver under the requested rate, so
                    # this is pinned to the clock: a frame count would stretch
                    # to whatever half a second of *achieved* frames is.
                    "-g", str(max(self.fps // 2, 1)),
                    "-keyint_min", str(max(self.fps // 2, 1)),
                    "-force_key_frames",
                    f"expr:gte(t,n_forced*{self.KEYFRAME_SECONDS})",
                    "-bf", "0",     # B-frames reorder, and reordering is delay
                    "-pix_fmt", "yuv420p"]
        # Resample against the output clock rather than letting capture jitter
        # accumulate as drift between the picture and the sound.
        cmd += ["-af", "aresample=async=1:first_pts=0"]
        cmd += self._output_args()
        return cmd + ["pipe:1"]

    def _window_specs(self) -> list:
        """gdigrab input specs for this window, best first.

        The handle is the honest identifier: window titles change while you
        watch them (a browser tab, a terminal running a spinner), two windows
        can share one, and gdigrab resolves a title only at open time. Older
        ffmpeg builds only understand ``title=``, so that stays as a fallback.
        """
        specs = [f"hwnd={self.hwnd}"]
        title = _window_title(self.hwnd)
        if title:
            specs.append(f"title={title}")
        return specs

    def _video_input(self) -> list:
        if self.hwnd:
            # ddagrab can only take a whole output, so a single window is
            # always the GDI path.
            specs = self._window_specs()
            spec = specs[min(self._window_spec, len(specs) - 1)]
            return ["-thread_queue_size", "1024", "-f", "gdigrab",
                    "-framerate", str(self.fps), "-draw_mouse", "1",
                    "-i", spec]
        if pick_screen_grabber() == "ddagrab":
            return ["-thread_queue_size", "1024", "-f", "lavfi",
                    "-i", f"ddagrab=output_idx=0:framerate={self.fps}"]
        return ["-thread_queue_size", "1024", "-f", "gdigrab",
                "-framerate", str(self.fps), "-draw_mouse", "1",
                "-i", "desktop"]

    def _video_filter(self) -> str:
        # ddagrab hands over frames still on the GPU; these encoders read
        # system memory.
        prefix = ("hwdownload,format=bgra,"
                  if not self.hwnd and pick_screen_grabber() == "ddagrab"
                  else "")
        # Cap the size, then force both dimensions even: H.264 4:2:0 requires
        # it, and a window can be any odd size at all.
        return (prefix +
                f"scale=w='min(iw,{self.MAX_WIDTH})':"
                f"h='min(ih,{self.MAX_HEIGHT})':"
                "force_original_aspect_ratio=decrease,"
                "scale=trunc(iw/2)*2:trunc(ih/2)*2")

    def _output_args(self) -> list:
        if self.container == "mp4":
            return ["-c:a", "aac", "-b:a", "160k", "-max_delay", "0",
                    "-f", "mp4",
                    # A live MP4 of unknown length: an empty moov up front,
                    # then short self-contained fragments as they are encoded.
                    "-movflags",
                    "+frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset",
                    "-frag_duration", "200000"]
        if self.container == "mpegts":
            return ["-c:a", "aac", "-b:a", "160k", "-max_delay", "0",
                    "-f", "mpegts",
                    # Repeat the tables so a receiver joining mid-stream finds
                    # the programme without waiting for the next cycle.
                    "-mpegts_flags", "+resend_headers",
                    "-flush_packets", "1"]
        # wav: fold surround or an odd rate down to something every receiver
        # takes, still uncompressed.
        return ["-ac", str(self.wire_channels()), "-ar", str(self.wire_rate()),
                "-c:a", "pcm_s16le", "-f", "wav", "-flush_packets", "1"]


def _terminate(proc) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class _LiveStreamHandler(http.server.BaseHTTPRequestHandler):
    """Serves one endless live stream per connection.

    Receivers disagree about how an endless body should be framed, and getting
    it wrong shows up as a device that connects and then plays nothing: DLNA
    renderers want a Content-Length and the DLNA feature headers, while
    Chromecast is happy to read until the connection closes.
    """

    protocol_version = "HTTP/1.1"
    #: DLNA: streaming transfer, no seeking, live source.
    DLNA_FLAGS = ("DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS="
                  "8D500000000000000000000000000000")

    def __init__(self, source: "ScreenSource", *args, **kwargs) -> None:
        self.source = source
        super().__init__(*args, **kwargs)

    def do_HEAD(self) -> None:
        self._serve(body=False)

    def do_GET(self) -> None:
        self._serve(body=True)

    def _serve(self, body: bool) -> None:
        src = self.source
        if self.path.lstrip("/") not in (src.path, ""):
            self.send_error(404)
            return
        endless_wav = src.container == "wav"
        if not endless_wav:
            # No length is knowable for a live encode, so closing the
            # connection is what ends the stream.
            self.protocol_version = "HTTP/1.0"
            self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", src.mime)
        self.send_header("Accept-Ranges", "none")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("transferMode.dlna.org", "Streaming")
        if self.headers.get("getcontentFeatures.dlna.org"):
            self.send_header("contentFeatures.dlna.org", self.DLNA_FLAGS)
        if endless_wav:
            self.send_header("Content-Length", str(44 + ENDLESS_WAV_BYTES))
        else:
            self.send_header("Connection", "close")
        self.end_headers()
        if not body:
            return
        try:
            src.pump(self.wfile.write)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                OSError):
            pass                     # the receiver stopped, or moved on

    def log_message(self, *args) -> None:
        pass


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
