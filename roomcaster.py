"""Roomcaster — send any URL, screen, or system audio to a Chromecast,
UPnP/DLNA renderer, or AirPlay device.

NVDA-friendly wx GUI. One asyncio loop runs on a daemon thread and owns
all pyatv (AirPlay) work; PyChromecast is synchronous and is called from
worker threads.

Usage: py roomcaster.py
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import http.server
import io
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.request
import urllib.error
import uuid as uuidlib
from typing import Any, Optional

import wx

import pychromecast
import pyatv
import zeroconf
from pyatv.const import Protocol
from pychromecast.const import CAST_TYPE_CHROMECAST
from pychromecast.controllers.media import MediaController
from pychromecast.controllers.youtube import YouTubeController
from pychromecast.models import CastInfo, HostServiceInfo

try:
    import yt_dlp
except ImportError:
    yt_dlp = None  # type: ignore[assignment]

from roomcaster_extras import (
    list_windows,
    ScreenSource,
    upnp_discover,
    upnp_play,
    upnp_stop,
    FileServer,
    yxc_available,
    yxc_set_input,
    upnp_host,
)

APP_TITLE = "Roomcaster"
APP_VERSION = "1.0.0"

YT_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{6,})"
)


def youtube_id(url: str) -> Optional[str]:
    m = YT_ID_RE.search(url)
    return m.group(1) if m else None


def guess_mime(url: str) -> str:
    path = url.split("?")[0].split("#")[0]
    mt, _ = mimetypes.guess_type(path)
    if mt:
        return mt
    lowered = url.lower()
    if ".mp3" in lowered or "aac" in lowered:
        return "audio/mpeg"
    return "audio/mpeg"


# Content-Type prefixes mapped to (mime, is_audio) for extension-less URLs
# (IPTV portals, provider VOD links, etc.).
_CT_VIDEO = {"video/mp4", "video/webm", "video/mp2t", "video/mpeg",
             "video/x-matroska", "video/quicktime", "video/x-msvideo"}
_CT_AUDIO = {"audio/mpeg", "audio/aac", "audio/aacp", "audio/mp4", "audio/x-m4a",
             "audio/ogg", "audio/flac", "audio/x-flac", "audio/wav", "audio/x-wav"}


def probe_media(url: str) -> dict:
    """Probe a URL for content type, audio-ness and live-ness.

    Magic bytes win over headers: IPTV servers lie about Content-Type
    (e.g. video/mp4 for raw MPEG-TS). Reads 193 bytes so both 188-byte TS
    and 192-byte M2TS sync patterns are visible.
    """
    result = {"url": url, "mime": guess_mime(url), "is_audio": None,
              "is_live": False}
    if not url.lower().startswith(("http://", "https://")):
        # Local file: decide from extension + magic bytes directly.
        path = url
        mt, _ = mimetypes.guess_type(path)
        mime = mt or "application/octet-stream"
        result["mime"] = mime
        result["is_audio"] = mime.startswith("audio/")
        result["is_live"] = False
        try:
            with open(path, "rb") as f:
                head = f.read(193)
            if head.startswith(b"\x47"):
                result["mime"], result["is_audio"], result["is_live"] = \
                    "video/mp2t", False, False
        except OSError:
            pass
        return result
    req = urllib.request.Request(url, headers={"User-Agent": "roomcaster/1.0",
                                               "Range": "bytes=0-192"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            head = r.read(193)
            ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    except Exception:
        ct = ""
        head = b""
    if ct in ("application/vnd.apple.mpegurl", "application/x-mpegurl"):
        result["mime"] = ct
        result["is_audio"] = False
        result["is_live"] = True
        return result
    is_audio = None
    ts_sync = (
        head.startswith(b"\x47")
        and (len(head) < 189
             or head[188:189] == b"\x47"      # 188-byte packets
             or (len(head) >= 193 and head[192:193] == b"\x47"))  # 192-byte M2TS
    )
    if ts_sync:
        # MPEG-TS sync bytes: live IPTV channel, whatever the header claims
        is_audio, mime = False, "video/mp2t"
    elif head.startswith(b"ID3") or head.startswith(b"\xff\xfb") or head.startswith(b"\xff\xf3"):
        is_audio, mime = True, "audio/mpeg"
    elif head.startswith(b"OggS"):
        is_audio, mime = True, "audio/ogg"
    elif head.startswith(b"fLaC"):
        is_audio, mime = True, "audio/flac"
    elif head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        is_audio, mime = True, "audio/wav"
    elif head.startswith(b"ftyp") or head[4:8] == b"ftyp":
        is_audio, mime = False, "video/mp4"
    elif ct in _CT_AUDIO:
        is_audio, mime = True, ct
    elif ct in _CT_VIDEO:
        is_audio, mime = False, ct
    else:
        mime = result["mime"]
        is_audio = mime.startswith("audio/")

    result["mime"] = mime
    result["is_audio"] = is_audio
    # MPEG-TS over HTTP is a live channel (segmented streams aside);
    # true VOD responses report Content-Length.
    result["is_live"] = mime == "video/mp2t"
    return result


def _no_window_kwargs() -> dict:
    """kwargs that keep any spawned subprocess fully windowless on Windows
    (no console flash, no taskbar entry, non-interactive)."""
    import sys
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        return {
            "startupinfo": si,
            "creationflags": (
                subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_BREAKAWAY_FROM_JOB
            ),
        }
    return {}


def _find_ffmpeg() -> str:
    """Locate ffmpeg: bundled (frozen exe) first, PATH, then winget."""
    import shutil
    import sys
    if getattr(sys, "frozen", False):
        bundled = os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe")
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
    raise FileNotFoundError("ffmpeg.exe not found next to the app, on PATH, or in winget packages")


_ENC_CANDIDATES = ("h264_qsv", "h264_amf", "h264_nvenc", "h264_mf", "libx264")
_encoder_cache: Optional[str] = None


def pick_h264_encoder() -> str:
    """Pick the fastest working H.264 encoder by timing a 2s 720p encode.
    Chain order alone lies (nvenc beats mf in toy tests but crawls on some
    boxes); and a tiny test lies the other way (libx264 wins toy tests but
    cannot sustain live). Real-size timing + realtime requirement does not.
    Result is cached."""
    global _encoder_cache
    if _encoder_cache:
        return _encoder_cache
    ff = _find_ffmpeg()
    results = {}
    for enc in _ENC_CANDIDATES:
        args = [
            ff, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i",
            "testsrc2=duration=2:size=1280x720:rate=30",
            "-c:v", enc, "-f", "null", "-",
        ]
        try:
            t0 = time.monotonic()
            p = subprocess.run(args, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=60,
                               **_no_window_kwargs())
            dt = time.monotonic() - t0
            if p.returncode == 0:
                results[enc] = dt
        except Exception:
            pass
    if not results:
        _encoder_cache = "libx264"
        return _encoder_cache
    best = min(results, key=lambda e: results[e])
    _encoder_cache = best
    return best


def _probe_codecs(url: str) -> list:
    """Return codec names for the URL's streams (video first). Parses the
    `ffmpeg -i` banner. Empty list on failure."""
    import re as _re
    import subprocess as _sp
    try:
        p = _sp.Popen(
            [_find_ffmpeg(), "-hide_banner", "-i", url],
            stdout=_sp.DEVNULL, stderr=_sp.PIPE, stdin=_sp.DEVNULL,
            **_no_window_kwargs())
    except OSError:
        return []
    try:
        banner = p.stderr.read(65536).decode("utf-8", "replace")
    except Exception:
        banner = ""
    finally:
        try:
            p.kill()
        except Exception:
            pass
    codecs = []
    for m in _re.finditer(
            r"Stream #0:\d+\S*\[?[^:]*\]?: (Video|Audio): (\w+)", banner):
        c = m.group(2).lower()
        if c not in codecs:
            codecs.append(c)
    return codecs


class HlsRelay:
    """Remux/transcode an MPEG-TS stream to HLS and serve it on a local port
    so a network receiver can play it.

    ffmpeg is killed and the server closed by stop(); nothing runs when idle.
    """

    # Segments the RECEIVER is shown. The encoder keeps more on disk; hiding
    # the newest ones puts the receiver a few seconds behind the live edge so
    # upstream IPTV jitter lands in a cushion instead of stalling playback.
    # 6 segments (~16s) also clears the cast receiver's minimum-buffer rule
    # of 3x TARGETDURATION, below which it refuses to start at all.
    TRAIL_KEEP = 6

    def __init__(self, url: str) -> None:
        self.url = url
        self.proc = None
        self.httpd = None
        self.port = 0
        self.root = None
        self.video_transcoded = False  # True: source video not H.264
        self._trail_drop = None   # segments hidden from the served playlist
        self._last_good = None    # last known-good playlist bytes

    def start(self, prime_segments: int = 4) -> str:
        import tempfile
        self.root = tempfile.mkdtemp(prefix="roomcaster_hls_")
        handler = functools.partial(HlsFileHandler, directory=self.root)
        # ThreadingHTTPServer + HTTP/1.1 keep-alive: the receiver reuses one
        # connection for playlist polls and segment fetches instead of a new
        # TCP handshake per request (visible as mid-playback stalls).
        self.httpd = http.server.ThreadingHTTPServer(("0.0.0.0", 0), handler)
        self.httpd.relay = self
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True,
                         name="roomcaster-hls").start()

        m3u8 = os.path.join(self.root, "live.m3u8")
        cmd = self._ffmpeg_cmd(m3u8)
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, **_no_window_kwargs())
        # Prime: accumulate a few segments BEFORE the receiver starts so it
        # begins playing with seconds of backlog already buffered.
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            segs = 0
            if os.path.exists(m3u8):
                segs = sum(1 for f in os.listdir(self.root)
                           if f.endswith(".ts") or f.endswith(".m4s"))
                if segs >= prime_segments:
                    break
            if self.proc.poll() is not None:
                raise RuntimeError("ffmpeg exited early while starting relay")
            time.sleep(0.25)
        else:
            self.stop()
            raise RuntimeError("relay produced no HLS playlist in time")
        return f"http://{self._lan_ip()}:{self.port}/live.m3u8"

    def _ffmpeg_cmd(self, m3u8: str) -> list:
        """ffmpeg command producing HLS for this relay's source."""
        cmd = [
            _find_ffmpeg(), "-hide_banner", "-loglevel", "error",
            # Survive IPTV sources dropping/jittering instead of stalling.
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "2",
            "-rw_timeout", "5000000",     # 5s read timeout on the source
            "-i", self.url,
            "-fflags", "+genpts",         # smooth over source timestamp jumps
        ]
        # Cast receivers play H.264-in-TS but reject anything else (HEVC,
        # AV1...). H.264 sources stay bit-exact; anything else gets the
        # video transcoded with the fastest hardware encoder available
        # while audio is copied untouched.
        codecs = _probe_codecs(self.url)
        bad_video = {"hevc", "h265", "av1", "mpeg2video", "mpeg4", "vp9"}
        self.video_transcoded = any(c in bad_video for c in codecs)
        if self.video_transcoded:
            cmd += ["-c:v", pick_h264_encoder(), "-c:a", "copy"]
        else:
            cmd += ["-c", "copy"]   # remux only: bit-exact, no quality loss
        cmd += [
            "-f", "hls",
            "-hls_time", "2",
            # Wide window: old segments stay listed/fetchable longer, so a
            # slow playlist poll never races the delete of a needed file.
            "-hls_list_size", "12",
            "-hls_flags", "delete_segments",
            "-hls_segment_filename", os.path.join(self.root, "seg%05d.ts"),
            m3u8,
        ]
        return cmd

    def trailing_playlist(self):
        """Playlist bytes trimmed to the trailing TRAIL_KEEP segments, so the
        receiver rides a few seconds behind the live edge. Returns None only
        before enough segments exist (the raw file is served then)."""
        if not self.root:
            return None   # relay already stopped; straggler request
        p = os.path.join(self.root, "live.m3u8")
        try:
            with open(p, "rb") as f:
                raw = f.read()
            if len(raw) < 20 or not raw.startswith(b"#EXTM3U"):
                raise OSError("partial playlist")
            self._last_good = raw
        except OSError:
            raw = self._last_good
            if raw is None:
                return None
        lines = raw.decode("utf-8", "replace").splitlines()
        try:
            seq_idx = next(i for i, l in enumerate(lines)
                           if l.startswith("#EXT-X-MEDIA-SEQUENCE:"))
            base_seq = int(lines[seq_idx].split(":", 1)[1])
        except (StopIteration, ValueError):
            return None
        segs = [i for i, l in enumerate(lines) if l and not l.startswith("#")]
        if len(segs) <= self.TRAIL_KEEP:
            return None
        drop = max(0, len(segs) - self.TRAIL_KEEP)
        # Monotonic: a receiver re-polling an older view must never see
        # segments reappear (HLS clients treat that as a broken stream).
        if self._trail_drop is None or drop > self._trail_drop:
            self._trail_drop = drop
        drop = self._trail_drop
        if drop == 0:
            return None

        def block_start(uri_idx: int) -> int:
            # Index of the first comment line belonging to this segment's
            # block (typically its EXTINF), stopping at the previous URI.
            j = uri_idx
            while j - 1 > seq_idx and lines[j - 1].startswith("#"):
                j -= 1
            return j

        header_end = block_start(segs[0])   # comments after MEDIA-SEQUENCE
        keep_from = block_start(segs[drop])
        out = lines[:seq_idx]
        out.append(f"#EXT-X-MEDIA-SEQUENCE:{base_seq + drop}")
        out.extend(lines[seq_idx + 1:header_end])  # version/targetduration
        out.extend(lines[keep_from:])
        return ("\n".join(out) + "\n").encode("utf-8")

    @staticmethod
    def _lan_ip() -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        self.proc = None
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.httpd = None
        if self.root:
            shutil.rmtree(self.root, ignore_errors=True)
        self.root = None


