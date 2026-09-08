# Copyright (c) serrebidev and contributors
# This file is part of Caster
# SPDX-License-Identifier: MIT
"""Caster — send any URL, screen, or system audio to a Chromecast,
UPnP/DLNA renderer, or AirPlay device.

NVDA-friendly wx GUI. One asyncio loop runs on a daemon thread and owns
all pyatv (AirPlay) work; PyChromecast is synchronous and is called from
worker threads.

Usage: py caster.py
"""

from __future__ import annotations

import asyncio
import collections
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
import threading
import time
import traceback
import urllib.request
import urllib.error
import urllib.parse
import uuid as uuidlib
from typing import Any, Optional

import wx

import pychromecast
import pyatv
import zeroconf
from pyatv.const import PairingRequirement, Protocol
from pychromecast.const import CAST_TYPE_CHROMECAST
from pychromecast.controllers.youtube import YouTubeController
from pychromecast.models import CastInfo, HostServiceInfo

try:
    import yt_dlp
except ImportError:
    yt_dlp = None  # type: ignore[assignment]

import caster_extras as ce
from caster_extras import (
    list_windows,
    list_output_devices,
    list_input_devices,
    ScreenSource,
    upnp_discover,
    upnp_play,
    upnp_stop,
    upnp_set_volume,
    FileServer,
    upnp_host,
)

from caster_config import Settings, preset
from caster_devices import (
    kodi_discover,
    kodi_pause,
    kodi_play,
    kodi_set_volume,
    kodi_stop,
    looks_like_sonos,
    roku_discover,
    roku_key,
    roku_play,
    roku_stop,
    sonos_discover,
    sonos_group,
    sonos_play,
    sonos_set_volume,
    yxc_available,
    yxc_set_input,
    yxc_set_power,
    yxc_set_volume,
    yxc_set_mute,
    yxc_get_volume,
    yxc_volume_db,
    yxc_current_input,
    yxc_status,
    yxc_set_link_control,
    yxc_set_link_audio_delay,
    musiccast_discover_at,
    musiccast_group,
    musiccast_ungroup,
    sonos_stop,
)
from caster_ui import (
    HotkeyManager,
    NvdaSpeaker,
    SettingsDialog,
    TrayIcon,
    labelled,
)
import caster_update

APP_TITLE = "Caster"
APP_VERSION = "0.5.7"

#: Diagnostic timeline, off unless CASTER_TRACE names a file. Buffering is a
#: timing problem and timing problems are invisible from a status bar, so this
#: records what happened and when: every probe, every encoder, every state the
#: receiver reported. Costs one environment lookup when it is off.
_TRACE_PATH = os.environ.get("CASTER_TRACE", "")
_trace_lock = threading.Lock()
_trace_t0 = time.monotonic()


def trace(event: str, detail: str = "") -> None:
    if not _TRACE_PATH:
        return
    line = (f"{time.monotonic() - _trace_t0:8.2f}s  "
            f"{threading.current_thread().name:22} {event:26} {detail}\n")
    try:
        with _trace_lock:
            with open(_TRACE_PATH, "a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError:
        pass

#: How long each discovery protocol listens for replies. SSDP and mDNS
#: answer over a few seconds rather than at once, so this is the floor on
#: how quick a scan can be -- and, because the protocols now run in
#: parallel, very nearly the whole cost of one.
DISCOVER_SECONDS = 5

YT_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{6,})"
)


def youtube_id(url: str) -> Optional[str]:
    m = YT_ID_RE.search(url)
    return m.group(1) if m else None


def url_path(url: str) -> str:
    """The URL with its query and fragment removed."""
    return url.split("?")[0].split("#")[0]


def guess_mime(url: str) -> str:
    mt, _ = mimetypes.guess_type(url_path(url))
    if mt:
        return mt
    if "aac" in url.lower():
        return "audio/aac"
    # Nothing in the URL says what this is. Radio streams are the common
    # extension-less case, so audio is the useful guess -- but it is only a
    # guess, which is why probe_media leaves is_audio unknown rather than
    # treating it as a fact. See the fallback branch there.
    return "audio/mpeg"


# Content-Type prefixes mapped to (mime, is_audio) for extension-less URLs
# (IPTV portals, provider VOD links, etc.).
_CT_VIDEO = {"video/mp4", "video/webm", "video/mp2t", "video/mpeg",
             "video/x-matroska", "video/quicktime", "video/x-msvideo"}
_CT_AUDIO = {"audio/mpeg", "audio/aac", "audio/aacp", "audio/mp4", "audio/x-m4a",
             "audio/ogg", "audio/flac", "audio/x-flac", "audio/wav", "audio/x-wav"}


def _looks_like_mpegts(head: bytes) -> bool:
    """Whether these first bytes are really MPEG-TS.

    The sync byte is 0x47, which is also ASCII "G", so on its own it says
    almost nothing: a text file, a subtitle or a GIF starting with that
    letter all pass it. What identifies the format is the byte repeating
    at the packet stride -- every 188 bytes, or every 192 for M2TS.

    Too few bytes to check the stride is an unproven claim, not a
    generous one: a buffer this small is either a file far too short to
    be a stream, or a server that sent almost nothing. Either way the
    extension is a better guide than one coincidental byte.
    """
    if not head.startswith(b"\x47"):
        return False
    if len(head) >= 189 and head[188:189] == b"\x47":
        return True                     # 188-byte packets
    return len(head) >= 193 and head[192:193] == b"\x47"   # M2TS