class SyncStreamReader(io.BufferedIOBase):
    """Adapts an asyncio StreamReader into a blocking io.BufferedIOBase so
    pyatv's miniaudio wrapper (which runs in an executor thread) can read
    from the ffmpeg pipe.
    """

    def __init__(self, reader: asyncio.StreamReader, loop: asyncio.AbstractEventLoop,
                 chunk: int = 65536) -> None:
        super().__init__()
        self._reader = reader
        self._loop = loop
        self._chunk = chunk
        self._eof = False

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self._eof:
            return b""
        want = self._chunk if size in (-1, None) else max(size, 1)
        fut = asyncio.run_coroutine_threadsafe(
            self._reader.read(want), self._loop)
        try:
            data = fut.result(timeout=30)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            data = b""
        if not data:
            self._eof = True
        return data

    def seekable(self) -> bool:
        return False

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def next(self) -> bytes:
        return self.__next__()


class HlsFileHandler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: fewer TCP handshakes

    relay_requests: list = []  # diagnostic record of every fetch

    _TYPES = {
        ".m3u8": "application/vnd.apple.mpegurl",
        ".ts": "video/mp2t",
        ".m4s": "video/iso.segment",
        ".mp4": "video/mp4",
    }

    def guess_type(self, path):
        # Windows' mimetypes registry maps .ts to TypeScript and .m3u8 to
        # random things; receivers reject those, so force the right types.
        import os as _os
        return self._TYPES.get(_os.path.splitext(path)[1].lower(),
                               "application/octet-stream")

    def end_headers(self):
        # Some cast receivers (esp. FFM-based TVs) refuse HLS without CORS
        # headers, even though the fetch is native. Send permissive ones.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        HlsFileHandler.relay_requests.append(
            (time.strftime("%H:%M:%S"), self.path, self.client_address[0]))
        # Playlist requests get the trailing-edge view (see HlsRelay).
        if self.path.rstrip("/").endswith("live.m3u8") and self.server is not None:
            relay = getattr(self.server, "relay", None)
            if relay is not None:
                data = relay.trailing_playlist()
                if data is not None:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
        return super().do_GET()

    def log_message(self, format, *args):
        pass  # keep console quiet