def _native_hls_url(url: str) -> Optional[str]:
    """Validate a live IPTV portal's sibling HLS feed before remuxing TS.

    Some portals replay buffered TS whenever a connection is reopened. Their
    HLS endpoint supplies stable sequence numbers instead. Only try the
    numeric channel URL convention, and keep the TS fallback for everything
    that does not return an actual live media playlist.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if not re.fullmatch(r"/(?:live/)?[^/]+/[^/]+/\d+\.ts", parts.path,
                        flags=re.IGNORECASE):
        return None
    candidate = urllib.parse.urlunsplit(parts._replace(path=parts.path[:-3] + ".m3u8"))
    try:
        req = urllib.request.Request(candidate, headers={"User-Agent": "caster/1.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            body = response.read(65537)
        if len(body) > 65536:
            return None
        lines = body.decode("utf-8-sig").splitlines()
        if (lines and lines[0] == "#EXTM3U"
                and any(line.startswith("#EXT-X-TARGETDURATION:") for line in lines)
                and any(line.startswith("#EXTINF:") for line in lines)
                and any(line and not line.startswith("#") for line in lines)
                and "#EXT-X-ENDLIST" not in lines):
            return candidate
    except (OSError, ValueError, UnicodeError):
        pass
    return None


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
        if os.path.splitext(path)[1].lower() == ".wav":
            mt = "audio/wav"
        mime = mt or "application/octet-stream"
        result["mime"] = mime
        result["is_audio"] = mime.startswith("audio/")
        result["is_live"] = False
        try:
            with open(path, "rb") as f:
                head = f.read(193)
            # One sync byte proves nothing: 0x47 is also ASCII "G", so a
            # local file that merely begins with that letter was being
            # sent down the live-remux path. Confirm the packet stride,
            # exactly as the HTTP branch does.
            if _looks_like_mpegts(head):
                result["mime"], result["is_audio"], result["is_live"] = \
                    "video/mp2t", False, False
        except OSError:
            pass
        return result
    req = urllib.request.Request(url, headers={"User-Agent": "caster/1.0",
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
    if _looks_like_mpegts(head):
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
    elif head[4:8] == b"ftyp":
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


def _probe_codecs(url: str, timeout: float = 8.0) -> list:
    """Return codec names for the URL's streams (video first). Parses the
    `ffmpeg -i` banner. Empty list on failure.

    Bounded twice over, because this runs in the connect path. ffmpeg is told
    how little of the stream to inspect -- the defaults spend five seconds
    analysing an MPEG-TS before saying a word -- and it is killed outright if
    it stops talking. A server that accepts the connection and then goes
    quiet would otherwise block this read with no timeout at all.
    """
    import re as _re
    import subprocess as _sp
    args = [_find_ffmpeg(), "-hide_banner",
            "-analyzeduration", "2000000", "-probesize", "2000000",
            "-i", url]
    try:
        done = _sp.run(args, stdout=_sp.DEVNULL, stderr=_sp.PIPE,
                       stdin=_sp.DEVNULL, timeout=timeout,
                       **_no_window_kwargs())
        raw = done.stderr
    except _sp.TimeoutExpired as exc:
        raw = exc.stderr or b""     # whatever it managed before the kill
    except OSError:
        return []
    banner = raw.decode("utf-8", "replace") if raw else ""
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
    # That cushion is also the delay, second for second, which is why the
    # quality preset gets to set it: a stuttering IPTV feed wants it deep, a
    # clean one wants the picture up sooner. The floor is the cast receiver's
    # minimum-buffer rule of 3x TARGETDURATION, below which it refuses to
    # start at all.
    TRAIL_KEEP = 6

    #: Sustained under-feed detection. Some IPTV CDNs cap a connection's
    #: throughput by age: it opens at full rate and decays (measured at 0.44x
    #: sustained on one live provider, 2026-09-05). A slow encoder does not drop --
    #: it just produces slower than real time, the relay's trail drains, and
    #: the receiver stalls with nothing on this side noticing. The cure is a
    #: fresh connection, which opens hot again: kill the starved encoder and
    #: let the supervisor's restart path bring one up in place (same dir,
    #: continuing numbering, monotonic playlist). Conservative by design: two
    #: consecutive ~20s windows below 55% of real time, only for sources the
    #: caller certifies live, never sooner than a minute after (re)start or
    #: 90s after the previous rotation.
    ROTATE_RATIO = 0.55      # media-seconds per wall-second that reads as fed
    ROTATE_EVAL = 20.0       # seconds between cadence evaluations
    ROTATE_STREAK = 2        # consecutive under-fed windows before rotating
    ROTATE_MIN_AGE = 45.0    # do not judge an encoder younger than this
    ROTATE_MIN_GAP = 90.0    # floor between forced rotations

    def __init__(self, url: str, hls_time: int = 2, prime_segments: int = 3,
                 trail_keep: int = TRAIL_KEEP, codecs: Optional[list] = None,
                 live: Optional[bool] = None) -> None:
        self.url = url
        #: Segment length. Shorter means the receiver can start sooner, since
        #: everything below is counted in segments, not seconds.
        self.hls_time = max(1, int(hls_time))
        #: Segments to accumulate before the URL is handed over. Three is the
        #: floor, not a preference: it is what clears 3x TARGETDURATION.
        self.prime_segments = max(3, int(prime_segments))
        self.trail_keep = max(3, int(trail_keep))
        #: Stream codecs, when the caller has already paid to find them out.
        #: Probing costs a whole extra connection to the source, and IPTV
        #: servers are slow to accept one and slower to authorise it.
        self.codecs = codecs
        self.proc = None
        self.httpd = None
        self.port = 0
        self.root = None
        self.video_transcoded = False  # True: source video not H.264
        self._trail_drop = None   # segments hidden from the served playlist
        self._last_good = None    # last known-good playlist bytes
        #: Highest EXT-X-MEDIA-SEQUENCE ever served. An HLS client treats a
        #: sequence that goes backwards as an instruction to replay, so this
        #: is a ratchet: whatever ffmpeg's own numbering does across a
        #: restart, what leaves here never decreases.
        self._served_seq = None
        self._restarted = 0       # upstream-drop restarts (diagnostics)
        #: True only when the caller certifies the source is live. Under-feed
        #: rotation restarts a connection from the live edge, which for a VOD
        #: or local file would restart the media from the beginning.
        self.live = live
        self._rotated = 0        # forced under-feed rotations (diagnostics)
        self._last_rotate = 0.0
        self._fail_streak = 0
        self._eval_t0 = None     # (monotonic, newest-seg) window start
        self._proc_born = 0.0
        #: Segment numbers where a restarted encoder began. Each is served
        #: with an #EXT-X-DISCONTINUITY tag: a fresh upstream connection
        #: continues the channel but not its timestamp clock, and a receiver
        #: told to treat the two timelines as one splices them into a replay
        #: of the last few seconds followed by a decode failure.
        self._discont_segs: set = set()
        self._playlist_lock = threading.Lock()

    def start(self, prime_segments: int = 0) -> str:
        import tempfile
        want = max(3, int(prime_segments or self.prime_segments))
        self.root = tempfile.mkdtemp(prefix="caster_hls_")
        try:
            handler = functools.partial(HlsFileHandler, directory=self.root)
            # ThreadingHTTPServer + HTTP/1.1 keep-alive: the receiver reuses
            # one connection for playlist polls and segment fetches instead of
            # a new TCP handshake per request (visible as mid-playback stalls).
            self.httpd = http.server.ThreadingHTTPServer(("0.0.0.0", 0),
                                                         handler)
            self.httpd.relay = self
            self.port = self.httpd.server_address[1]
            threading.Thread(target=self.httpd.serve_forever, daemon=True,
                             name="caster-hls").start()

            m3u8 = os.path.join(self.root, "live.m3u8")
            self._spawn_ffmpeg()
            # Prime: accumulate the minimum backlog the receiver will accept
            # BEFORE handing it the URL. On a live source these segments
            # arrive in real time, so every one asked for here is a second of
            # the wait -- which is why this is the floor and not a cushion.
            # The cushion is trail_keep, and that costs nothing up front.
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if os.path.exists(m3u8):
                    segs = sum(1 for f in os.listdir(self.root)
                               if f.endswith(".ts") or f.endswith(".m4s"))
                    if segs >= want:
                        break
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        "ffmpeg exited early while starting relay")
                time.sleep(0.1)
            else:
                raise RuntimeError("relay produced no HLS playlist in time")
        except BaseException:
            # Every exit from here leaks a server, its thread, an ffmpeg and
            # a temp directory if it does not tear them down itself.
            self.stop()
            raise
        # IPTV sources drop connections mid-stream (server reset, idle-timeout,
        # route flap). ffmpeg's reconnect flags cover reconnectable HTTP errors
        # but ffmpeg EXITS on a dead read; a supervisor restarts it in place so
        # the playlist keeps advancing and the receiver never notices.
        threading.Thread(target=self._supervise, daemon=True,
                         name="caster-relay-supervisor").start()
        return f"http://{self._lan_ip()}:{self.port}/live.m3u8"

    def _newest_seg_number(self) -> int:
        """Highest segNNNNN.ts on disk, or -1 before the first one."""
        highest = -1
        try:
            for name in os.listdir(self.root):
                match = re.fullmatch(r"seg(\d+)\.ts", name)
                if match:
                    highest = max(highest, int(match.group(1)))
        except OSError:
            pass
        return highest

    def _next_segment_number(self) -> int:
        """The number a restarted encoder must resume from.

        Without this a restart begins again at seg00000, and the receiver --
        which has already played that name and may still be holding it -- is
        handed a file it believes it knows. What comes out of the speakers is
        audio from the start of the stream: playback jumps backwards.
        """
        return self._newest_seg_number() + 1

    def _spawn_ffmpeg(self) -> None:
        """(Re)start the ffmpeg encoder process for this relay.

        Kills any still-running previous encoder FIRST, so there is always
        exactly one ffmpeg per relay. Without this, _supervise's two-step
        read-self.proc-then-kill-proc races _spawn_ffmpeg's self.proc swap:
        the kill lands on the new process and the old one keeps running as a
        zombie -- the dual-ffmpeg state seen live on 2026-09-05.
        """
        if self.root is None:
            return          # stop() already tore down the temp directory
        # One encoder per relay, always.
        prev = self.proc
        self.proc = None
        if prev is not None and prev.poll() is None:
            prev.kill()
            try:
                prev.wait(timeout=5)
            except Exception:
                prev.kill()
        m3u8 = os.path.join(self.root, "live.m3u8")
        start = self._next_segment_number()
        # append_list marks the actual first new segment. Do not guess its
        # name from files on disk: an unfinished segment can be there too.
        cmd = self._ffmpeg_cmd(m3u8, start)
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, **_no_window_kwargs())
        # A (re)started encoder gets the benefit of the doubt: rotation is
        # judged only after it has had a chance to prove its cadence.
        self._proc_born = time.monotonic()
        self._fail_streak = 0
        self._eval_t0 = None

    def _supervise(self) -> None:
        """Restart ffmpeg if it dies while the relay is up.

        A dead encoder freezes the playlist and the receiver eventually gives
        up ("stream disconnected"). Restarting in the same directory but continuing the
        segment numbering keeps the HLS continuity: names are never reused,
        the playlist is appended to rather than started over, and its
        MEDIA-SEQUENCE stays monotonic via trailing_playlist. The receiver
        just sees the stream continue.
        """
        while self.httpd is not None:
            time.sleep(2)
            if self.httpd is None:
                break   # stopped while sleeping
            now = time.monotonic()
            proc = self.proc
            if proc is not None and proc.poll() is not None:
                # ffmpeg exited on its own: upstream dropped and reconnect
                # flags gave up. Restart it unless we are shutting down.
                if self.httpd is None:
                    break
                self._restarted += 1
                try:
                    self._spawn_ffmpeg()
                except Exception:
                    pass
                continue
            if proc is not None:
                try:
                    self._check_underfeed(now)
                except Exception:
                    pass

    def _check_underfeed(self, now: float) -> None:
        """Rotate the source connection when a live encoder under-produces.

        ffmpeg does not drop on a throttled source: it keeps the connection
        and simply delivers packets slower than real time, so the relay's
        trail drains and the receiver stalls -- on a Chromecast a starved
        live HLS stream just sits there reporting PLAYING, which the app's
        reconnect watchdog never sees. Some IPTV CDNs cap throughput by
        connection age (hot at open, decaying after), so the cure is a fresh
        connection: kill this encoder and the supervisor's restart path
        brings one up in the same directory with continuing numbering.
        """
        if not self.live:
            return
        if not self.url.lower().startswith(("http://", "https://")):
            return
        if now - self._proc_born < self.ROTATE_MIN_AGE:
            return
        if now - self._last_rotate < self.ROTATE_MIN_GAP:
            return
        newest = self._newest_seg_number()
        if newest < 0:
            return
        if self._eval_t0 is None:
            # Open a cadence window: count segments from here for EVAL secs.
            self._eval_t0 = (now, newest)
            return
        t0, n0 = self._eval_t0
        span = now - t0
        if span < self.ROTATE_EVAL:
            return
        # Close the window and open the next one.
        self._eval_t0 = (now, newest)
        seg_dur = self._read_seg_dur()
        if not seg_dur or seg_dur <= 0:
            return
        # Media produced per wall-second. A live encoder keeping up scores
        # ~1.0; one provider's cap measured 0.44.
        ratio = (newest - n0) * seg_dur / span
        if ratio >= self.ROTATE_RATIO:
            self._fail_streak = 0
            return
        self._fail_streak += 1
        if self._fail_streak < self.ROTATE_STREAK:
            return
        # Sustained under-feed: rotate to a connection that opens hot.
        self._last_rotate = now
        self._fail_streak = 0
        self._eval_t0 = None
        self._rotated += 1
        trace("relay.rotate",
              f"{ratio:.2f}x media for {self.ROTATE_STREAK} consecutive "
              f"windows; restarting the encoder")
        try:
            self.proc.kill()
        except Exception:
            pass   # already gone; nothing to kill

    def _read_seg_dur(self) -> Optional[float]:
        """Media seconds per segment, from the encoder's own EXTINF line.

        Used as the cadence yardstick: at full rate a segment lands every
        EXTINF seconds of wall time, so this sidesteps hls_time's overcut.
        """
        if self.root is None:
            return None
        try:
            with open(os.path.join(self.root, "live.m3u8"), "r",
                      encoding="utf-8", errors="replace") as f:
                dur = None
                for line in f:
                    if line.startswith("#EXTINF:"):
                        try:
                            dur = float(line.split(":", 1)[1]
                                        .split(",", 1)[0])
                        except (ValueError, IndexError):
                            dur = None
                return dur
        except OSError:
            return None

    def _ffmpeg_cmd(self, m3u8: str, start_number: int = 0) -> list:
        """ffmpeg command producing HLS for this relay's source."""
        append = start_number > 0
        if append:
            # append_list adds the old entry count to start_number. Passing
            # the next filename shifts the sequence of EVERY retained URI.
            # Continue from the existing playlist's base instead.
            try:
                with open(m3u8, encoding="utf-8") as playlist:
                    for line in playlist:
                        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                            start_number = int(line.split(":", 1)[1])
                            break
            except (OSError, ValueError):
                pass
        cmd = [_find_ffmpeg(), "-hide_banner", "-loglevel", "error"]
        if self.url.lower().startswith(("http://", "https://")):
            # Survive IPTV sources dropping/jittering instead of stalling.
            # These belong to the HTTP protocol handler and nothing else:
            # handed a local path, ffmpeg refuses the whole command with
            # "Option reconnect not found" and opens no input at all.
            cmd += [
                # A live stream has no byte positions to come back to. Left
                # to itself ffmpeg reconnects with "Range: bytes=<offset>"
                # after every drop -- and this server drops every ten to
                # twenty seconds. -seekable 0 used to stop the Range request
                # and measured 0.89x here (2026-09-03); the server changed
                # (2026-09-05): it now IGNORES the unseekable declaration,
                # serves its own buffer from an earlier point, and ffmpeg
                # splices that in as though it followed on. Measured 5.2x
                # media per wall-second -- most of the channel arriving
                # twice, heard and seen as constant skip-backs.
                #
                # So: no reconnect flags at all. A drop makes ffmpeg EXIT,
                # and _supervise restarts it at the live edge in the same
                # directory -- the playlist stays monotonic, the receiver
                # rides through with a short freeze instead of a rewind.
                # The restart loop is the resilience now, not ffmpeg.
                "-seekable", "0",
                "-rw_timeout", "5000000",   # 5s read timeout on the source
            ]
        # Inspect as little of the source as it takes to identify it. The
        # defaults spend five seconds on an MPEG-TS before writing anything,
        # and that is five seconds of nothing at the start of every channel.
        cmd += ["-analyzeduration", "1000000", "-probesize", "1000000"]
        cmd += [
            # An input flag: it fills in timestamps the source omits, so it
            # has to be set on the demuxer, before -i. After -i it lands on
            # the muxer, where it means nothing.
            "-fflags", "+genpts",         # smooth over source timestamp jumps
            "-i", self.url,
        ]
        # Cast receivers play H.264-in-TS but reject anything else (HEVC,
        # AV1...). H.264 sources stay bit-exact; anything else gets the
        # video transcoded with the fastest hardware encoder available
        # while audio is copied untouched.
        codecs = self.codecs
        if codecs is None:
            codecs = _probe_codecs(self.url)
        bad_video = {"hevc", "h265", "av1", "mpeg2video", "mpeg4", "vp9"}
        self.video_transcoded = any(c in bad_video for c in codecs)
        if self.video_transcoded:
            cmd += ["-c:v", pick_h264_encoder(), "-c:a", "copy"]
        else:
            cmd += ["-c", "copy"]   # remux only: bit-exact, no quality loss
        # Put the H.264 parameter sets in front of every keyframe. An HLS
        # segment has to be decodable on its own -- a receiver may join at any
        # one of them -- and a live TS carries those sets only occasionally,
        # so without this the first segment of a channel can arrive describing
        # frames with nothing to describe them by. Costs a few bytes a
        # keyframe and nothing else.
        if not self.video_transcoded:
            cmd += ["-bsf:v", "dump_extra=freq=keyframe"]
        cmd += [
            "-f", "hls",
            "-hls_time", str(self.hls_time),
            # Wide window: old segments stay listed/fetchable longer, so a
            # slow playlist poll never races the delete of a needed file.
            "-hls_list_size", "12",
            # append_list continues the existing playlist across a restart
            # instead of truncating it, which would strip the segments the
            # receiver is still working through.
            "-hls_flags",
            "delete_segments+append_list" if append else "delete_segments",
            "-start_number", str(start_number),
            "-hls_segment_filename", os.path.join(self.root, "seg%05d.ts"),
            m3u8,
        ]
        return cmd

    def trailing_playlist(self):
        # Concurrent HTTP polls must see a consistent timeline.
        with self._playlist_lock:
            return self._trailing_playlist()

    def _trailing_playlist(self):
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
        if not segs:
            return None
        drop = max(0, len(segs) - self.trail_keep)
        # Monotonic: a receiver re-polling an older view must never see
        # segments reappear (HLS clients treat that as a broken stream).
        if self._trail_drop is None or drop > self._trail_drop:
            self._trail_drop = drop
        # ...but never past the end of a playlist that has since got shorter,
        # which is what a restarted encoder produces.
        drop = min(self._trail_drop, len(segs) - 1)

        def block_start(uri_idx: int) -> int:
            # Index of the first comment line belonging to this segment's
            # block (typically its EXTINF), stopping at the previous URI.
            j = uri_idx
            while j - 1 > seq_idx and lines[j - 1].startswith("#"):
                j -= 1
            return j

        header_end = block_start(segs[0])   # comments after MEDIA-SEQUENCE
        # append_list already marks restart seams. Record before trimming.
        for uri_idx in segs:
            num = re.fullmatch(r"seg(\d+)\.ts", lines[uri_idx].strip())
            if num and "#EXT-X-DISCONTINUITY" in lines[block_start(uri_idx):uri_idx]:
                self._discont_segs.add(int(num.group(1)))
        # The ratchet. A restarted encoder numbers from wherever it likes, and
        # handing the receiver a sequence lower than one it has already seen
        # tells it to play those segments again -- heard as the stream jumping
        # backwards. Serving a short playlist raw did exactly that, which is
        # why this rewrite now happens for every playlist, not only long ones.
        seq = base_seq + drop
        if self._served_seq is not None and seq < self._served_seq:
            seq = self._served_seq
        self._served_seq = seq

        # Restart seams. A segment written by a NEW encoder connection does
        # not continue the previous one's timestamp clock, so it is preceded
        # by a DISCONTINUITY tag -- the receiver then re-initialises its
        # decoder at the seam instead of splicing the two timelines, which
        # played the last few seconds twice and then failed.
        out = lines[:seq_idx]
        out = [line for line in out
               if not line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:")]
        out.append(f"#EXT-X-MEDIA-SEQUENCE:{seq}")
        first = re.fullmatch(r"seg(\d+)\.ts", lines[segs[drop]].strip())
        if first and self._discont_segs:
            # Preserve timeline IDs when a restart seam leaves the window.
            count = sum(n < int(first.group(1)) for n in self._discont_segs)
            out.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{count}")
        out.extend(lines[seq_idx + 1:header_end])  # version/targetduration
        for uri_idx in segs[drop:]:
            num = re.fullmatch(r"seg(\d+)\.ts", lines[uri_idx].strip())
            if num and int(num.group(1)) in self._discont_segs:
                out.append("#EXT-X-DISCONTINUITY")
            out.extend(line for line in lines[block_start(uri_idx):uri_idx + 1]
                       if line != "#EXT-X-DISCONTINUITY")
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
        # Tear the supervisor's loop condition down FIRST: httpd=None makes
        # _supervise exit, so it never restarts a relay being torn down.
        httpd, self.httpd = self.httpd, None
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        if httpd:
            # Without these the port stays bound and the serving thread stays
            # alive for the life of the app, once per relay. shutdown() waits
            # for serve_forever to notice, and stop() is called from the UI
            # thread, so it goes on a thread of its own.
            threading.Thread(target=httpd.shutdown, daemon=True).start()
            httpd.server_close()
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

    #: Diagnostic record of recent fetches. Bounded: a long IPTV cast fetches
    #: a segment every couple of seconds for hours, and an unbounded list of
    #: them is a slow leak for something only ever read while debugging.
    relay_requests = collections.deque(maxlen=200)

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

    def _serve_playlist(self, send_body: bool) -> bool:
        """Serve the relay's current playlist for GET or HEAD.

        Chromecast's media stack can probe a playlist with HEAD before it
        starts its normal GET/poll loop.  A HEAD response has to carry the
        same headers as GET but *no body*: writing playlist bytes there puts
        unexpected bytes into the persistent HTTP/1.1 connection, so the
        next parser can mistake them for the beginning of another response.
        """
        path_only = self.path.split("?")[0]
        if not path_only.rstrip("/").endswith("live.m3u8") or self.server is None:
            return False
        relay = getattr(self.server, "relay", None)
        if relay is None:
            return False
        data = relay.trailing_playlist()
        if data is None:
            return False
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if send_body:
            self.wfile.write(data)
        return True

    def do_GET(self):
        HlsFileHandler.relay_requests.append(
            (time.strftime("%H:%M:%S"), self.path, self.client_address[0]))
        # Playlist requests get the trailing-edge view (see HlsRelay).
        # Strip query string: a receiver appending ?_=N must still match.
        if self._serve_playlist(send_body=True):
            return
        return super().do_GET()

    def do_HEAD(self):
        # HEAD has the same representation headers as GET, but RFC 9110
        # forbids a response body.  In particular, do not delegate to
        # do_GET(): this handler deliberately uses persistent connections.
        if self._serve_playlist(send_body=False):
            return
        return super().do_HEAD()

    def log_message(self, format, *args):
        pass  # keep console quiet


class LoopThread(threading.Thread):
    """Daemon thread owning the asyncio loop for pyatv."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="caster-asyncio")
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()

    def run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        self.loop.run_forever()

    def submit(self, coro) -> concurrent.futures.Future:
        """Run a coroutine on the loop and hand back its future.

        Returning it matters: the caller cancels through it on stop and waits
        on it while closing. Dropping it made both no-ops -- the AirPlay
        runner was never cancelled and never waited for, so the app exited
        without letting the receiver tear the session down.
        """
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


#: How each receiver kind is shown in the device list.
KIND_LABELS = {
    "chromecast": "Cast",
    "airplay": "AirPlay",
    "upnp": "UPnP",
    "sonos": "Sonos",
    "roku": "Roku",
    "kodi": "Kodi",
    "musiccast": "MusicCast zone",
}

#: Receivers that can show a picture. Sonos is speakers, and AirPlay here is
#: RAOP, which carries audio only -- pyatv cannot mirror a screen.
VIDEO_KINDS = {"chromecast", "upnp", "roku", "kodi"}


class Device:
    def __init__(self, kind: str, name: str, key: Any,
                 sinks: frozenset = frozenset()) -> None:
        self.kind = kind          # a key of KIND_LABELS
        self.name = name
        self.key = key
        #: Content types the receiver said it accepts, empty when it did not
        #: say. See Device.supports_video.
        self.sinks = sinks
        #: {"host", "zone"} when this device also answers MusicCast, which is
        #: a control channel rather than a transport -- see caster_devices.
        self.musiccast: Optional[dict] = None

    @property
    def host(self) -> str:
        """The device's address, however its protocol happens to record it."""
        key = self.key
        if isinstance(key, dict):
            if key.get("host"):
                return str(key["host"])
            if key.get("ip"):
                return str(key["ip"])
            for field in ("control_url", "base"):
                if key.get(field):
                    return upnp_host(str(key[field]))
            return ""
        return str(getattr(key, "address", "") or "")

    @property
    def supports_video(self) -> bool:
        """Whether sending this device a picture is worth doing.

        A renderer that published its accepted content types has answered
        this outright, and is believed: "UPnP renderer" covers televisions
        and stereo amplifiers alike, and encoding H.264 for an amplifier
        costs a whole encoder to produce something it will refuse. Silence
        falls back to the kind, because plenty of renderers answer
        GetProtocolInfo badly or not at all.
        """
        if self.sinks:
            return any(m.startswith("video/") for m in self.sinks)
        return self.kind in VIDEO_KINDS

    @property
    def label(self) -> str:
        return f"{self.name} ({KIND_LABELS.get(self.kind, self.kind)})"

    def __repr__(self) -> str:
        return f"Device({self.kind}, {self.name!r})"


class MainFrame(wx.Frame):
    def __init__(self) -> None:
        super().__init__(None, title=APP_TITLE, size=(560, 480))
        self.loop_thread = LoopThread()
        self.loop_thread.start()
        self.loop_thread.ready.wait()

        self.devices: dict[str, Device] = {}
        self.current: Optional[Device] = None
        self.cast: Optional[pychromecast.Chromecast] = None
        #: One Zeroconf for every cast connection this session, closed on
        #: exit. See _cast_zeroconf().
        self._cast_zc = None
        self.yt: Optional[YouTubeController] = None
        self.atv = None  # pyatv AppleTV
        #: Per-device tracking for multi-room: stop() needs every connection.
        self._casts: dict[str, pychromecast.Chromecast] = {}
        self._cast_live_loads = {}
        self._cast_recovering = set()
        self._atvs: dict[str, object] = {}
        self.stream_task: Optional[asyncio.Task] = None
        self._runner_fut: Optional[concurrent.futures.Future] = None
        #: One runner per selected AirPlay receiver.  ``_runner_fut`` remains
        #: the primary transport-control runner, but Stop must cancel every
        #: RAOP session in a multi-room selection.
        self._air_runner_futs: list[concurrent.futures.Future] = []
        self._stop_flag = True
        self._ffmpeg_proc = None
        self._air_ffmpeg_procs: dict[str, object] = {}
        #: Relays currently running. There is more than one whenever a TS
        #: url goes to more than one receiver, and every one owns an ffmpeg
        #: and an HTTP server that has to be stopped.
        self._relays: list = []
        self._relay_lock = threading.Lock()
        self._relay = None            # the most recent, for transport control
        self._file_server = None
        #: Live captures. Usually one; a selection spanning receivers that
        #: need different wire formats gets one per format.
        self._sources: list = []
        self._targets: list = []     # every device currently being cast to
        self._updating_slider = False
        self._muted = False
        self._pre_mute_volume = 100
        self._cast_started = 0.0
        #: {(host, zone): input} to put back when the cast ends, and the hosts
        #: currently linked into one MusicCast group.
        self._mc_restore: dict = {}
        self._mc_grouped: list = []
        #: Bumped per cast so a restore from an earlier one can tell it is
        #: stale and stand down. See _musiccast_prepare.
        self._mc_epoch = 0
        self._tray = None

        self.settings = Settings()
        self._install_grabber_cache()
        self.speaker = NvdaSpeaker()
        self._last_spoken = ""

        self._build_ui()
        self._bind_events()
        self.status_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_status_timer, self.status_timer)
        self.status_timer.Start(1500)
        # Sleep timer and reconnect share one slow tick; neither needs to be
        # prompt, and a second timer would just be more to shut down.
        self.watchdog_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_watchdog, self.watchdog_timer)
        self.watchdog_timer.Start(15000)

        self.hotkeys = HotkeyManager(self, self._on_hotkey_action)
        if self.settings["global_hotkeys"]:
            self._report_lost_hotkeys(self.hotkeys.register_all())

        self.vol_slider.SetValue(int(self.settings["volume"]))
        self.Show()
        if self.settings["discover_on_launch"]:
            self.discover()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        self._make_menu()

        panel = wx.Panel(self)
        vbox = wx.BoxSizer(wx.VERTICAL)

        # A checkable list rather than a multi-selection one: arrowing
        # through a wxLB_MULTIPLE list toggles every device it passes over,
        # which makes picking one of ten a fight. Here the arrows move,
        # space ticks, and a screen reader announces the tick state --
        # so casting to one device stays a single keystroke and casting to
        # a whole room is space on each.
        self.device_list = labelled(
            panel, vbox, "&Devices:",
            lambda p: wx.CheckListBox(p, choices=[]), proportion=1)

        # A combo box, so previously cast URLs are reachable with the arrow
        # keys instead of being retyped.
        self.url_box = labelled(
            panel, vbox, "URL to &cast:",
            lambda p: wx.ComboBox(p, style=wx.TE_PROCESS_ENTER))

        self.btn_cast = wx.Button(panel, label="&Play")
        self.btn_cast.SetDefault()
        vbox.Add(self.btn_cast, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 8)

        row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_playpause = wx.Button(panel, label="Pa&use")
        self.btn_stop = wx.Button(panel, label="S&top")
        self.btn_back = wx.Button(panel, label="&Back 10s")
        self.btn_fwd = wx.Button(panel, label="For&ward 10s")
        for b in (self.btn_playpause, self.btn_stop, self.btn_back, self.btn_fwd):
            row.Add(b, 1, wx.RIGHT, 8)
        vbox.Add(row, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 8)

        self.pos_slider = labelled(
            panel, vbox, "P&osition in seconds:",
            lambda p: wx.Slider(p, value=0, minValue=0, maxValue=100,
                                style=wx.SL_HORIZONTAL | wx.SL_LABELS))

        self.vol_slider = labelled(
            panel, vbox, "&Volume percent:",
            lambda p: wx.Slider(p, value=100, minValue=0, maxValue=100,
                                style=wx.SL_HORIZONTAL | wx.SL_LABELS))

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
        self._refresh_recent()
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
                             "Screen plus system audio")
        self.mi_screen_id = mi_screen.GetId()
        mi_window = m.Append(wx.ID_ANY, "Cast &window\tCtrl+W",
                             "One app window plus its audio")
        self.mi_window_id = mi_window.GetId()
        mi_audio = m.Append(
            wx.ID_ANY, "Cast system &audio\tCtrl+Shift+A",
            "System sound only, lowest delay")
        self.mi_audio_id = mi_audio.GetId()
        m.AppendSeparator()
        mi_file = m.Append(wx.ID_ANY, "&Open file...\tCtrl+O",
                           "Cast a local file")
        self.mi_file_id = mi_file.GetId()
        m.AppendSeparator()
        mi_copy = m.Append(wx.ID_ANY, "&Copy stream address\tCtrl+Shift+C",
                           "Copy the stream address")
        self.mi_copy_id = mi_copy.GetId()
        mi_mute = m.Append(wx.ID_ANY, "&Mute\tCtrl+M",
                           "Silence the receiver, keep casting")
        self.mi_mute_id = mi_mute.GetId()
        m.AppendSeparator()
        mi_settings = m.Append(wx.ID_PREFERENCES, "&Settings...\tCtrl+,",
                               "Quality, audio source, hotkeys")
        self.mi_settings_id = mi_settings.GetId()
        mi_quit = m.Append(wx.ID_EXIT, "E&xit\tAlt+F4")
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), mi_quit)
        self._menu_device = m

        # Favourites
        self._menu_favourites = wx.Menu()
        mi_add_fav = self._menu_favourites.Append(
            wx.ID_ANY, "&Add current URL...\tCtrl+B",
            "Save the current URL")
        self.mi_add_fav_id = mi_add_fav.GetId()
        mi_del_fav = self._menu_favourites.Append(
            wx.ID_ANY, "&Remove...", "Delete a saved favourite")
        self.mi_del_fav_id = mi_del_fav.GetId()
        self._menu_favourites.AppendSeparator()
        self._fav_item_ids: dict = {}
        # Help menu
        h = wx.Menu()
        mi_keys = h.Append(wx.ID_ANY, "&Keyboard shortcuts")
        self.Bind(wx.EVT_MENU, lambda e: self._show_shortcuts(), mi_keys)
        mi_update = h.Append(wx.ID_ANY, "Check for &updates...",
                             "Check GitHub Releases for a newer Caster")
        self.mi_update_id = mi_update.GetId()
        mi_about = h.Append(wx.ID_ABOUT, "&About")
        self.Bind(wx.EVT_MENU,
                  lambda e: wx.MessageBox(
                      f"{APP_TITLE} {APP_VERSION}\n\n"
                      "Casts URLs, files, the screen or an app window to "
                      "Chromecast, Sonos, Roku, Kodi, UPnP/DLNA and "
                      "AirPlay.\n\n"
                      "Shortcuts: Help, Keyboard shortcuts.",
                      APP_TITLE, wx.ICON_INFORMATION),
                  mi_about)
        mb.Append(m, "&Device")
        mb.Append(self._menu_favourites, "&Favourites")
        mb.Append(h, "&Help")
        self.SetMenuBar(mb)
        self._rebuild_favourites()

    def _bind_events(self) -> None:
        self.Bind(wx.EVT_MENU, lambda e: self.discover(),
                  id=self.btn_discover_id)
        self.Bind(wx.EVT_MENU, lambda e: self.cast_screen(),
                  id=self.mi_screen_id)
        self.Bind(wx.EVT_MENU, lambda e: self.cast_window(),
                  id=self.mi_window_id)
        self.Bind(wx.EVT_MENU, lambda e: self.cast_audio(),
                  id=self.mi_audio_id)
        self.Bind(wx.EVT_MENU, lambda e: self.copy_stream_url(),
                  id=self.mi_copy_id)
        self.Bind(wx.EVT_MENU, lambda e: self.toggle_mute(),
                  id=self.mi_mute_id)
        self.Bind(wx.EVT_MENU, lambda e: self.show_settings(),
                  id=self.mi_settings_id)
        self.Bind(wx.EVT_MENU, lambda e: self.add_favourite(),
                  id=self.mi_add_fav_id)
        self.Bind(wx.EVT_MENU, lambda e: self.remove_favourite(),
                  id=self.mi_del_fav_id)
        self.Bind(wx.EVT_MENU, lambda e: self.check_for_updates(),
                  id=self.mi_update_id)
        self.url_box.Bind(wx.EVT_TEXT_ENTER, lambda e: self.play())
        self.Bind(wx.EVT_MENU, lambda e: self.open_file(),
                  id=self.mi_file_id)
        self.btn_cast.Bind(wx.EVT_BUTTON, lambda e: self.play())
        self.btn_playpause.Bind(wx.EVT_BUTTON, lambda e: self.toggle_pause())
        self.btn_stop.Bind(wx.EVT_BUTTON, lambda e: self.stop())
        self.btn_back.Bind(wx.EVT_BUTTON, lambda e: self.seek_relative(-10))
        self.btn_fwd.Bind(wx.EVT_BUTTON, lambda e: self.seek_relative(10))
        self.device_list.Bind(wx.EVT_LISTBOX,
                              lambda e: self._on_device_selected())
        self.vol_slider.Bind(wx.EVT_SLIDER, self._on_volume)
        self.pos_slider.Bind(wx.EVT_SCROLL_THUMBRELEASE, self._on_seek_release)
        self.pos_slider.Bind(wx.EVT_SCROLL_CHANGED, self._on_seek_release)
        self.pos_slider.Bind(wx.EVT_KEY_UP, self._on_seek_key)

    # ---------- helpers ----------

    def set_status(self, text: str, speak: bool = True) -> None:
        self.status_bar.SetStatusText(text)
        # A status bar is read on request, not when it changes, so without
        # this the outcome of a cast is silent. Repeats are dropped: the
        # position tick rewrites this every 1.5 seconds.
        if speak and text != self._last_spoken and self.settings["speak_status"]:
            self._last_spoken = text
            self.speaker.speak(text)

    def _ensure_tray(self) -> bool:
        if self._tray is not None:
            return True
        try:
            self._tray = TrayIcon(self, [
                ("&Show Caster", lambda: self._tray.restore()),
                ("-", None),
                ("Cast system &audio", self.cast_audio),
                ("Cast &screen", self.cast_screen),
                ("&Stop casting", self.stop),
                ("-", None),
                ("&Quit", self._quit_from_tray),
            ])
            return True
        except Exception:
            self._tray = None
            return False

    def _quit_from_tray(self) -> None:
        self.settings.set("minimise_to_tray", False)
        wx.CallAfter(self.Close)

    def _show_shortcuts(self) -> None:
        wx.MessageBox(
            "In the window:\n"
            "Ctrl+D scan for devices\n"
            "Ctrl+U jump to the URL box\n"
            "Ctrl+S cast the screen\n"
            "Ctrl+W cast an app window\n"
            "Ctrl+Shift+A cast system audio only\n"
            "Ctrl+O cast a local file\n"
            "Ctrl+B save the URL as a favourite\n"
            "Ctrl+Shift+C copy the stream address\n"
            "Ctrl+M mute\n"
            "Space ticks a device for multi-room\n\n"
            "Anywhere in Windows:\n" + HotkeyManager.describe(),
            "Keyboard shortcuts", wx.ICON_INFORMATION)

    def check_for_updates(self) -> None:
        """Check GitHub Releases without holding the wx event loop."""
        self.set_status("Checking for updates...", speak=False)

        def worker() -> None:
            update = caster_update.latest_update(APP_VERSION)
            if update is None:
                self._ui(self.set_status, "No newer release found.")
                return
            self._ui(self._offer_update, update)

        threading.Thread(target=worker, daemon=True,
                         name="caster-update-check").start()

    def _offer_update(self, update) -> None:
        version = ".".join(map(str, update.version))
        answer = wx.MessageBox(
            f"Caster {version} is available. Download and install it?\n\n"
            "Caster will close only after the download completes.",
            "Caster update", wx.YES_NO | wx.YES_DEFAULT | wx.ICON_INFORMATION,
            self)
        if answer != wx.YES:
            self.set_status("Update not downloaded.", speak=False)
            return
        self.set_status(f"Downloading Caster {version}...", speak=False)

        def worker() -> None:
            try:
                archive = caster_update.download(update)
            except Exception as exc:
                self._ui(self.set_status, f"Update download failed: {exc}")
                return
            self._ui(self._install_update, update, archive)

        threading.Thread(target=worker, daemon=True,
                         name="caster-update-download").start()

    def _install_update(self, update, archive: str) -> None:
        version = ".".join(map(str, update.version))
        answer = wx.MessageBox(
            f"Caster {version} is downloaded and ready to install.\n\n"
            "Install now? Caster will close, update invisibly, and reopen.",
            "Caster update", wx.YES_NO | wx.YES_DEFAULT | wx.ICON_INFORMATION,
            self)
        if answer != wx.YES:
            caster_update.discard(archive)
            self.set_status("Update cancelled.", speak=False)
            return
        try:
            caster_update.launch_installer(archive)
        except Exception as exc:
            caster_update.discard(archive)
            self.set_status(f"Update install failed: {exc}")
            return
        self.set_status(f"Installing Caster {version}.")
        # The detached helper waits for this process to release Caster.exe.
        self.Close()

    def selected_devices(self) -> list:
        """Every ticked device, or just the highlighted one if none is ticked.

        Ticking is for multi-room. Casting to a single device should not
        require ticking it first, so the cursor position stands in when
        nothing is ticked.
        """
        indices = list(self.device_list.GetCheckedItems())
        if not indices:
            current = self.device_list.GetSelection()
            if current == wx.NOT_FOUND:
                return []
            indices = [current]
        return [dev for dev in
                (self.devices.get(self.device_list.GetString(i))
                 for i in indices)
                if dev is not None]

    def selected_device(self) -> Optional[Device]:
        """The primary device: the first selection.

        Transport controls act on this one. Volume and stop act on every
        selected device, because those are the ones that make sense to apply
        to a whole room at once.
        """
        chosen = self.selected_devices()
        return chosen[0] if chosen else None

    def _report_lost_hotkeys(self, failed: list) -> None:
        """Say which system-wide hotkeys another program already owns.

        Silently losing one is worse than it sounds: the hotkey still does
        something, just in whichever program claimed it first, so the only
        symptom is Caster appearing to ignore a key it documents.
        """
        if not failed:
            return
        names = ", ".join(sorted(failed))
        self.set_status(f"Hotkeys already used by another program: {names}.",
                        speak=False)

    def _ui(self, fn, *args, **kwargs) -> None:
        # Keywords forwarded too: set_status(speak=False) is the common case,
        # and dropping them here turned a status line into a TypeError that
        # surfaced as "Cast error" with the real failure nowhere in sight.
        wx.CallAfter(fn, *args, **kwargs)

    # ---------- discovery ----------

    def discover(self) -> None:
        self.set_status("Scanning...")
        threading.Thread(target=self._discover_sync, daemon=True).start()

    def _scan_chromecast(self, zc) -> dict:
        """Cast devices, by browsing _googlecast._tcp directly.

        pychromecast's own browser misses some Cast devices (e.g. the FFM
        smart TVs here), so read the TXT records ourselves. Each service is
        resolved the moment it is announced instead of after the browse
        window closes: resolving them one at a time, several seconds
        apiece, was most of what made a scan feel slow.
        """
        if zc is None:
            return {}
        import socket as socket_mod

        found: dict[str, Device] = {}
        seen: set[str] = set()
        lock = threading.Lock()
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="cast-resolve")

        def resolve(type_: str, name: str) -> None:
            try:
                info = zc.get_service_info(type_, name, 3000)
            except Exception:
                return
            if not info or not info.addresses:
                return
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
            device = Device("chromecast", fn, {
                "host": host,
                "port": info.port or 8009,
                "uuid": props.get("id") or uuidlib.uuid4().hex,
                "model": props.get("md") or "Chromecast",
            })
            with lock:
                # Two Cast devices sharing a friendly name collapse into one.
                # Disambiguate by appending the IP so both appear in the list.
                key = fn
                if key in found:
                    key = f"{fn} ({host})"
                found[key] = device

        class _Listener:
            def add_service(self, zc_, type_, name):
                with lock:
                    if name in seen:
                        return
                    seen.add(name)
                pool.submit(resolve, type_, name)

            def update_service(self, zc_, type_, name):
                pass

            def remove_service(self, zc_, type_, name):
                pass

        browser = None
        try:
            browser = zeroconf.ServiceBrowser(
                zc, "_googlecast._tcp.local.", _Listener())
            time.sleep(DISCOVER_SECONDS)
        finally:
            if browser is not None:
                try:
                    browser.cancel()
                except Exception:
                    pass
            # Waits for the resolves already in flight; they were started
            # while the browse window was still open, so this is a tail of
            # a fraction of a second rather than another full round.
            pool.shutdown(wait=True)
        return found

    def _scan_airplay(self) -> dict:
        found: dict[str, Device] = {}
        futs = asyncio.run_coroutine_threadsafe(
            pyatv.scan(self.loop_thread.loop, timeout=DISCOVER_SECONDS,
                       protocol={Protocol.RAOP, Protocol.AirPlay}),
            self.loop_thread.loop,
        )
        for cfg in futs.result(DISCOVER_SECONDS + 15):
            if not cfg.name:
                continue
            # This app sends audio through RAOP and has no pairing flow.  An
            # AirPlay-only advertisement, or a RAOP service requiring pairing,
            # cannot receive anything Caster can send.  Showing it anyway
            # produces an inevitable, unexplained connection failure.
            raop = next((service for service in cfg.services
                         if service.protocol == Protocol.RAOP), None)
            if (raop is None
                    or raop.pairing != PairingRequirement.NotNeeded):
                continue
            # Sonos advertises AirPlay 2, and streaming to it that way
            # fails: it demands MFi hardware authentication no Python
            # client can perform, and refuses the audio port. Its own
            # protocol is discovered separately and works.
            if looks_like_sonos(cfg.name, str(
                    getattr(cfg, "device_info", ""))):
                continue
            found.setdefault(cfg.name, Device("airplay", cfg.name, cfg))
        return found

    # ---- MusicCast ----
    #
    # MusicCast is a control channel, not a transport: a Yamaha receiver takes
    # its audio over AirPlay or DLNA like anything else, and answers YXC on
    # port 80 alongside. So it is attached to whichever device entry won
    # discovery rather than being a device kind of its own -- the alternative
    # is a second entry for the same box that plays nothing.

    def _attach_musiccast(self, found: dict) -> None:
        """Attach MusicCast control to any discovered device that speaks it,
        and add an entry for each extra zone the unit has."""
        by_host: dict = {}
        for device in found.values():
            host = device.host
            if host:
                by_host.setdefault(host, device)
        if not by_host:
            return
        try:
            units = musiccast_discover_at(list(by_host))
        except Exception:
            traceback.print_exc()
            return
        for host, info in units.items():
            device = by_host[host]
            device.musiccast = {"host": host, "zone": "main",
                                "model": info.get("model", "")}
            # Extra zones are separate amplifiers fed by the same stream, so
            # they are worth offering as their own targets: ticking one wakes
            # it and puts it on the same input. It is not a separate cast --
            # the audio still arrives once, by whatever protocol the unit's
            # main entry uses.
            for zone in info.get("zones", [])[1:]:
                label = f"{device.name} {zone.replace('zone', 'Zone ')}"
                if label in found:
                    continue
                extra = Device("musiccast", label,
                               {"host": host, "zone": zone,
                                "follows": device.name})
                extra.musiccast = {"host": host, "zone": zone,
                                   "model": info.get("model", "")}
                found[label] = extra

    def _musiccast_targets(self) -> list:
        """(host, zone) for every selected device that speaks MusicCast."""
        seen, out = set(), []
        for device in (self._targets or []):
            mc = device.musiccast
            if not mc:
                continue
            pair = (mc["host"], mc["zone"])
            if pair not in seen:
                seen.add(pair)
                out.append(pair)
        return out

    def _musiccast_prepare(self, devices: list) -> None:
        """Wake and aim every MusicCast zone about to be cast to.

        A receiver in network standby accepts a stream and plays it into a
        powered-down amplifier, which is indistinguishable from a cast that
        failed; and the network input has to be selected before the push, or
        a MusicCast unit ignores it and says nothing. Both are done here,
        before a byte is sent, and neither is allowed to fail the cast.
        """
        # Every cast gets a number. A restore belonging to an older cast
        # must never run: it would switch the receiver off the stream that is
        # playing right now. Casting, stopping, and casting again within a few
        # seconds is ordinary use, and that is exactly when it happened --
        # the previous cast's restore landed in the middle of the next one and
        # put the amplifier back on the television, silently.
        self._mc_epoch += 1
        wanted = {}
        for device in devices:
            mc = device.musiccast
            if not mc:
                continue
            want = "server"
            if device.kind == "airplay":
                # RAOP makes the receiver select its own AirPlay input as the
                # session opens. Setting it here as well is at best redundant
                # and at worst a fight with the receiver over which input it
                # should be on while the stream is starting.
                want = ""
            elif device.kind == "musiccast":
                # A follower zone plays whatever the main zone is fed.
                want = ""
            wanted[(mc["host"], mc["zone"])] = want
        for (host, zone), want in wanted.items():
            try:
                previous = yxc_status(host, zone)
                if previous.get("input") and (host, zone) not in self._mc_restore:
                    self._mc_restore[(host, zone)] = previous["input"]
                if self.settings["musiccast_power_on"]:
                    yxc_set_power(host, True, zone)
                # The receiver's own volume is the authority, and the slider
                # follows it. The reverse -- pushing a remembered percentage
                # onto the amplifier as a cast begins -- turns a slider left
                # at 100 into 161 of 161 on an AV receiver, which is a room
                # at full output with no warning. Never do that implicitly.
                current = yxc_get_volume(host, zone)
                if current >= 0 and zone == "main":
                    self._ui(self._sync_volume_slider, current)
                if want:
                    yxc_set_input(host, want, zone)
                control = self.settings["musiccast_link_control"]
                delay = self.settings["musiccast_link_audio_delay"]
                if control:
                    yxc_set_link_control(host, control, zone)
                # Documented as ignored while Link Control is on Stability
                # Boost, so it is not even attempted there.
                if delay and control != "stability":
                    yxc_set_link_audio_delay(host, delay, zone)
            except Exception:
                traceback.print_exc()

    def _musiccast_restore(self) -> None:
        """Put every MusicCast zone back on the input it was showing.

        Without this a cast leaves the receiver parked on its network input
        with nothing playing, and the television it was on before is silent
        until someone finds the remote.
        """
        restore, self._mc_restore = dict(self._mc_restore), {}
        if not restore or not self.settings["musiccast_restore_input"]:
            return
        epoch = self._mc_epoch

        def worker() -> None:
            # The transport is still tearing down when stop() returns: an
            # AirPlay session that has not finished closing puts the receiver
            # back on its own input a moment later, undoing a restore sent too
            # early. So this waits for the stream to let go, then checks that
            # the input actually stuck.
            for _ in range(4):
                time.sleep(0.5)
                if self._mc_epoch != epoch:
                    return          # a new cast started; it owns the input now
            for (host, zone), previous in restore.items():
                for _ in range(3):
                    if self._mc_epoch != epoch or self._targets:
                        return
                    try:
                        if yxc_current_input(host, zone) == previous:
                            break
                        yxc_set_input(host, previous, zone)
                    except Exception:
                        pass
                    time.sleep(1.5)
        threading.Thread(target=worker, daemon=True,
                         name="musiccast-restore").start()

    def _group_musiccast(self, devices: list) -> list:
        """Link several MusicCast units so one stream feeds them all in sync.

        Same reasoning as _group_sonos: sending each unit its own copy leaves
        them audibly out of step, and the receiver can do the syncing itself.
        With a single MusicCast unit selected -- the ordinary case -- this
        does nothing at all.
        """
        hosts, mains = [], []
        for device in devices:
            mc = device.musiccast
            if mc and mc["zone"] == "main" and device.kind != "musiccast":
                hosts.append(mc["host"])
                mains.append(device)
        if len(hosts) < 2:
            return devices
        try:
            server = musiccast_group(hosts)
        except Exception as exc:
            self.set_status(f"MusicCast grouping failed: {exc}")
            return devices
        self._mc_grouped = list(hosts)
        keep = [d for d in mains if d.musiccast["host"] == server] or mains[:1]
        return [d for d in devices if d not in mains] + keep

    def _scan_upnp(self) -> dict:
        found: dict[str, Device] = {}
        for name, url, maker, sinks in upnp_discover(
                timeout=DISCOVER_SECONDS):
            # A Sonos answers UPnP too, but wants its own transport
            # handling and its own grouping; it is discovered natively
            # by _scan_sonos. Listing it twice would just offer the
            # worse path.
            if looks_like_sonos(name, maker):
                continue
            found.setdefault(
                name, Device("upnp", name,
                             {"control_url": url.replace("&amp;", "&")},
                             sinks=sinks))
        return found

    def _scan_sonos(self) -> dict:
        return {
            name: Device("sonos", name, {"ip": ip})
            for name, ip in sonos_discover(
                timeout=DISCOVER_SECONDS,
                seed_ips=self.settings["sonos_seed_ips"])
        }

    def _scan_roku(self) -> dict:
        return {name: Device("roku", name, {"base": base})
                for name, base in roku_discover(timeout=DISCOVER_SECONDS)}

    def _scan_kodi(self, zc) -> dict:
        return {name: Device("kodi", name, {"base": base})
                for name, base in kodi_discover(
                    timeout=DISCOVER_SECONDS, zc=zc)}

    def _discover_sync(self) -> None:
        """Scan every protocol at once.

        Each of these waits out a fixed listen window -- SSDP, mDNS and
        pyatv all collect replies over several seconds rather than
        answering at once -- so run one after another the scan cost the
        sum of six windows, well over half a minute. Run together it
        costs the longest single window instead.
        """
        try:
            # One Zeroconf for both mDNS scans. A second instance would
            # bind port 5353 again and send the same queries twice for
            # nothing; the two browsers on it are independent.
            zc = zeroconf.Zeroconf()
        except Exception:
            traceback.print_exc()
            zc = None

        scans = [
            ("chromecast", lambda: self._scan_chromecast(zc)),
            ("airplay", self._scan_airplay),
            ("upnp", self._scan_upnp),
            ("sonos", self._scan_sonos),
            ("roku", self._scan_roku),
            ("kodi", lambda: self._scan_kodi(zc)),
        ]

        def run(scan) -> dict:
            try:
                return scan() or {}
            except Exception:
                # One missing protocol should cost its own devices and
                # nothing else, so a failure here is never raised out.
                traceback.print_exc()
                return {}

        results: dict[str, dict] = {}
        try:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=len(scans),
                    thread_name_prefix="discover") as pool:
                futures = {pool.submit(run, scan): key for key, scan in scans}
                for done, fut in enumerate(
                        concurrent.futures.as_completed(futures), start=1):
                    results[futures[fut]] = fut.result()
                    # Deliberately not spoken: six progress lines in a row
                    # would talk over the result that actually matters.
                    self._ui(self.set_status,
                             f"Scanning... {done} of {len(scans)} done.",
                             False)
        finally:
            if zc is not None:
                try:
                    zc.close()
                except Exception:
                    pass

        # Merged in a fixed order rather than in the order the scans
        # happened to finish, so which protocol answered first cannot
        # change what ends up in the list. Sonos is applied last and wins
        # outright: it answers AirPlay and UPnP as well, and only its own
        # protocol actually plays.
        found: dict[str, Device] = {}
        for key in ("chromecast", "airplay", "upnp", "roku", "kodi"):
            for name, device in results.get(key, {}).items():
                seen = found.get(name)
                if seen is None:
                    found[name] = device
                elif (device.sinks and device.supports_video
                        and not seen.supports_video):
                    # One box answering two protocols under one name: keep the
                    # answer that can do more. A television advertises AirPlay
                    # and DLNA both, and AirPlay here is RAOP -- audio only --
                    # so first-come would silently cost it its picture. Order
                    # still decides every tie, so an amplifier (no video on
                    # either) keeps the AirPlay path, which is the better one.
                    # Only a published sink list counts as proof: a renderer
                    # that answered nothing must not displace a working entry
                    # on the strength of what its kind usually implies.
                    found[name] = device
        found.update(results.get("sonos", {}))
        self._attach_musiccast(found)

        self._ui(self._apply_devices, found)

    def _apply_devices(self, found: dict[str, Device]) -> None:
        # Keyed by display label only: the list box hands back a label, and
        # keeping bare names in here too meant two entries per device.
        self.devices = {dev.label: dev for dev in found.values()}
        self.device_list.Clear()
        for label in sorted(self.devices):
            self.device_list.Append(label)
        self.set_status(f"{len(self.devices)} device(s).")
        if self.settings["reselect_last_device"]:
            self._reselect_last_device()

    def _reselect_last_device(self) -> None:
        """Select the most recently used device that is on the network.

        Without this every launch starts with nothing selected, and the
        first action is always the same hunt through the list.
        """
        for label in self.settings["last_devices"]:
            index = self.device_list.FindString(label)
            if index != wx.NOT_FOUND:
                self.device_list.SetSelection(index)
                try:
                    self.device_list.EnsureVisible(index)
                except Exception:
                    pass
                self._on_device_selected()
                self.set_status(f"{len(self.devices)} device(s). "
                                f"{label} selected.")
                return

    def _refresh_recent(self) -> None:
        """Reload the URL box's drop-down without disturbing what is typed."""
        typed = self.url_box.GetValue()
        self.url_box.Set(self.settings["recent_urls"])
        self.url_box.SetValue(typed)

    def _rebuild_favourites(self) -> None:
        for item_id in list(self._fav_item_ids):
            item = self._menu_favourites.FindItemById(item_id)
            if item:
                self._menu_favourites.Delete(item)
        self._fav_item_ids.clear()
        for fav in self.settings["favourites"]:
            item = self._menu_favourites.Append(
                wx.ID_ANY, fav["name"], fav["url"])
            self._fav_item_ids[item.GetId()] = fav["url"]
            self.Bind(wx.EVT_MENU,
                      lambda e, u=fav["url"]: self._play_favourite(u), item)

    def _play_favourite(self, url: str) -> None:
        self.url_box.SetValue(url)
        self.play()

    def add_favourite(self) -> None:
        url = self.url_box.GetValue().strip()
        if not url:
            self.set_status("Enter a URL first.")
            self.url_box.SetFocus()
            return
        dlg = wx.TextEntryDialog(self, "Name for this favourite:",
                                 "Add favourite")
        if dlg.ShowModal() == wx.ID_OK and dlg.GetValue().strip():
            name = dlg.GetValue().strip()
            self.settings.add_favourite(name, url)
            self._rebuild_favourites()
            self.set_status(f"Saved {name}.")
        dlg.Destroy()

    def remove_favourite(self) -> None:
        names = [f["name"] for f in self.settings["favourites"]]
        if not names:
            self.set_status("No favourites saved.")
            return
        dlg = wx.SingleChoiceDialog(self, "Remove which favourite?",
                                    "Remove favourite", names)
        if dlg.ShowModal() == wx.ID_OK:
            name = names[dlg.GetSelection()]
            self.settings.remove_favourite(name)
            self._rebuild_favourites()
            self.set_status(f"Removed {name}.")
        dlg.Destroy()

    def copy_stream_url(self) -> None:
        """Put the live stream's address on the clipboard.

        Anything that plays HTTP -- VLC, a browser, a phone, a receiver this
        app has no protocol for -- can then open it. It is the catch-all for
        every device not in the list.
        """
        if not self._sources:
            self.set_status("Nothing is being captured.")
            return
        urls = [src.url for src in self._sources if src.url]
        if not urls:
            self.set_status("This capture has no address to copy.")
            return
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject("\n".join(urls)))
            wx.TheClipboard.Close()
            self.set_status(f"Copied {urls[0]}")
        else:
            self.set_status("Could not open the clipboard.")

    def show_settings(self) -> None:
        dlg = SettingsDialog(self, self.settings,
                             list_output_devices(), list_input_devices())
        if dlg.ShowModal() == wx.ID_OK:
            dlg.apply()
            if self.settings["global_hotkeys"]:
                self.hotkeys.unregister_all()
                self._report_lost_hotkeys(self.hotkeys.register_all())
            else:
                self.hotkeys.unregister_all()
            self.set_status("Settings saved.")
        dlg.Destroy()

    def _on_hotkey_action(self, action: str) -> None:
        {"cast_audio": self.cast_audio,
         "cast_screen": self.cast_screen,
         "stop": self.stop,
         "mute": self.toggle_mute,
         "volume_up": lambda: self.nudge_volume(5),
         "volume_down": lambda: self.nudge_volume(-5)}.get(
            action, lambda: None)()

    def _on_device_selected(self) -> None:
        # All transport controls work on all device kinds.
        self.btn_playpause.Enable()
        self.btn_back.Enable()
        self.btn_fwd.Enable()

    # ---------- playback ----------

    def play(self) -> None:
        devices = self.selected_devices()
        url = self.url_box.GetValue().strip()
        if not devices:
            self.set_status("Pick a device first.")
            return
        if not url:
            self.set_status("Enter a URL.")
            self.url_box.SetFocus()
            return
        self.stop_silent()
        self.current = devices[0]
        self._targets = list(devices)
        self._runner_fut = None
        self._stop_flag = False
        self.settings.add_recent_url(url)
        self.settings.note_device(devices[0].label)
        self._refresh_recent()
        self._cast_started = time.monotonic()
        devices = self._group_musiccast(self._group_sonos(devices))
        self._targets = list(devices)

        trace("play", str([d.label for d in devices]))

        def start() -> None:
            # Waking a receiver and aiming its input is several HTTP round
            # trips and can include a settle sleep. Off the UI thread, or the
            # window stops answering -- and a window that stops answering is a
            # screen reader that has gone quiet at the exact moment the user
            # is waiting to hear what happened. Dispatch goes back to the UI
            # thread, where the protocol handlers expect to be called.
            self._musiccast_prepare(devices)
            self._ui(lambda: [self._dispatch(dev, url) for dev in devices])
        threading.Thread(target=start, daemon=True, name="play").start()

    def _group_sonos(self, devices: list) -> list:
        """Collapse several Sonos speakers into one grouped coordinator.

        Sending the same stream to each speaker independently leaves them
        audibly out of step. Grouping them means Sonos itself keeps them in
        sync, and only the coordinator is told to play.
        """
        sonos = [d for d in devices if d.kind == "sonos"]
        if len(sonos) < 2:
            return devices
        try:
            coordinator_ip = sonos_group([d.key["ip"] for d in sonos])
        except Exception as exc:
            self.set_status(f"Sonos grouping failed: {exc}")
            return devices
        keep = [d for d in sonos if d.key["ip"] == coordinator_ip] or sonos[:1]
        return [d for d in devices if d.kind != "sonos"] + keep

    def _dispatch(self, dev: Device, url: str, mime: str = "",
                  is_live: Optional[bool] = None,
                  title: str = APP_TITLE) -> None:
        """Send `url` to one device by whatever protocol it speaks."""
        if dev.kind == "chromecast":
            self._play_chromecast(dev, url, mime, is_live)
        elif dev.kind == "upnp":
            self._play_upnp(dev, url, mime, title)
        elif dev.kind == "sonos":
            self._play_sonos(dev, url, mime, title)
        elif dev.kind == "roku":
            self._play_roku(dev, url, mime, title)
        elif dev.kind == "kodi":
            self._play_kodi(dev, url)
        elif dev.kind == "musiccast":
            # Control-only target: _musiccast_prepare has already woken it and
            # put it on the input its main zone is playing.
            self._ui(self.set_status, f"{dev.name} following.", speak=False)
        else:
            self._play_airplay(dev, url)

    # ---- Sonos ----

    def _play_sonos(self, dev: Device, url: str, mime: str = "",
                    title: str = APP_TITLE) -> None:
        def worker() -> None:
            try:
                sonos_play(dev.key["ip"], url,
                           title, mime or "audio/wav")
                self._ui(self.set_status, f"Playing on {dev.name}.")
            except Exception as exc:
                message = f"Sonos error: {exc}"
                self._ui(self.set_status, message)
        threading.Thread(target=worker, daemon=True, name="sonos-play").start()
        self.set_status(f"Connecting to {dev.name}...")

    # ---- Roku ----

    def _play_roku(self, dev: Device, url: str, mime: str = "",
                   title: str = APP_TITLE) -> None:
        def worker() -> None:
            try:
                roku_play(dev.key["base"], url, mime or "video/mp4", title)
                self._ui(self.set_status, f"Playing on {dev.name}.")
            except Exception as exc:
                message = f"Roku error: {exc}"
                self._ui(self.set_status, message)
        threading.Thread(target=worker, daemon=True, name="roku-play").start()
        self.set_status(f"Connecting to {dev.name}...")

    # ---- Kodi ----

    def _kodi_auth(self) -> tuple:
        return (self.settings["kodi_username"], self.settings["kodi_password"])

    def _play_kodi(self, dev: Device, url: str) -> None:
        def worker() -> None:
            try:
                kodi_play(dev.key["base"], url, self._kodi_auth())
                self._ui(self.set_status, f"Playing on {dev.name}.")
            except urllib.error.HTTPError as exc:
                message = ("Kodi refused the request; set a username and "
                           "password in settings."
                           if exc.code == 401 else f"Kodi error: {exc}")
                self._ui(self.set_status, message)
            except Exception as exc:
                message = f"Kodi error: {exc}"
                self._ui(self.set_status, message)
        threading.Thread(target=worker, daemon=True, name="kodi-play").start()
        self.set_status(f"Connecting to {dev.name}...")

    # ---- UPnP/DLNA ----

    def _play_upnp(self, dev: Device, url: str, mime: str = "",
                   title: str = APP_TITLE) -> None:
        """Serve the URL through the local HLS relay when needed, then push
        it to the renderer via AVTransport.

        `mime` short-circuits probing for streams this app is generating
        itself; probing a live capture would spawn a second encoder just to
        read the first few bytes and then throw it away.
        """
        def worker() -> None:
            relay = None
            try:
                if mime:
                    self._upnp_push(dev, url, mime, title)
                    return
                probe = probe_media(url)
                if probe["mime"] == "video/mp2t":
                    relay = self._make_relay(url,
                                             live=bool(probe["is_live"]))
                    self._keep_relay(relay)
                    play_url = relay.start()
                else:
                    # UPnP renderers can usually fetch plain URLs; but local
                    # files need serving, so relay everything except http(s).
                    if url.lower().startswith(("http://", "https://")):
                        play_url = url
                    else:
                        relay = self._make_relay(url)  # may fail: not media
                        self._keep_relay(relay)
                        play_url = relay.start()
                # The relay republishes the source as HLS, so what the
                # renderer is being handed is a playlist, not the probed type.
                self._upnp_push(
                    dev, play_url,
                    probe["mime"] if play_url == url
                    else "application/vnd.apple.mpegurl",
                    title)
            except Exception as exc:
                message = f"UPnP error: {exc}"
                self._ui(self.set_status, message)
                self._stop_relay(relay)
        threading.Thread(target=worker, daemon=True).start()
        self.set_status(f"Connecting to {dev.name}...")

    def _upnp_push(self, dev: Device, play_url: str, mime: str,
                   title: str) -> None:
        """SetAVTransportURI + Play, with the DIDL metadata the renderer
        needs to accept the stream."""
        control_url = dev.key["control_url"]
        # MusicCast receivers ignore a pushed URL unless the input is already
        # switched to the network source, and give no error when they do.
        host = upnp_host(control_url)
        # _musiccast_prepare already did this for a device discovered as
        # MusicCast; this covers a renderer reached by URL or by a path that
        # never went through discovery.
        if host and not any(h == host for h, _ in self._musiccast_targets()) \
                and yxc_available(host):
            yxc_set_input(host, "server")
        upnp_play(control_url, play_url, title, mime,
                  "object.item.audioItem.musicTrack"
                  if mime.startswith("audio/") else "object.item.videoItem")
        self._ui(self.set_status, f"Playing on {dev.name}.")

    # ---- Chromecast ----

    #: Cast receiver app id (Default Media Receiver).
    CAST_APP_ID = "CC1AD845"

    def _cast_zeroconf(self):
        """The one Zeroconf the cast client uses, made on demand.

        A fresh instance per connect binds port 5353 again and is never
        closed, so a session of channel-hopping leaks one per play.
        """
        if self._cast_zc is None:
            self._cast_zc = zeroconf.Zeroconf()
        return self._cast_zc

    def _ensure_receiver(self, cast, timeout: float = 8.0) -> None:
        """Make sure the media receiver app is running, and no more than that.

        Relaunching an app that is already up costs a full teardown and launch
        -- on a TV, seconds of it -- for no gain, so the launch is forced only
        when something else holds the screen. What replaces the old flat sleep
        afterwards is watching for the app to actually report itself: a fast
        receiver is then waited on for as long as it needs and no longer.
        """
        try:
            if cast.app_id == self.CAST_APP_ID:
                return
            cast.start_app(self.CAST_APP_ID, force_launch=True)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if cast.app_id == self.CAST_APP_ID:
                    # The app id lands a moment before the receiver will take
                    # media; this is the settle, and it is a tenth of what the
                    # old unconditional wait cost.
                    time.sleep(0.25)
                    return
                time.sleep(0.05)
        except Exception:
            pass        # play_media reports the real failure a moment later

    #: How long to give a load before calling it rejected, and how often to
    #: look. The poll interval is the floor on how soon "Playing" can be
    #: spoken, so it is short: a blind user hears this as the app hanging.
    LOAD_TIMEOUT = 12.0
    LOAD_POLL = 0.15

    #: Idle reasons that describe the END of a previous playback rather than
    #: a rejection of the new one. INTERRUPTED in particular is what the
    #: receiver says when a session is replaced -- which is exactly what
    #: loading something else does -- so treating it as a failure meant
    #: reporting the old stream's death as the new stream's.
    STALE_IDLE_REASONS = ("INTERRUPTED", "CANCELLED")

    def _await_playing(self, mc, previous_session=None) -> bool:
        """True once the receiver is playing; False if it rejected the load.

        The status object keeps reporting the previous session until the
        receiver sends one for the new one, so anything still carrying the old
        session id is ignored outright. Without that, a load issued moments
        after a stop reads the old session's dying status and is declared
        failed while it is in fact starting normally.
        """
        deadline = time.monotonic() + self.LOAD_TIMEOUT
        seen = ""
        while time.monotonic() < deadline:
            status = mc.status
            if status is not None:
                session = getattr(status, "media_session_id", None)
                stale = (previous_session is not None
                         and session == previous_session)
                state = status.player_state
                if f"{state}{stale}" != seen:
                    seen = f"{state}{stale}"
                    trace("cast.state", f"{state} {status.idle_reason or ''}"
                                        f"{' (stale session)' if stale else ''}")
                if not stale:
                    if state == "PLAYING":
                        return True
                    if (state == "IDLE" and status.idle_reason
                            and status.idle_reason
                            not in self.STALE_IDLE_REASONS):
                        return False
            time.sleep(self.LOAD_POLL)
        return False

    def _make_relay(self, url: str, codecs: Optional[list] = None,
                    live: Optional[bool] = None) -> HlsRelay:
        """An HLS relay tuned to the chosen quality preset.

        Segment length and how far behind the live edge to sit are the whole
        delay/robustness trade, so they follow the same setting the user
        already picked for capture rather than being fixed here.
        """
        chosen = preset(self.settings["capture_quality"])
        if codecs is None:
            codecs = _probe_codecs(url)
        return HlsRelay(url,
                        hls_time=chosen["hls_time"],
                        prime_segments=chosen["hls_prime"],
                        trail_keep=chosen["hls_trail"],
                        codecs=codecs, live=live)

    def _play_chromecast(self, dev: Device, url: str, mime: str = "",
                         is_live: Optional[bool] = None) -> None:
        """Load `url` on a Chromecast.

        `mime` and `is_live` short-circuit probing for streams this app is
        generating itself: probing a live capture would spawn a second
        encoder just to read its first bytes and then throw it away.
        """
        def worker() -> None:
            relay = None
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
                    ci, zconf=self._cast_zeroconf(), tries=3, timeout=15,
                )
                cast.wait(20)
                trace("cast.connected", f"{dev.name} app={cast.app_id}")
                if self._stop_flag:
                    # Stop was pressed while this was connecting. Publishing
                    # the connection now would put a live cast back on the
                    # frame that stop_silent has already cleared: nothing
                    # would ever tear it down, and the next play would read
                    # its stale PLAYING status as its own.
                    try:
                        cast.disconnect(blocking=False)
                    except Exception:
                        pass
                    return
                self.cast = cast
                self._casts[dev.label] = cast

                vid = youtube_id(url)
                if vid:
                    yt = YouTubeController()
                    cast.register_handler(yt)
                    self.yt = yt
                    yt.play_video(vid)
                    self._ui(self.set_status, f"YouTube {vid} on {dev.name}.")
                else:
                    mc = cast.media_controller
                    # Get the receiver app coming up NOW, on its own thread.
                    # A TV takes seconds to launch one, and every one of those
                    # seconds is otherwise spent after the probe and the relay
                    # have finished rather than alongside them.
                    warm = threading.Thread(
                        target=self._ensure_receiver, args=(cast,),
                        daemon=True, name="cast-warm")
                    warm.start()
                    if mime:
                        probe = {"mime": mime, "is_live": bool(is_live)}
                    else:
                        self._ui(self.set_status, "Probing...", speak=False)
                        _t = time.monotonic()
                        probe = probe_media(url)
                        trace("cast.probe",
                              f"{time.monotonic()-_t:.2f}s {probe['mime']} "
                              f"live={probe['is_live']}")
                    load_mime = probe["mime"]
                    native_url = (_native_hls_url(url)
                                  if probe["mime"] == "video/mp2t" and probe["is_live"]
                                  else None)
                    if native_url:
                        play_url = native_url
                        load_mime = "application/vnd.apple.mpegurl"
                        trace("cast.native_hls", "validated live playlist")
                    elif probe["mime"] == "video/mp2t" and not url.lower().split("?")[0].endswith(".m3u8"):
                        # Cast receivers reject raw MPEG-TS; remux via the
                        # local relay (runs only while playing).
                        self._ui(self.set_status, "Relay starting...",
                                 speak=False)
                        relay = self._make_relay(
                            url, live=bool(probe["is_live"]))
                        HlsFileHandler.relay_requests.clear()
                        self._keep_relay(relay)
                        trace("cast.relay.start",
                              f"hls_time={relay.hls_time} "
                              f"prime={relay.prime_segments} "
                              f"trail={relay.trail_keep}")
                        _t = time.monotonic()
                        play_url = relay.start()
                        trace("cast.relay.ready",
                              f"{time.monotonic()-_t:.2f}s "
                              f"transcoded={relay.video_transcoded}")
                        load_mime = "application/vnd.apple.mpegurl"
                    else:
                        play_url = url
                    # Live channels must be LIVE; VOD must be BUFFERED or
                    # the receiver rejects/fails the load.
                    stream_type = "LIVE" if probe["is_live"] else "BUFFERED"
                    # The receiver app must be running or play_media silently
                    # no-ops (LOADING -> IDLE/FINISHED).
                    warm.join(timeout=10)
                    self._ensure_receiver(cast)
                    trace("cast.receiver.ready", f"app={cast.app_id}")
                    # Whatever session is showing now belongs to the last
                    # thing played; the new one has to be told apart from it.
                    before = getattr(mc.status, "media_session_id", None)
                    mc.play_media(play_url, load_mime,
                                  stream_type=stream_type)
                    mc.block_until_active(15)
                    trace("cast.load", f"{load_mime} {stream_type} "
                                       f"prev_session={before}")
                    settled = self._await_playing(mc, before)
                    trace("cast.settled", str(settled))
                    if not settled and native_url and not self._stop_flag:
                        # A valid provider playlist can still be incompatible
                        # with this receiver. Retain the original TS route.
                        trace("cast.native_hls.fallback", "receiver did not start")
                        relay = self._make_relay(url, live=True)
                        self._keep_relay(relay)
                        play_url = relay.start()
                        before = getattr(mc.status, "media_session_id", None)
                        mc.play_media(play_url, load_mime, stream_type=stream_type)
                        mc.block_until_active(15)
                        settled = self._await_playing(mc, before)
                    if not settled:
                        # Load rejected; flip stream type and retry once.
                        stream_type = ("BUFFERED" if stream_type == "LIVE"
                                       else "LIVE")
                        before = getattr(mc.status, "media_session_id", None)
                        mc.play_media(play_url, load_mime,
                                      stream_type=stream_type)
                        mc.block_until_active(15)
                        trace("cast.retry", stream_type)
                        settled = self._await_playing(mc, before)
                    if settled:
                        if (probe["is_live"] and not self._stop_flag
                                and self._casts.get(dev.label) is cast):
                            self._cast_live_loads[dev.label] = (
                                cast, play_url, load_mime, stream_type)
                        kind = "live" if stream_type == "LIVE" else "file"
                        self._ui(self.set_status,
                                 f"Playing {kind} on {dev.name}.")
                    else:
                        self._ui(self.set_status,
                                 f"Load failed ({mc.status.idle_reason}).")
                        self._stop_relay(relay)
                vol = cast.status.volume_level
                self._ui(lambda: (self.vol_slider.SetValue(int(vol * 100)) if vol else None,
                                  self.btn_playpause.SetLabel("Pa&use")))
            except Exception as exc:
                self._ui(self.set_status, f"Cast error: {exc}")
                # Playback never started; don't leave the relay running.
                self._stop_relay(relay)

        threading.Thread(target=worker, daemon=True, name="cast-play").start()
        self.set_status(f"Connecting to {dev.name}...")

    # ---- AirPlay ----

    def _play_airplay(self, dev: Device, url: str, source=None) -> None:
        """Stream `url` to an AirPlay receiver over RAOP.

        `source` is a live ScreenSource instead of a URL. RAOP carries audio
        only -- pyatv cannot mirror a screen -- so a live source contributes
        its system audio, read straight off the capture tap with no encoder,
        no container and no HTTP hop in between.
        """
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
            atv = None
            stream_task = None
            wake = asyncio.Event()
            shutdown = asyncio.Event()
            self._air_wake = wake
            self._air_shutdown = shutdown
            try:
                if source is not None:
                    vid = None
                    self._air_kind = "live"
                    self._air_is_live = True
                else:
                    # Probe once up front (executor thread).
                    probe = await asyncio.get_running_loop().run_in_executor(
                        None, probe_media, url)
                    vid = youtube_id(url)
                    self._air_kind = "youtube" if vid else (
                        "audio" if probe["is_audio"] else "video")
                    self._air_is_live = (bool(probe["is_live"])
                                         and self._air_kind == "video")

                self._ui(self.set_status, f"Connecting to {dev.name}...")
                atv = await pyatv.connect(dev.key, self.loop_thread.loop,
                                          protocol=Protocol.RAOP)
                self.atv = atv
                self._atvs[dev.label] = atv
                if shutdown.is_set():
                    raise asyncio.CancelledError()

                # Stream/restart loop: pause cancels the stream task and waits
                # on `wake` while KEEPING the RAOP session alive; resume/seek
                # sets `wake` and the loop reopens the source (with seek).
                while not shutdown.is_set():
                    stream = (source.open_wav_reader() if source is not None
                              else await self._raop_source(url, vid, dev.label))
                    if self._air_play_t0 is None:
                        self._air_play_t0 = time.monotonic()
                    self._air_state = "playing"
                    wake.clear()
                    trace("air.stream.start", dev.name)
                    self._ui(self.set_status, f"Streaming to {dev.name}...")
                    stream_task = asyncio.create_task(
                        atv.stream.stream_file(stream)
                    )
                    # Transport controls address the current primary session;
                    # each runner keeps its own task so another AirPlay room
                    # cannot cancel or await the wrong stream.
                    if self.atv is atv:
                        self.stream_task = stream_task
                    stop_wait = asyncio.create_task(shutdown.wait())
                    wake_wait = asyncio.create_task(wake.wait())
                    try:
                        done, pending = await asyncio.wait(
                            [stream_task, stop_wait, wake_wait],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        # Stop the stream task unless it already finished.
                        if not stream_task.done():
                            stream_task.cancel()
                        try:
                            await stream_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        if self.stream_task is stream_task:
                            self.stream_task = None
                        stream_task = None
                        if shutdown.is_set():
                            break
                        if wake.is_set():
                            wake.clear()
                            continue   # resume or seek: reopen the source
                        if self._air_state == "paused":
                            # Pause killed the source; wait for resume/seek
                            # to set wake, then reopen on the same session.
                            await wake.wait()
                            wake.clear()
                            continue
                        if self._stop_flag or shutdown.is_set():
                            break       # stop() already said "Stopped."
                        if (self._air_is_live
                                and self._air_kind in ("video", "live")):
                            # Live ffmpeg pipe: the source dropped (they do,
                            # every ten to twenty seconds) and ffmpeg exited
                            # -- there are no reconnect flags any more
                            # because the byte-offset resume spliced
                            # already-heard media in as skip-backs. Reopen
                            # at the live edge on the same RAOP session.
                            trace("air.stream.restart", "live pipe ended")
                            continue
                        # Stream ended naturally.
                        self._ui(lambda: self.set_status("Finished."))
                        break
                    finally:
                        stop_wait.cancel()
                        wake_wait.cancel()
                        self._kill_ffmpeg(dev.label)
            except asyncio.CancelledError:
                cancelled = True
            except Exception as exc:
                message = f"AirPlay error: {exc}"
                self._ui(self.set_status, message)
            finally:
                st, stream_task = stream_task, None
                if st and not st.done():
                    st.cancel()
                    try:
                        await st
                    except (asyncio.CancelledError, Exception):
                        pass
                if self.stream_task is st:
                    self.stream_task = None
                self._kill_ffmpeg(dev.label)
                if atv:
                    try:
                        await asyncio.gather(*atv.close())
                    except Exception:
                        pass
                if self._atvs.get(dev.label) is atv:
                    self._atvs.pop(dev.label, None)
                if self.atv is atv:
                    self.atv = None
                    self._air_state = "stopped"
                    self._air_wake = None
                    self._air_shutdown = None
                if cancelled:
                    self._ui(lambda: self.set_status("Stopped."))

        self._air_runner_futs = [fut for fut in self._air_runner_futs
                                 if not fut.done()]
        self._runner_fut = self.loop_thread.submit(runner())
        self._air_runner_futs.append(self._runner_fut)
        self.set_status(f"Starting to {dev.name}...")

    async def _raop_source(self, url: str, vid: Optional[str],
                           owner: Optional[str] = None):
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
        trace("air.ffmpeg.spawn")
        ffmpeg = _find_ffmpeg()
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
        # Survive IPTV source drops/jitter instead of feeding silence.
        cmd += [
            # A live stream has no byte positions to come back to, and this
            # source now IGNORES -seekable 0: on a drop it serves its buffer
            # from an earlier point and ffmpeg splices that in -- the overlap
            # is heard as the stream jumping back (measured 5.2x media per
            # wall-second, 2026-09-05). So no reconnect flags at all: a drop
            # ends the pipe and the RAOP runner's restart loop reopens at
            # the live edge. Only meaningful for http(s).
            *(("-seekable", "0") if url.lower().startswith(
                ("http://", "https://")) else ()),
            "-rw_timeout", "5000000",
            # Look at as little of the stream as it takes to find the audio.
            # The defaults inspect five seconds before emitting a byte, and
            # every one of those is silence at the start of a channel.
            "-analyzeduration", "1000000",
            "-probesize", "1000000",
        ]
        if self._pending_seek:
            # Input seeking: fast, lands on a keyframe close to the target.
            cmd += ["-ss", str(max(self._pending_seek - 2.0, 0.0))]
        cmd += [
            # +nobuffer and low_delay stop the demuxer holding packets back;
            # +genpts fills in timestamps the source omits.
            "-fflags", "+genpts+nobuffer",
            "-flags", "+low_delay",
            "-i", url,
            "-vn", "-map", "a:0?",          # audio track only
            # The receiver plays at its own crystal's pace and the channel
            # arrives at the source's; the two drift apart all day. async is
            # the budget for correcting that, in samples per second, and it
            # was 1 -- so the error grew until ffmpeg gave up and jumped,
            # which is heard as the stream skipping. 1000 lets it be
            # stretched away continuously instead, inaudibly. first_pts=0
            # pins the start so the first packet's timestamp, whatever the
            # channel says it is, does not lurch the stream on its way in.
            "-af", "aresample=44100:async=1000:first_pts=0",
            "-f", "wav", "-c:a", "pcm_s16le",
            "-flush_packets", "1",
            "-",                             # pipe to stdout
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
            **_no_window_kwargs(),
        )
        if owner is None:
            self._ffmpeg_proc = proc
        else:
            self._air_ffmpeg_procs[owner] = proc
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

    # ---- screen / app-window / system-audio casting ----

    def cast_screen(self) -> None:
        """Cast the whole desktop plus system audio to the selected device."""
        self._cast_capture("Screen")

    def cast_audio(self) -> None:
        """Cast this PC's sound with nothing else in the path.

        No screen capture and no video encoder means the delay is a capture
        period plus the receiver's own buffer, which is as close to realtime
        as this gets.
        """
        self._cast_capture("System audio", audio_only=True)

    def cast_window(self) -> None:
        """Pick a visible top-level window and cast it with system audio."""
        picks = list_windows()
        if not picks:
            self.set_status("No windows found.")
            return
        dlg = wx.SingleChoiceDialog(self, "Window:", "Cast app",
                                    [t for _, t in picks], wx.CHOICEDLG_STYLE)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        hwnd, title = picks[dlg.GetSelection()]
        dlg.Destroy()
        self._cast_capture(title, hwnd=hwnd)

    def _install_grabber_cache(self) -> None:
        """Let the screen-grabber probe remember its answer between launches.

        The probe hangs for its whole timeout on any box where ddagrab does
        not work, and a box where it does not work never starts working, so
        paying that once per install beats paying it once per launch. The
        answer is filed against the machine and session type, because that is
        what it actually depends on -- the same PC over RDP is a different
        answer.
        """
        settings = self.settings

        def load() -> str:
            if settings["screen_grabber_key"] != ce.grabber_machine_key():
                return ""
            return settings["screen_grabber"]

        def store(value: str) -> None:
            settings.update(screen_grabber=value,
                            screen_grabber_key=ce.grabber_machine_key())

        ce.grabber_cache_load = load
        ce.grabber_cache_store = store
        # Settle it now, off any connect path. Nothing waits on this.
        ce.prewarm_screen_grabber()

    def _capture_container(self, dev: Device, audio_only: bool) -> str:
        """The wire format `dev` can actually play.

        A receiver that cannot show a picture gets sound whatever was asked
        for -- Sonos is speakers, and AirPlay here is RAOP, which carries no
        video at all.
        """
        if audio_only or not dev.supports_video:
            return "wav"
        if dev.kind == "chromecast":
            return "mp4"        # progressive fragmented MP4
        if dev.kind == "roku":
            # Roku's media player handles MP4 and HLS but has no MPEG-TS.
            return "mp4"
        return "mpegts"         # DLNA renderers, TVs and Kodi

    def _capture_source(self, container: str, hwnd: int) -> ScreenSource:
        """A capture configured from the saved quality and audio settings."""
        chosen = preset(self.settings["capture_quality"])
        return ScreenSource(
            hwnd=hwnd, container=container,
            fps=chosen["fps"], bitrate=chosen["bitrate"],
            max_width=chosen["max_width"], max_height=chosen["max_height"],
            keyframe_seconds=chosen["keyframe_seconds"],
            audio_device=self.settings["capture_audio_device"],
            mic_device=self.settings["capture_mic_device"],
            av_offset_ms=int(self.settings["av_offset_ms"]))

    def _cast_capture(self, label: str, hwnd: int = 0,
                      audio_only: bool = False) -> None:
        """Start a live capture and hand it to every selected device.

        Receivers disagree about wire formats, so a selection spanning a
        Chromecast and a DLNA amplifier needs two encodes of the same
        capture. They are grouped by format and one source is started per
        format -- the ordinary single-device case is unchanged, and the
        mixed case costs an extra encoder rather than silently sending one
        of them something it cannot play.

        Starting a capture verifies the whole chain, which takes a few
        seconds, so this runs off the UI thread and its failures are
        reported rather than leaving a device that quietly plays nothing.
        """
        devices = self.selected_devices()
        if not devices:
            self.set_status("Pick a device first.")
            return
        self.stop_silent()
        devices = self._group_musiccast(self._group_sonos(devices))
        self.current = devices[0]
        self._targets = list(devices)
        self._stop_flag = False

        # AirPlay reads the capture tap directly rather than over HTTP, so it
        # needs a source to exist but not a stream to be served.
        airplay = [d for d in devices if d.kind == "airplay"]
        by_container: dict = {}
        for dev in devices:
            # A follower zone is fed by the unit's main zone, already woken
            # and aimed by _musiccast_prepare. It needs no stream of its own.
            if dev.kind in ("airplay", "musiccast"):
                continue
            by_container.setdefault(
                self._capture_container(dev, audio_only), []).append(dev)
        if airplay and not by_container:
            by_container["wav"] = []

        formats = ", ".join(sorted(by_container)) or "audio"
        names = ", ".join(d.name for d in devices)
        self.set_status(f"Starting {label.lower()} capture "
                        f"({formats}) for {names}...")

        # Published before anything starts, and appended to as sources come
        # up: stop() pressed midway must always find a handle to every capture
        # already running, including one started in the gap.
        started: list = []
        self._sources = started

        def worker() -> None:
            # Same reason as play(): this talks to the receiver over HTTP, so
            # it belongs on the worker and not on the UI thread.
            self._musiccast_prepare(devices)
            watched = []
            for container, group in by_container.items():
                try:
                    src = self._capture_source(container, hwnd)
                    # Nothing will connect to a source that only exists to
                    # feed AirPlay, so there is nothing to verify.
                    src.start(verify=bool(group))
                except Exception as exc:
                    for done in started:
                        done.stop()
                    message = f"Capture failed: {exc}"
                    self._ui(self.set_status, message)
                    return
                started.append(src)
                if self._stop_flag:
                    for done in started:
                        done.stop()
                    return
                for dev in group:
                    self._ui(self._dispatch, dev, src.url, src.mime, True,
                             f"{APP_TITLE}: {label}")
                if group:
                    watched.append(src)
            self._cast_started = time.monotonic()
            for dev in airplay:
                self._ui(self._play_airplay, dev, "", started[0])
            for dev in devices:
                self.settings.note_device(dev.label)
            # The receiver's own connection is what proves the chain works.
            # Waiting on it here rather than before dispatch means the check
            # overlaps the receiver connecting instead of delaying it -- and
            # costs no second encoder to perform.
            for src in watched:
                try:
                    src.wait_for_media()
                except Exception as exc:
                    if self._stop_flag or src not in self._sources:
                        return       # stopped while waiting; not a failure
                    self._ui(self.set_status, f"Capture failed: {exc}")
                    return

        threading.Thread(target=worker, daemon=True,
                         name="caster-capture").start()

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
        self._cast_started = time.monotonic()
        name = os.path.basename(path)
        self.set_status(f"Serving {name}...")
        if dev.kind == "chromecast":
            self._play_chromecast(dev, url)
        elif dev.kind == "upnp":
            self._play_upnp(dev, url)
        elif dev.kind == "sonos":
            self._play_sonos(dev, url, server.mime, name)
        elif dev.kind == "roku":
            self._play_roku(dev, url, server.mime, name)
        elif dev.kind == "kodi":
            self._play_kodi(dev, url)
        elif dev.kind == "airplay":
            self._play_airplay(dev, url)
        else:
            # MusicCast zones are control-only followers, not transports.
            self.set_status(
                "Select a playback device; a MusicCast zone follows it.")

    # ---- teardown ----

    def stop_silent(self) -> None:
        trace("stop", "")
        self._stop_flag = True
        task = self.stream_task
        if task and not task.done():
            self.loop_thread.submit(self._cancel_stream(task))
        for rf in self._air_runner_futs:
            if not rf.done():
                # A runner may still be connecting.  Cancel every selected
                # receiver, not only the last one whose future was recorded.
                rf.cancel()
        self._kill_ffmpeg()
        self._stop_relay()
        for src in self._sources:
            try:
                src.stop()
            except Exception:
                pass
        self._sources = []
        self._cast_started = 0.0
        # Every device that was being cast to, not just the primary one:
        # otherwise a multi-room cast leaves the other speakers running with
        # nothing left to feed them.
        for dev in (self._targets or ([self.current] if self.current else [])):
            try:
                if dev.kind == "upnp":
                    upnp_stop(dev.key["control_url"])
                elif dev.kind == "sonos":
                    sonos_stop(dev.key["ip"])
                elif dev.kind == "roku":
                    roku_stop(dev.key["base"])
                elif dev.kind == "kodi":
                    kodi_stop(dev.key["base"], self._kodi_auth())
            except Exception:
                pass
        grouped, self._mc_grouped = list(self._mc_grouped), []
        if grouped:
            # Nothing waits on this, and stop() is pressed on the UI thread.
            threading.Thread(
                target=lambda: musiccast_ungroup(grouped), daemon=True,
                name="musiccast-ungroup").start()
        self._musiccast_restore()
        self._targets = []
        # Disconnect every Chromecast, not just the most recent one.
        # Multi-room casts create several, and self.cast only holds one.
        casts, self._casts = dict(self._casts), {}
        self._cast_live_loads = {}
        for cast in casts.values():
            try:
                cast.stop_app()
            except Exception:
                pass
            try:
                # Without this the socket client thread outlives the play and
                # keeps the connection open, once per device cast to.
                cast.disconnect(blocking=False)
            except Exception:
                pass
        # Also handle self.cast for code that still writes to it directly.
        cast, self.cast = self.cast, None
        if cast and cast not in list(casts.values()):
            try:
                cast.stop_app()
            except Exception:
                pass
            try:
                cast.disconnect(blocking=False)
            except Exception:
                pass
        self._atvs.clear()

    async def _cancel_stream(self, task: asyncio.Task) -> None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    def _kill_ffmpeg(self, owner: Optional[str] = None) -> None:
        if owner is None:
            proc = self._ffmpeg_proc
            self._ffmpeg_proc = None
        else:
            proc = self._air_ffmpeg_procs.pop(owner, None)
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass

    def _keep_relay(self, relay) -> None:
        """Hold a relay for teardown, stopping any it displaces.

        There is one attribute and there can be several receivers: casting one
        TS url to two UPnP renderers built two relays, and the second quietly
        replaced the first -- leaving an ffmpeg and an HTTP server running
        with nothing able to stop them.
        """
        with self._relay_lock:
            self._relays.append(relay)
            self._relay = relay

    def _stop_relay(self, only=None) -> None:
        """Stop relays.  With *only*, remove and stop a single relay; without
        it, tear everything down (called by stop_silent)."""
        with self._relay_lock:
            if only is not None:
                if only in self._relays:
                    self._relays.remove(only)
                    if self._relay is only:
                        self._relay = self._relays[-1] if self._relays else None
                relay = only
            else:
                relays, self._relays = list(self._relays), []
                self._relay = None
                fs = self._file_server
                self._file_server = None
                for relay in relays:
                    try:
                        relay.stop()
                    except Exception:
                        pass
                if fs:
                    fs.stop()
                return
        if relay:
            try:
                relay.stop()
            except Exception:
                pass

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
                    self.btn_playpause.SetLabel("&Resume")
                    self.set_status("Paused.")
                else:
                    mc.play()
                    self.btn_playpause.SetLabel("Pa&use")
                    self.set_status("Playing.")
            except Exception as exc:
                self.set_status(f"Error: {exc}")
            return
        if self.atv:
            self._airplay_pause()
            return
        primary = self.current
        if primary and primary.kind == "kodi":
            try:
                kodi_pause(primary.key["base"], self._kodi_auth())
                self.set_status("Play or pause sent to Kodi.")
            except Exception as exc:
                self.set_status(f"Kodi error: {exc}")
            return
        if primary and primary.kind == "roku":
            try:
                roku_key(primary.key["base"], "Play")
                self.set_status("Play or pause sent to Roku.")
            except Exception as exc:
                self.set_status(f"Roku error: {exc}")
            return
        if primary and primary.kind in ("upnp", "sonos"):
            # A live capture has nothing to pause into: there is no buffer
            # to resume from, so stopping is the honest action.
            self.set_status("This device has no pause; use Stop.")
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
            # Cancel the stream task so that direct-audio URLs and live
            # capture sources (which have no ffmpeg to kill) also stop.
            task = self.stream_task
            if task and not task.done():
                self.loop_thread.submit(self._cancel_stream(task))
            self.btn_playpause.SetLabel("&Resume")
            self.set_status("Paused.")
        elif self._air_state == "paused" and self._air_wake:
            self._air_state = "playing"
            self._air_play_t0 = time.monotonic()
            self.loop_thread.submit(self._set_event(self._air_wake))
            self.btn_playpause.SetLabel("Pa&use")
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

    def _sync_volume_slider(self, level: int) -> None:
        """Show the receiver's real volume without sending it back."""
        self._updating_slider = True
        try:
            self.vol_slider.SetValue(max(0, min(100, int(level))))
            self.settings.set("volume", int(level))
        finally:
            self._updating_slider = False

    def _on_volume(self, event) -> None:
        if self._updating_slider:
            return          # we moved it to match the device, not the user
        self.apply_volume(self.vol_slider.GetValue(), user=True)

    def apply_volume(self, level: int, remember: bool = True,
                     user: bool = False) -> None:
        """Set the volume on every device being cast to.

        Each protocol has its own way of being told, and a room is only
        usefully quieter if all of it gets quieter.

        `user` marks a change the person actually asked for. Only those reach
        a MusicCast receiver: its scale is its own (161 steps here, not 100),
        so a remembered slider position replayed at it is not "the volume they
        had" but a fraction of a completely different range -- and 100 means
        maximum output on an amplifier wired to real speakers.
        """
        level = max(0, min(100, int(level)))
        if remember:
            self.settings.set("volume", level)
        try:
            if self.cast:
                self.cast.set_volume(level / 100.0)
            if self.atv:
                self.loop_thread.submit(_set_atv_volume(self.atv, float(level)))
        except Exception:
            pass

        def worker() -> None:
            for dev in list(self._targets):
                try:
                    # MusicCast first: it is the receiver's own volume, on the
                    # receiver's own scale (161 steps here, not 100), so it
                    # lands on a real step and matches the front panel. It is
                    # also the only way to reach a zone that has no transport
                    # of its own, and the only volume an AirPlay target has
                    # that survives the stream ending.
                    if dev.musiccast:
                        if not user:
                            continue    # never impose a level nobody chose
                        mc = dev.musiccast
                        if yxc_set_volume(mc["host"], level, mc["zone"]):
                            continue
                    if dev.kind == "sonos":
                        sonos_set_volume(dev.key["ip"], level)
                    elif dev.kind == "kodi":
                        kodi_set_volume(dev.key["base"], level,
                                        self._kodi_auth())
                    elif dev.kind == "upnp":
                        upnp_set_volume(dev.key["control_url"], level)
                except Exception:
                    pass
        threading.Thread(target=worker, daemon=True, name="volume").start()

    def speak_volume(self) -> str:
        """The volume as the receiver itself shows it, when it can say.

        A percentage is a guess at someone else's scale; "-35.5 dB" is the
        number on the front panel and on the remote.
        """
        for dev in (self._targets or []):
            if dev.musiccast:
                shown = yxc_volume_db(dev.musiccast["host"],
                                      dev.musiccast["zone"])
                if shown:
                    return shown
        return ""

    def nudge_volume(self, delta: int) -> None:
        level = max(0, min(100, self.vol_slider.GetValue() + delta))
        self.vol_slider.SetValue(level)
        self.apply_volume(level, user=True)
        self.set_status(f"Volume {level} percent.")

        def announce() -> None:
            # Read back rather than compute: the receiver rounds to its own
            # step, so the number it shows is not the one that was sent.
            shown = self.speak_volume()
            if shown:
                self._ui(self.set_status, f"Volume {level} percent, {shown}.")
        if self._musiccast_targets():
            threading.Thread(target=announce, daemon=True,
                             name="volume-readback").start()

    def _musiccast_mute(self, muted: bool) -> None:
        """Mute every MusicCast zone, off the UI thread.

        Mute is on a global hotkey, and every one of these is an HTTP round
        trip to a receiver that may be asleep or off the network. Done inline
        that is a window that stops answering the moment the key is pressed.
        """
        targets = self._musiccast_targets()
        if not targets:
            return

        def worker() -> None:
            for host, zone in targets:
                try:
                    yxc_set_mute(host, muted, zone)
                except Exception:
                    pass
        threading.Thread(target=worker, daemon=True,
                         name="musiccast-mute").start()

    def toggle_mute(self) -> None:
        """Silence the receivers without ending the cast.

        Restoring the previous level rather than a fixed one matters: coming
        back from mute to full volume in a quiet house is unpleasant.
        """
        if self._muted:
            self._muted = False
            self.vol_slider.SetValue(self._pre_mute_volume)
            self._musiccast_mute(False)
            # A MusicCast receiver has a real mute, already released above, so
            # its level was never touched and must not be rewritten here.
            self.apply_volume(self._pre_mute_volume,
                              user=not self._musiccast_targets())
            self.set_status(f"Unmuted, volume {self._pre_mute_volume} percent.")
        else:
            self._muted = True
            self._pre_mute_volume = self.vol_slider.GetValue()
            self.vol_slider.SetValue(0)
            # A receiver that has a real mute gets it: winding the volume to
            # zero and back walks the amplifier through every step in between,
            # and on a MusicCast unit that is audible.
            self._musiccast_mute(True)
            self.apply_volume(0, remember=False,
                              user=not self._musiccast_targets())
            self.set_status("Muted.")
        # Roku has no volume API, only the remote's own keys.
        for dev in list(self._targets):
            if dev.kind == "roku":
                try:
                    roku_key(dev.key["base"], "VolumeMute")
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

    def _on_watchdog(self, event) -> None:
        """Slow housekeeping: the sleep timer, and keeping a cast alive."""
        self._check_sleep_timer()
        if self.settings["auto_reconnect"]:
            self._check_reconnect()

    def _check_sleep_timer(self) -> None:
        minutes = int(self.settings["sleep_timer_minutes"])
        if not minutes or not self._cast_started:
            return
        if time.monotonic() - self._cast_started >= minutes * 60:
            self.stop_silent()
            self.set_status(f"Stopped after {minutes} minutes.")

    def _check_reconnect(self) -> None:
        """Put a cast back that ended without being asked to.

        Only the two cases that can actually be detected are handled. A
        Chromecast reports going idle, and a Sonos reports its transport
        state; everything else pulls the stream over HTTP, where a device
        that goes away simply closes the connection and there is nothing
        left to ask.
        """
        if self._stop_flag:
            return
        self._recover_live_casts()
        if not self._sources or not self._targets:
            return
        source = self._sources[0]
        if self.cast:
            try:
                if (self.cast.media_controller.status.player_state == "IDLE"
                        and self.current
                        and self.current.label not in self._cast_live_loads):
                    self.set_status("Receiver dropped; reconnecting...")
                    self._dispatch(self.current, source.url, source.mime, True)
                    return
            except Exception:
                pass
        self._check_sonos_resync(source)

    def _recover_live_casts(self) -> None:
        """Reload an ended live URL using its existing relay and connection."""
        for label, load in list(self._cast_live_loads.items()):
            cast, url, mime, stream_type = load
            if label in self._cast_recovering:
                continue
            try:
                status = cast.media_controller.status
                if status is None or status.player_state != "IDLE":
                    continue
            except Exception as exc:
                # Status arrives asynchronously.  A socket that is briefly
                # unavailable must not let this wx timer callback escape --
                # the next watchdog tick can still recover the same load.
                trace("cast.recover.status_failed", type(exc).__name__)
                continue
            self._cast_recovering.add(label)

            def recover(label=label, load=load):
                cast, url, mime, stream_type = load
                try:
                    self._ensure_receiver(cast)
                    if (self._stop_flag
                            or self._cast_live_loads.get(label) is not load):
                        return
                    mc = cast.media_controller
                    before = getattr(mc.status, "media_session_id", None)
                    trace("cast.recover", label)
                    mc.play_media(url, mime, stream_type=stream_type)
                    mc.block_until_active(15)
                    if (self._await_playing(mc, before)
                            and not self._stop_flag
                            and self._cast_live_loads.get(label) is load):
                        self._ui(self.set_status, f"Reconnected to {label}.")
                except Exception as exc:
                    trace("cast.recover.failed", type(exc).__name__)
                finally:
                    self._cast_recovering.discard(label)

            threading.Thread(target=recover, daemon=True,
                             name="cast-recover").start()

    def _check_sonos_resync(self, source) -> None:
        """Periodically hand Sonos a fresh connection.

        A Sonos connection left open for hours drifts: the delay between
        this machine and the speaker creeps upward with nothing on this side
        causing it, and only a new connection resets it. Reconnecting on a
        schedule buys that reset for a brief re-buffering gap instead of
        requiring the app to be restarted.
        """
        hours = int(self.settings["sonos_resync_hours"])
        if not hours or not self._cast_started:
            return
        if time.monotonic() - self._cast_started < hours * 3600:
            return
        sonos = [d for d in self._targets if d.kind == "sonos"]
        if not sonos:
            return
        self._cast_started = time.monotonic()
        for dev in sonos:
            self._play_sonos(dev, source.url, source.mime,
                             f"{APP_TITLE}: system audio")
        self.set_status("Refreshed the Sonos connection.")

    def _reset_sliders(self) -> None:
        self._updating_slider = True
        self.pos_slider.SetValue(0)
        self._updating_slider = False

    # ---------- shutdown ----------

    def _on_close(self, event) -> None:
        # Closing to the tray keeps the hotkeys live, which is the point of
        # having them: casting the PC's sound should not need this window.
        if (self.settings["minimise_to_tray"] and event.CanVeto()
                and self._ensure_tray()):
            self.Hide()
            event.Veto()
            return
        try:
            self.settings.set("volume", self.vol_slider.GetValue())
            self.hotkeys.unregister_all()
            self.status_timer.Stop()
            self.watchdog_timer.Stop()
            if self._tray:
                self._tray.RemoveIcon()
                self._tray.Destroy()
                self._tray = None
        except Exception:
            pass
        try:
            self.stop_silent()
            for fut in self._air_runner_futs:
                if fut and not fut.done():
                    try:
                        # Wait for every runner to cancel its stream and
                        # close its own receiver before the loop stops.
                        fut.result(timeout=8)
                    except Exception:
                        pass
        finally:
            zc, self._cast_zc = self._cast_zc, None
            if zc:
                try:
                    zc.close()
                except Exception:
                    pass
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
    if not frame.settings["discover_on_launch"]:
        frame.set_status("Ready. Ctrl+D to scan.")
    app.MainLoop()


if __name__ == "__main__":
    main()