class LoopThread(threading.Thread):
    """Daemon thread owning the asyncio loop for pyatv."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="roomcaster-asyncio")
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()

    def run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        self.loop.run_forever()

    def submit(self, coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, self.loop)


class Device:
    def __init__(self, kind: str, name: str, key: Any) -> None:
        self.kind = kind  # "chromecast" | "airplay" | "upnp"
        self.name = name
        self.key = key


class MainFrame(wx.Frame):
    def __init__(self) -> None:
        super().__init__(None, title=APP_TITLE, size=(560, 480))
        self.loop_thread = LoopThread()
        self.loop_thread.start()
        self.loop_thread.ready.wait()

        self.devices: dict[str, Device] = {}
        self.current: Optional[Device] = None
        self.cast: Optional[pychromecast.Chromecast] = None
        self.yt: Optional[YouTubeController] = None
        self.atv = None  # pyatv AppleTV
        self.stream_task: Optional[asyncio.Task] = None
        self._runner_fut: Optional[concurrent.futures.Future] = None
        self._stop_flag = True
        self._ffmpeg_proc = None
        self._relay = None
        self._file_server = None
        self._updating_slider = False

        self._build_ui()
        self._bind_events()
        self.status_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_status_timer, self.status_timer)
        self.status_timer.Start(1500)
        self.Show()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        self._make_menu()

        panel = wx.Panel(self)
        vbox = wx.BoxSizer(wx.VERTICAL)

        self.device_list = wx.ListBox(panel, style=wx.LB_SINGLE)
        self.device_list.SetHelpText("Discovered devices.")
        vbox.Add(self.device_list, 1, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 8)

        self.url_box = wx.TextCtrl(panel)
        self.url_box.SetHelpText("URL to cast.")
        vbox.Add(self.url_box, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 8)

        self.btn_cast = wx.Button(panel, label="&Play")
        self.btn_cast.SetDefault()
        vbox.Add(self.btn_cast, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 8)

        row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_playpause = wx.Button(panel, label="&Pause")
        self.btn_stop = wx.Button(panel, label="S&top")
        self.btn_back = wx.Button(panel, label="&-10s")
        self.btn_fwd = wx.Button(panel, label="&+10s")
        for b in (self.btn_playpause, self.btn_stop, self.btn_back, self.btn_fwd):
            row.Add(b, 1, wx.RIGHT, 8)
        vbox.Add(row, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 8)

        self.pos_slider = wx.Slider(panel, value=0, minValue=0, maxValue=100,
                                    style=wx.SL_HORIZONTAL | wx.SL_LABELS)
        self.pos_slider.SetHelpText("Position. Arrows move it.")
        vbox.Add(self.pos_slider, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

        self.vol_slider = wx.Slider(panel, value=100, minValue=0, maxValue=100,
                                    style=wx.SL_HORIZONTAL | wx.SL_LABELS)
        self.vol_slider.SetHelpText("Volume. Arrows adjust it.")
        vbox.Add(self.vol_slider, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

        self.status_bar = wx.StatusBar(self)
        self.SetStatusBar(self.status_bar)
        self.status_bar.SetStatusText("Ready.")

        panel.SetSizer(vbox)

        self._acc_url_id = wx.NewIdRef()
        self.SetAcceleratorTable(wx.AcceleratorTable([
            (wx.ACCEL_CTRL, ord("D"), self.btn_discover.GetId()),
            (wx.ACCEL_CTRL, ord("U"), self._acc_url_id),
            (wx.ACCEL_CTRL, ord("O"), self.mi_file_id),
        ]))
        self.Bind(wx.EVT_MENU, lambda e: self.url_box.SetFocus(),
                  id=self._acc_url_id)
        self.Bind(wx.EVT_CLOSE, self._on_close)

    def _make_menu(self) -> None:
        mb = wx.MenuBar()
        # Device menu
        m = wx.Menu()
        self.btn_discover = m.Append(wx.ID_ANY, "&Discover\tCtrl+D",
                                     "Scan for devices")
        self.btn_discover_id = self.btn_discover.GetId()
        m.AppendSeparator()
        mi_screen = m.Append(wx.ID_ANY, "Cast &screen\tCtrl+S",
                             "Cast this PC's screen and system audio")
        self.mi_screen_id = mi_screen.GetId()
        mi_window = m.Append(wx.ID_ANY, "Cast &window\tCtrl+W",
                             "Cast a running app window with its audio")
        self.mi_window_id = mi_window.GetId()
        m.AppendSeparator()
        mi_file = m.Append(wx.ID_ANY, "&Open file...\tCtrl+O",
                           "Cast a local media file")
        self.mi_file_id = mi_file.GetId()
        m.AppendSeparator()
        mi_quit = m.Append(wx.ID_EXIT, "E&xit\tAlt+F4")
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), mi_quit)
        self._menu_device = m
        # Help menu
        h = wx.Menu()
        mi_about = h.Append(wx.ID_ABOUT, "&About")
        self.Bind(wx.EVT_MENU,
                  lambda e: wx.MessageBox(
                      f"{APP_TITLE} {APP_VERSION}\n\n"
                      "Cast URLs, files, screens and apps to "
                      "Chromecast, UPnP/DLNA and AirPlay devices.",
                      APP_TITLE, wx.ICON_INFORMATION),
                  mi_about)
        mb.Append(m, "&Device")
        mb.Append(h, "&Help")
        self.SetMenuBar(mb)

    def _bind_events(self) -> None:
        self.Bind(wx.EVT_MENU, lambda e: self.discover(),
                  id=self.btn_discover_id)
        self.Bind(wx.EVT_MENU, lambda e: self.cast_screen(),
                  id=self.mi_screen_id)
        self.Bind(wx.EVT_MENU, lambda e: self.cast_window(),
                  id=self.mi_window_id)
        self.Bind(wx.EVT_MENU, lambda e: self.open_file(),
                  id=self.mi_file_id)
        self.btn_cast.Bind(wx.EVT_BUTTON, lambda e: self.play())
        self.btn_playpause.Bind(wx.EVT_BUTTON, lambda e: self.toggle_pause())
        self.btn_stop.Bind(wx.EVT_BUTTON, lambda e: self.stop())
        self.btn_back.Bind(wx.EVT_BUTTON, lambda e: self.seek_relative(-10))
        self.btn_fwd.Bind(wx.EVT_BUTTON, lambda e: self.seek_relative(10))
        self.device_list.Bind(wx.EVT_LISTBOX, lambda e: self._on_device_selected())
        self.vol_slider.Bind(wx.EVT_SLIDER, self._on_volume)
        self.pos_slider.Bind(wx.EVT_SCROLL_THUMBRELEASE, self._on_seek_release)
        self.pos_slider.Bind(wx.EVT_SCROLL_CHANGED, self._on_seek_release)
        self.pos_slider.Bind(wx.EVT_KEY_UP, self._on_seek_key)

    # ---------- helpers ----------

    def set_status(self, text: str) -> None:
        self.status_bar.SetStatusText(text)

    def selected_device(self) -> Optional[Device]:
        sel = self.device_list.GetStringSelection()
        if not sel:
            return None
        return self.devices.get(sel)

    def _ui(self, fn, *args) -> None:
        wx.CallAfter(fn, *args)

    # ---------- discovery ----------

    def discover(self) -> None:
        self.set_status("Scanning...")
        threading.Thread(target=self._discover_sync, daemon=True).start()

    def _discover_sync(self) -> None:
        found: dict[str, Device] = {}
        # pychromecast's own browser misses some Cast devices (e.g. the FFM
        # smart TVs here), so browse _googlecast._tcp directly and read the
        # TXT records ourselves.
        try:
            import socket as socket_mod
            from zeroconf import ServiceBrowser, Zeroconf

            hits: list[tuple[str, str]] = []

            class _Listener:
                def add_service(self, zc, type_, name):
                    hits.append((type_, name))

                def update_service(self, zc, type_, name):
                    pass

                def remove_service(self, zc, type_, name):
                    pass

            zc = Zeroconf()
            ServiceBrowser(zc, "_googlecast._tcp.local.", _Listener())
            time.sleep(8)
            for type_, name in hits:
                try:
                    info = zc.get_service_info(type_, name, 4000)
                except Exception:
                    continue
                if not info or not info.addresses:
                    continue
                props = {}
                for k, v in info.properties.items():
                    kd = k.decode() if isinstance(k, bytes) else k
                    vd = (
                        v.decode(errors="replace")
                        if isinstance(v, bytes)
                        else (v or "")
                    )
                    props[kd] = vd
                host = socket_mod.inet_ntoa(info.addresses[0])
                fn = props.get("fn") or name.split(".")[0]
                found[fn] = Device("chromecast", fn, {
                    "host": host,
                    "port": info.port or 8009,
                    "uuid": props.get("id") or uuidlib.uuid4().hex,
                    "model": props.get("md") or "Chromecast",
                })
            zc.close()
        except Exception:
            traceback.print_exc()
        try:
            futs = asyncio.run_coroutine_threadsafe(
                pyatv.scan(self.loop_thread.loop, timeout=6,
                           protocol={Protocol.RAOP, Protocol.AirPlay}),
                self.loop_thread.loop,
            )
            for cfg in futs.result(20):
                if cfg.name:
                    found.setdefault(
                        cfg.name,
                        Device("airplay", cfg.name, cfg),
                    )
        except Exception:
            traceback.print_exc()
        try:
            for name, url in upnp_discover(timeout=8):
                found.setdefault(
                    name, Device("upnp", name,
                                 {"control_url": url.replace("&amp;", "&")}))
        except Exception:
            traceback.print_exc()

        self._ui(self._apply_devices, found)

    def _apply_devices(self, found: dict[str, Device]) -> None:
        self.devices = found
        self.device_list.Clear()
        for name, dev in sorted(found.items()):
            kind = {"chromecast": "Cast", "airplay": "AirPlay",
                    "upnp": "UPnP"}[dev.kind]
            label = f"{name} ({kind})"
            self.device_list.Append(label)
            # map display label back to key
            self.devices[label] = dev
        self.set_status(f"{len(found)} device(s).")

    def _on_device_selected(self) -> None:
        # All transport controls work on all device kinds.
        self.btn_playpause.Enable()
        self.btn_back.Enable()
        self.btn_fwd.Enable()

    # ---------- playback ----------

    def play(self) -> None:
        dev = self.selected_device()
        url = self.url_box.GetValue().strip()
        if not dev:
            self.set_status("Pick a device first.")
            return
        if not url:
            self.set_status("Enter a URL.")
            self.url_box.SetFocus()
            return
        self.stop_silent()
        self.current = dev
        self._runner_fut = None
        self._stop_flag = False
        if dev.kind == "chromecast":
            self._play_chromecast(dev, url)
        elif dev.kind == "upnp":
            self._play_upnp(dev, url)
        else:
            self._play_airplay(dev, url)

    # ---- UPnP/DLNA ----

    def _play_upnp(self, dev: Device, url: str) -> None:
        """Serve the URL through the local HLS relay when needed, then push
        it to the renderer via AVTransport."""
        def worker() -> None:
            try:
                probe = probe_media(url)
                if probe["mime"] == "video/mp2t":
                    relay = HlsRelay(url)
                    play_url = relay.start()
                    self._relay = relay
                else:
                    # UPnP renderers can usually fetch plain URLs; but local
                    # files need serving, so relay everything except http(s).
                    if url.lower().startswith(("http://", "https://")):
                        play_url = url
                    else:
                        relay = HlsRelay(url)  # may fail for non-media
                        play_url = relay.start()
                        self._relay = relay
                meta = (f"<DIDL-Lite xmlns:urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/>"
                        f"<item id=\"\" parentID=\"0\" restricted=\"1\">"
                        f"<dc:title xmlns:dc=\"http://purl.org/dc/elements/1.1/\">"
                        f"Roomcaster</dc:title>"
                        f"<upnp:class xmlns:upnp=\"urn:schemas-upnp-org:metadata-1-0/upnp/\">"
                        f"object.item.videoItem</upnp:class>"
                        f"<res>{play_url}</res></item></DIDL-Lite>")
                upnp_play(dev.key["control_url"], play_url, meta)
                self._ui(self.set_status, f"Playing on {dev.name}.")
            except Exception as exc:
                self._ui(self.set_status, f"UPnP error: {exc}")
                self._stop_relay()
        threading.Thread(target=worker, daemon=True).start()
        self.set_status(f"Connecting to {dev.name}...")

    # ---- Chromecast ----

    def _play_chromecast(self, dev: Device, url: str) -> None:
        def worker() -> None:
            try:
                host = dev.key["host"]
                port = dev.key.get("port", 8009)
                ci = CastInfo(
                    uuid=uuidlib.UUID(dev.key["uuid"]),
                    host=host,
                    port=port,
                    cast_type=CAST_TYPE_CHROMECAST,
                    manufacturer="",
                    model_name=dev.key.get("model") or "Chromecast",
                    friendly_name=dev.name,
                    # A HostServiceInfo entry is mandatory; with an empty set
                    # the socket client never even attempts to connect.
                    services={HostServiceInfo(host, port)},
                )
                cast = pychromecast.Chromecast(
                    ci, zconf=zeroconf.Zeroconf(), tries=3, timeout=15,
                )
                cast.wait(20)
                self.cast = cast

                vid = youtube_id(url)
                if vid:
                    yt = YouTubeController()
                    cast.register_handler(yt)
                    self.yt = yt
                    yt.play_video(vid)
                    self._ui(self.set_status, f"YouTube {vid} on {dev.name}.")
                else:
                    self._ui(self.set_status, "Probing...")
                    probe = probe_media(url)
                    mime = probe["mime"]
                    mc = cast.media_controller
                    if probe["mime"] == "video/mp2t" and not url.lower().split("?")[0].endswith(".m3u8"):
                        # Cast receivers reject raw MPEG-TS; remux via the
                        # local relay (runs only while playing).
                        self._ui(self.set_status, "Relay starting...")
                        relay = HlsRelay(url)
                        HlsFileHandler.relay_requests.clear()
                        play_url = relay.start()
                        self._relay = relay
                    else:
                        play_url = url
                    # Live channels must be LIVE; VOD must be BUFFERED or
                    # the receiver rejects/fails the load.
                    stream_type = "LIVE" if probe["is_live"] else "BUFFERED"
                    # The receiver app must be running or play_media silently
                    # no-ops (LOADING -> IDLE/FINISHED). Launch it explicitly.
                    cast.start_app("CC1AD845", force_launch=True)
                    time.sleep(2.5)
                    mc.play_media(play_url, mime if play_url == url else "application/vnd.apple.mpegurl", stream_type=stream_type)
                    mc.block_until_active(15)
                    settled = False
                    for _ in range(8):
                        time.sleep(1.5)
                        state = mc.status.player_state
                        if state == "PLAYING":
                            settled = True
                            break
                        if state == "IDLE" and mc.status.idle_reason:
                            # Load rejected; flip stream type and retry once.
                            other = "BUFFERED" if stream_type == "LIVE" else "LIVE"
                            mc.play_media(
                                play_url,
                                mime if play_url == url else "application/vnd.apple.mpegurl",
                                stream_type=other,
                            )
                            mc.block_until_active(15)
                            stream_type = other
                            continue
                    if settled:
                        kind = "live" if stream_type == "LIVE" else "file"
                        self._ui(self.set_status,
                                 f"Playing {kind} on {dev.name}.")
                    else:
                        self._ui(self.set_status,
                                 f"Load failed ({mc.status.idle_reason}).")
                        self._stop_relay()
                vol = cast.status.volume_level
                self._ui(lambda: (self.vol_slider.SetValue(int(vol * 100)) if vol else None,
                                  self.btn_playpause.SetLabel("&Pause")))
            except Exception as exc:
                self._ui(self.set_status, f"Cast error: {exc}")
                # Playback never started; don't leave the relay running.
                self._stop_relay()

        threading.Thread(target=worker, daemon=True, name="cast-play").start()
        self.set_status(f"Connecting to {dev.name}...")

    # ---- AirPlay ----

    def _play_airplay(self, dev: Device, url: str) -> None:
        # AirPlay state shared with the transport handlers on the UI thread.
        self._air_kind = None          # "audio" | "video" | "youtube"
        self._air_is_live = False
        self._air_duration = 0.0
        self._air_pos = 0.0            # position at last play/seek/pause
        self._air_play_t0 = None       # monotonic clock at (re)start
        self._air_state = "stopped"    # "stopped" | "playing" | "paused"
        self._pending_seek = None
        self._air_wake = None          # asyncio.Event: resume/seek requested
        self._air_shutdown = None      # asyncio.Event: stop the runner

        async def runner() -> None:
            cancelled = False
            wake = asyncio.Event()
            shutdown = asyncio.Event()
            self._air_wake = wake
            self._air_shutdown = shutdown
            try:
                # Probe once up front (executor thread).
                probe = await asyncio.get_running_loop().run_in_executor(
                    None, probe_media, url)
                vid = youtube_id(url)
                self._air_kind = "youtube" if vid else (
                    "audio" if probe["is_audio"] else "video")
                self._air_is_live = bool(probe["is_live"]) and self._air_kind == "video"

                self._ui(self.set_status, f"Connecting to {dev.name}...")
                atv = await pyatv.connect(dev.key, self.loop_thread.loop,
                                          protocol=Protocol.RAOP)
                self.atv = atv
                if shutdown.is_set():
                    raise asyncio.CancelledError()

                # Stream/restart loop: pause cancels the stream task and waits
                # on `wake` while KEEPING the RAOP session alive; resume/seek
                # sets `wake` and the loop reopens the source (with seek).
                while not shutdown.is_set():
                    source = await self._raop_source(url, vid)
                    if self._air_play_t0 is None:
                        self._air_play_t0 = time.monotonic()
                    self._air_state = "playing"
                    wake.clear()
                    self._ui(self.set_status, f"Streaming to {dev.name}...")
                    self.stream_task = asyncio.create_task(
                        atv.stream.stream_file(source)
                    )
                    stop_wait = asyncio.create_task(shutdown.wait())
                    wake_wait = asyncio.create_task(wake.wait())
                    try:
                        done, pending = await asyncio.wait(
                            [self.stream_task, stop_wait, wake_wait],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        # Stop the stream task unless it already finished.
                        if not self.stream_task.done():
                            self.stream_task.cancel()
                        try:
                            await self.stream_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        self.stream_task = None
                        if shutdown.is_set():
                            break
                        if wake.is_set():
                            wake.clear()
                            continue   # resume or seek: reopen the source
                        # Stream ended naturally.
                        self._ui(lambda: self.set_status("Finished."))
                        break
                    finally:
                        stop_wait.cancel()
                        wake_wait.cancel()
                        self._kill_ffmpeg()
            except asyncio.CancelledError:
                cancelled = True
            except Exception as exc:
                self._ui(lambda: self.set_status(f"AirPlay error: {exc}"))
            finally:
                st = self.stream_task
                self.stream_task = None
                if st and not st.done():
                    st.cancel()
                    try:
                        await st
                    except (asyncio.CancelledError, Exception):
                        pass
                self._kill_ffmpeg()
                atv_local = self.atv
                self.atv = None
                if atv_local:
                    try:
                        await asyncio.gather(*atv_local.close())
                    except Exception:
                        pass
                self._air_state = "stopped"
                self._air_wake = None
                self._air_shutdown = None
                if cancelled:
                    self._ui(lambda: self.set_status("Stopped."))

        self._runner_fut = self.loop_thread.submit(runner())
        self.set_status(f"Starting to {dev.name}...")

    async def _raop_source(self, url: str, vid: Optional[str]):
        """Open an audio source for RAOP at the current seek position.

        Audio URLs stream directly (RAOP decodes them). Everything else
        (YouTube, IPTV TS, provider VOD, screen/app capture) is piped
        through ffmpeg, which extracts the audio track as WAV; pyatv
        decodes the WAV stream and resamples to the negotiated format.
        """
        loop = asyncio.get_running_loop()
        if vid:
            if yt_dlp is None:
                raise RuntimeError("yt-dlp required for YouTube on AirPlay")

            def extract():
                opts = {
                    "quiet": True,
                    "noplaylist": True,
                    # Prefer m4a over HTTPS: progressive URLs work with plain
                    # range requests, while separate DASH streams can hang.
                    "format": "bestaudio[ext=m4a][protocol^=https]/"
                              "bestaudio[protocol^=https]/bestaudio",
                }
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                return info

            info = await loop.run_in_executor(None, extract)
            if info is None:
                raise RuntimeError("yt-dlp could not resolve the video")
            # A resolved CDN URL is valid briefly; fetch it fresh at open time
            # and use its real duration for the position slider.
            self._air_duration = float(info.get("duration") or 0.0)
            return info.get("url")

        if self._air_kind == "audio":
            return url

        # Video (IPTV TS channel or provider VOD): ffmpeg audio pipe.
        self._ui(self.set_status, "Extracting audio...")
        ffmpeg = _find_ffmpeg()
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
        # Survive IPTV source drops/jitter instead of feeding silence.
        cmd += [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "2",
            "-rw_timeout", "5000000",
        ]
        if self._pending_seek:
            # Input seeking: fast, lands on a keyframe close to the target.
            cmd += ["-ss", str(max(self._pending_seek - 2.0, 0.0))]
        cmd += [
            "-fflags", "+genpts",          # smooth over source timestamp jumps
            "-i", url,
            "-vn", "-map", "a:0?",          # audio track only
            "-af", "aresample=44100:async=1",  # steady clock, absorbs jitter
            "-f", "wav", "-c:a", "pcm_s16le",
            "-",                             # pipe to stdout
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
            **_no_window_kwargs(),
        )
        self._ffmpeg_proc = proc
        if self._pending_seek:
            self._air_pos = self._pending_seek
            self._pending_seek = None
        # Wrap the async pipe in a blocking reader for pyatv's miniaudio
        # wrapper (it decodes the WAV stream itself, header included).
        return SyncStreamReader(proc.stdout, self.loop_thread.loop)

    async def _resolve_for_raop(self, url: str) -> tuple[Any, str]:
        """Compatibility shim retained for tests; the runner calls
        _raop_source directly."""
        return await self._raop_source(url, youtube_id(url))

    # ---- screen / app-window casting ----

    def cast_screen(self) -> None:
        """Cast the whole desktop + system audio to the selected device."""
        dev = self.selected_device()
        if not dev:
            self.set_status("Pick a device first.")
            return
        self.stop_silent()
        self.current = dev
        src = ScreenSource()
        self._source = src
        self._stop_flag = False
        if dev.kind == "chromecast":
            self._play_chromecast(dev, src.hls_url)
        elif dev.kind == "upnp":
            self._play_upnp(dev, src.hls_url)
        else:
            self._play_airplay(dev, src.hls_url)

    # ---- local file casting ----

    def open_file(self) -> None:
        """Pick a local media file and cast it to the selected device."""
        dlg = wx.FileDialog(
            self, "Open media",
            wildcard=("Media files|*.mp3;*.m4a;*.aac;*.flac;*.wav;*.ogg;"
                      "*.mp4;*.mkv;*.avi;*.ts;*.m2ts;*.mov;*.webm"
                      "|All files|*.*"),
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        path = dlg.GetPath()
        dlg.Destroy()
        self.cast_file(path)

    def cast_file(self, path: str) -> None:
        dev = self.selected_device()
        if not dev:
            self.set_status("Pick a device first.")
            return
        self.stop_silent()
        self.current = dev
        try:
            server = FileServer(path)
            url = server.start()
        except Exception as exc:
            self.set_status(f"File error: {exc}")
            return
        self._file_server = server
        self._stop_flag = False
        name = os.path.basename(path)
        self.set_status(f"Serving {name}...")
        if dev.kind == "chromecast":
            self._play_chromecast(dev, url)
        elif dev.kind == "upnp":
            self._play_upnp(dev, url)
        else:
            self._play_airplay(dev, url)

    def cast_window(self) -> None:
        """Pick a visible top-level window and cast it with system audio."""
        picks = list_windows()
        if not picks:
            self.set_status("No windows found.")
            return
        names = [t for h, t in picks]
        dlg = wx.SingleChoiceDialog(self, "Window:", "Cast app",
                                    names, wx.CHOICEDLG_STYLE)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        hwnd, _ = picks[dlg.GetSelection()]
        dlg.Destroy()
        dev = self.selected_device()
        if not dev:
            self.set_status("Pick a device first.")
            return
        self.stop_silent()
        self.current = dev
        src = ScreenSource(hwnd=hwnd)
        self._source = src
        self._stop_flag = False
        if dev.kind == "chromecast":
            self._play_chromecast(dev, src.hls_url)
        elif dev.kind == "upnp":
            self._play_upnp(dev, src.hls_url)
        else:
            self._play_airplay(dev, src.hls_url)

    # ---- teardown ----

    def stop_silent(self) -> None:
        self._stop_flag = True
        task = self.stream_task
        if task and not task.done():
            self.loop_thread.submit(self._cancel_stream(task))
        rf = self._runner_fut
        if rf and not rf.done():
            # Runner may still be connecting; cancel it there too.
            rf.cancel()
        self._runner_fut = None
        self._kill_ffmpeg()
        self._stop_relay()
        src = getattr(self, "_source", None)
        if src:
            src.stop()
            self._source = None
        if self.current and self.current.kind == "upnp":
            upnp_stop(self.current.key["control_url"])
        if self.cast:
            try:
                self.cast.stop_app()
            except Exception:
                pass
            self.cast = None

    async def _cancel_stream(self, task: asyncio.Task) -> None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    def _kill_ffmpeg(self) -> None:
        proc = self._ffmpeg_proc
        self._ffmpeg_proc = None
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass

    def _stop_relay(self) -> None:
        relay = self._relay
        self._relay = None
        if relay:
            relay.stop()
        fs = self._file_server
        self._file_server = None
        if fs:
            fs.stop()

    def stop(self) -> None:
        self.stop_silent()
        self.set_status("Stopped.")
        self._reset_sliders()

    def toggle_pause(self) -> None:
        if self.cast:
            try:
                mc = self.cast.media_controller
                if mc.status.player_state == "PLAYING":
                    mc.pause()
                    self.btn_playpause.SetLabel("&Play")
                    self.set_status("Paused.")
                else:
                    mc.play()
                    self.btn_playpause.SetLabel("&Pause")
                    self.set_status("Playing.")
            except Exception as exc:
                self.set_status(f"Error: {exc}")
            return
        if self.atv:
            self._airplay_pause()
            return
        self.set_status("Nothing playing.")

    def _airplay_pause(self) -> None:
        """AirPlay pause: stop feeding (session stays open); resume restarts
        the stream task on the same connection."""
        if self._air_state == "playing":
            self._air_state = "paused"
            if self._air_play_t0 is not None:
                self._air_pos += time.monotonic() - self._air_play_t0
                self._air_play_t0 = None
            self._kill_ffmpeg()
            self.btn_playpause.SetLabel("&Play")
            self.set_status("Paused.")
        elif self._air_state == "paused" and self._air_wake:
            self._air_state = "playing"
            self._air_play_t0 = time.monotonic()
            self.loop_thread.submit(self._set_event(self._air_wake))
            self.btn_playpause.SetLabel("&Pause")
            self.set_status("Playing.")

    def seek_relative(self, seconds: int) -> None:
        if self.cast:
            try:
                mc = self.cast.media_controller
                cur = mc.status.current_time
                if cur is None:
                    self.set_status("Stream not seekable.")
                    return
                dur = mc.status.duration or 0
                target = max(0, min(cur + seconds, dur - 1 if dur else cur + seconds))
                mc.seek(target)
                self.set_status(f"{int(target)}s.")
            except Exception as exc:
                self.set_status(f"Seek error: {exc}")
            return
        if self.atv and self._air_kind in ("video", "youtube"):
            if self._air_is_live:
                self.set_status("Live: no seek.")
                return
            cur = self._air_current_position()
            target = max(0.0, cur + seconds)
            if self._air_duration:
                target = min(target, max(self._air_duration - 1, 0))
            self._air_seek(target)
            return
        if self.atv:
            self.set_status("No seek for this stream.")
            return
        self.set_status("Nothing playing.")

    def _air_seek(self, target: float) -> None:
        self._pending_seek = target
        self._air_pos = target
        self._air_play_t0 = time.monotonic()
        self._air_state = "playing"
        if self._air_wake:
            # Wake the runner: it cancels the stream and reopens the source
            # at the new position on the same RAOP session.
            self.loop_thread.submit(self._set_event(self._air_wake))
        self.set_status(f"{int(target)}s.")

    @staticmethod
    async def _set_event(ev: asyncio.Event) -> None:
        ev.set()

    def _air_current_position(self) -> float:
        pos = self._air_pos
        if self._air_state == "playing" and self._air_play_t0 is not None:
            pos += time.monotonic() - self._air_play_t0
        return pos

    def _on_volume(self, event) -> None:
        level = self.vol_slider.GetValue()
        try:
            if self.cast:
                self.cast.set_volume(level / 100.0)
            if self.atv:
                atv = self.atv
                self.loop_thread.submit(_set_atv_volume(atv, float(level)))
        except Exception:
            pass

    def _on_seek_release(self, event) -> None:
        self._seek_slider(self.pos_slider.GetValue())

    def _on_seek_key(self, event) -> None:
        key = event.GetKeyCode()
        if key in (wx.WXK_LEFT, wx.WXK_RIGHT, wx.WXK_UP, wx.WXK_DOWN,
                   wx.WXK_PAGEUP, wx.WXK_PAGEDOWN, wx.WXK_HOME, wx.WXK_END):
            wx.CallAfter(self._seek_slider, self.pos_slider.GetValue())
        event.Skip()

    def _seek_slider(self, value: int) -> None:
        if self.cast:
            try:
                self.cast.media_controller.seek(float(value))
                self.set_status(f"{value}s.")
            except Exception as exc:
                self.set_status(f"Seek error: {exc}")
            return
        if self.atv and self._air_kind in ("video", "youtube") and not self._air_is_live:
            self._air_seek(float(value))

    # ---------- status polling ----------

    def _on_status_timer(self, event) -> None:
        if self.atv and self._air_state != "stopped":
            # AirPlay: local position tracking, ffmpeg-driven sources only.
            if self._air_kind in ("video", "youtube") and not self._air_is_live:
                cur = int(self._air_current_position())
                dur = int(self._air_duration) if self._air_duration else 0
                self._updating_slider = True
                if dur:
                    self.pos_slider.SetRange(0, dur)
                self.pos_slider.SetValue(min(cur, self.pos_slider.Max))
                self._updating_slider = False
                state = "Playing" if self._air_state == "playing" else "Paused"
                self.set_status(
                    f"{state} — {cur}/{dur}s." if dur
                    else f"{state} — {cur}s.")
            return
        if not self.cast:
            return
        try:
            mc = self.cast.media_controller
            st = mc.status
            state = st.player_state or "idle"
            dur = int(st.duration) if st.duration else 0
            cur = int(st.current_time) if st.current_time else 0
            if dur:
                self._updating_slider = True
                self.pos_slider.SetRange(0, dur)
                self.pos_slider.SetValue(cur)
                self._updating_slider = False
                self.set_status(f"{state} — {cur}/{dur}s.")
            else:
                self.set_status(f"{state}.")
        except Exception:
            pass

    def _reset_sliders(self) -> None:
        self._updating_slider = True
        self.pos_slider.SetValue(0)
        self._updating_slider = False

    # ---------- shutdown ----------

    def _on_close(self, event) -> None:
        try:
            self.stop_silent()
            fut = self._runner_fut
            if fut and not fut.done():
                try:
                    # Wait for the runner to cancel the stream and close the
                    # device so nothing is torn down midway.
                    fut.result(timeout=8)
                except Exception:
                    pass
        finally:
            self.loop_thread.loop.call_soon_threadsafe(self.loop_thread.loop.stop)
            event.Skip()


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _close_atv(atv) -> None:
    try:
        await asyncio.gather(*atv.close())
    except Exception:
        pass


async def _set_atv_volume(atv, level: float) -> None:
    try:
        await atv.audio.set_volume(level)
    except Exception:
        pass


def main() -> None:
    app = wx.App(False)
    frame = MainFrame()
    frame.set_status("Ready. Ctrl+D to scan.")
    app.MainLoop()


if __name__ == "__main__":
    main()
