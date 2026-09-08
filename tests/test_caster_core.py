"""Offline unit tests for the casting core in caster.py.

Every test here guards a failure that was actually observed on real
hardware, and every one runs without a network, a device or ffmpeg: the
things that talk to the outside world (`_find_ffmpeg`, `pick_h264_encoder`,
`_probe_codecs`) are either monkeypatched or bypassed by passing the answer
in, and the only real I/O is writing small files under `tmp_path`.

The whole module sleeps well under a second in total: the two places that
wait (`_await_playing`, `_ensure_receiver`) are driven by fakes that reach
their terminal state within a couple of poll intervals.
"""
from __future__ import annotations

import functools
import http.client
import http.server
import io
import os
import socket
import threading
import time
import types

import pytest

import caster
from caster import Device, HlsFileHandler, HlsRelay, MainFrame, probe_media


LIVE_HLS = b'#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nsegment.ts\n'


def _ts_bytes(packets=8, stride=188):
    return b''.join(b'\x47' + bytes(stride - 1) for _ in range(packets))


def test_mpegts_detected_when_the_stream_starts_mid_packet():
    """A live server joins you mid-flight; byte 0 is not a packet boundary.

    Demanding the sync byte at byte 0 misread one provider's live channels
    as audio/mpeg, and a live channel read as VOD loses the piped reader,
    the replay dedupe and the under-feed rotation at once.
    """
    assert caster._looks_like_mpegts(_ts_bytes()[77:])
    assert caster._looks_like_mpegts(_ts_bytes(stride=192)[100:])
    assert caster._looks_like_mpegts(_ts_bytes()[:193])   # aligned and short


@pytest.mark.parametrize('blob', [
    b'G' + b'ood morning everyone. ' * 100,
    b'\xff\xfb' + bytes(2046),
    b'\x47' + bytes(200),
], ids=['text-starting-G', 'mp3-frame', 'too-short-to-prove-a-stride'])
def test_mpegts_detection_still_refuses_a_coincidence(blob):
    assert not caster._looks_like_mpegts(blob)


def test_probe_retries_past_a_load_balancer_that_says_nothing(monkeypatch):
    """One reply is too much weight when some CDN nodes answer with nothing.

    Measured against one provider: HTTP 503s in clusters and HTTP 200 with a
    zero-byte body, mixed with nodes serving proper MPEG-TS.
    """
    replies = [TimeoutError('503'), b'', b'', _ts_bytes(12)]
    def flaky(req, timeout=None):
        item = replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item, 'video/mp4')
    monkeypatch.setattr(caster.urllib.request, 'urlopen', flaky)
    monkeypatch.setattr(caster.time, 'sleep', lambda *_: None)
    out = caster.probe_media('https://example.invalid/u/p/12345')
    assert out['mime'] == 'video/mp2t'
    assert out['is_live'] is True


def test_probe_never_calls_a_stream_vod_on_no_evidence(monkeypatch):
    """When every attempt fails, the URL guess must not assert VOD.

    guess_mime reads an extensionless portal URL as audio/mpeg out of thin
    air, and that verdict switched off every live protection.
    """
    monkeypatch.setattr(caster.urllib.request, 'urlopen',
                        lambda *a, **kw: (_ for _ in ()).throw(TimeoutError('down')))
    monkeypatch.setattr(caster.time, 'sleep', lambda *_: None)
    out = caster.probe_media('https://example.invalid/u/p/12345')
    assert out['is_live'] is True


class _FakeResponse:
    """Enough of urlopen's result for probe_media: a body and a header."""

    def __init__(self, body, content_type=''):
        self._body = body
        self.headers = {'Content-Type': content_type}

    def read(self, n=None):
        return self._body[:n] if n else self._body

    def geturl(self):
        return 'https://example.invalid/u/p/12345'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_native_hls_checks_body_and_preserves_query(monkeypatch):
    requested = []
    def open_playlist(req, timeout):
        requested.append((req.full_url, timeout))
        return io.BytesIO(LIVE_HLS)
    monkeypatch.setattr(caster.urllib.request, 'urlopen', open_playlist)
    source = 'https://example.invalid/live/user/password/123.ts?token=value'
    expected = source.replace('.ts?', '.m3u8?')
    assert caster._native_hls_url(source) == expected
    assert requested == [(expected, 5)]


@pytest.mark.parametrize('body', [b'<html>Not found</html>', b'#EXTM3U\n',
                                LIVE_HLS + b'#EXT-X-ENDLIST\n', b'x' * 65537],
                         ids=['html', 'empty', 'vod', 'oversized'])
def test_native_hls_rejects_non_live_or_invalid_responses(monkeypatch, body):
    monkeypatch.setattr(caster.urllib.request, 'urlopen',
                        lambda *a, **kw: io.BytesIO(body))
    assert caster._native_hls_url('http://example.invalid/u/p/123.ts') is None


LONG_SEG_HLS = (b'#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-MEDIA-SEQUENCE:7254\n'
                b'#EXT-X-TARGETDURATION:17\n'
                b'#EXTINF:11.386356,\n/hls/aaa\n'
                b'#EXTINF:16.690000,\n/hls/bbb\n')


def test_native_hls_rejected_when_its_segments_would_stall_the_receiver(monkeypatch):
    """A portal playlist is only worth preferring while it plays better.

    A receiver that reaches the live edge waits out the next segment, so
    segment length is the worst-case stall. One portal served 16.7s segments
    behind a TARGETDURATION of 17 while the relay served the same channel in
    steady 2.00s segments; preferring that feed was the buffering.
    """
    monkeypatch.setattr(caster.urllib.request, 'urlopen',
                        lambda *a, **kw: io.BytesIO(LONG_SEG_HLS))
    assert caster._native_hls_url('http://example.invalid/u/p/123.ts') is None


def test_native_hls_still_preferred_when_its_segments_are_normal(monkeypatch):
    """The gate must not switch the whole feature off.

    A playlist built to the usual 6s convention still wins: it costs no local
    encoder and no re-encode.
    """
    monkeypatch.setattr(caster.urllib.request, 'urlopen',
                        lambda *a, **kw: io.BytesIO(LIVE_HLS))
    assert caster._native_hls_url(
        'http://example.invalid/u/p/123.ts') == 'http://example.invalid/u/p/123.m3u8'


def test_native_hls_rejects_an_unparsable_duration(monkeypatch):
    """An EXTINF that is not a number leaves the relay as the safe answer."""
    body = b'#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:soon,\nsegment.ts\n'
    monkeypatch.setattr(caster.urllib.request, 'urlopen',
                        lambda *a, **kw: io.BytesIO(body))
    assert caster._native_hls_url('http://example.invalid/u/p/123.ts') is None


def test_native_hls_network_failure_keeps_ts_fallback(monkeypatch):
    def unavailable(*args, **kwargs):
        raise TimeoutError('source unavailable')
    monkeypatch.setattr(caster.urllib.request, 'urlopen', unavailable)
    assert caster._native_hls_url('http://example.invalid/u/p/123.ts') is None


@pytest.mark.parametrize('url', ['C:/recording.ts', 'http://example.invalid/123.ts',
                               'http://example.invalid/u/p/123.mp4',
                               'http://example.invalid/u/p/segment.ts'])
def test_native_hls_does_not_probe_unrelated_urls(monkeypatch, url):
    def unexpected(*args, **kwargs):
        raise AssertionError('unexpected network request')
    monkeypatch.setattr(caster.urllib.request, 'urlopen', unexpected)
    assert caster._native_hls_url(url) is None


@pytest.mark.parametrize('reject_native', [False, True])
def test_cast_prefers_native_hls_with_receiver_fallback(frame, monkeypatch, reject_native):
    loaded = []
    kept = []
    source = 'http://example.invalid/u/p/123.ts'
    native = source[:-3] + '.m3u8'
    mc = types.SimpleNamespace(
        status=types.SimpleNamespace(media_session_id=1),
        play_media=lambda url, mime, **kw: loaded.append(url),
        block_until_active=lambda timeout: None)
    cast = types.SimpleNamespace(media_controller=mc, app_id=frame.CAST_APP_ID,
                                 status=types.SimpleNamespace(volume_level=None),
                                 wait=lambda timeout: None)
    monkeypatch.setattr(caster.pychromecast, 'Chromecast', lambda *a, **kw: cast)
    monkeypatch.setattr(caster, '_native_hls_url', lambda url: native)
    monkeypatch.setattr(caster, 'probe_media',
                        lambda url: {'mime': 'video/mp2t', 'is_live': True})
    monkeypatch.setattr(caster.threading, 'Thread', lambda target, args=(), **kw:
                        types.SimpleNamespace(start=lambda: target(*args),
                                              join=lambda **kw: None))
    frame._cast_zeroconf = lambda: None
    frame._ensure_receiver = lambda cast: None
    results = iter([False, True] if reject_native else [True])
    frame._await_playing = lambda *args: next(results)
    frame._ui = lambda *a, **kw: None
    frame.set_status = lambda *a, **kw: None
    relay = types.SimpleNamespace(start=lambda: 'http://relay/live.m3u8')
    frame._make_relay = lambda *a, **kw: relay
    frame._keep_relay = kept.append
    frame._stop_relay = lambda *a: None
    dev = Device('chromecast', 'TV', {'host': '192.0.2.1', 'uuid': '0' * 32})
    frame._play_chromecast(dev, source)
    assert loaded == ([native, 'http://relay/live.m3u8'] if reject_native else [native])
    assert kept == ([relay] if reject_native else [])
    assert frame._cast_live_loads[dev.label][1] == loaded[-1]


@pytest.mark.parametrize('state,expected', [('IDLE', 1), ('PLAYING', 0), ('BUFFERING', 0)])
def test_live_url_recovers_without_capture_sources(frame, monkeypatch, state, expected):
    calls = []
    mc = types.SimpleNamespace(
        status=types.SimpleNamespace(player_state=state, media_session_id=7),
        play_media=lambda *args, **kw: calls.append((args, kw)),
        block_until_active=lambda timeout: None)
    cast = types.SimpleNamespace(media_controller=mc)
    frame._cast_live_loads['TV'] = (cast, 'http://relay/live.m3u8', 'application/vnd.apple.mpegurl', 'LIVE')
    frame._ensure_receiver = lambda cast: None
    frame._await_playing = lambda mc, before: True
    frame._ui = lambda *args, **kw: None
    monkeypatch.setattr(caster.threading, 'Thread',
                        lambda target, **kw: types.SimpleNamespace(start=target))
    frame._check_reconnect()
    assert len(calls) == expected
    if calls:
        assert calls[0][0][0] == 'http://relay/live.m3u8'
    assert not frame._cast_recovering


def test_live_url_recovery_does_not_reload_after_stop(frame, monkeypatch):
    calls = []
    mc = types.SimpleNamespace(status=types.SimpleNamespace(player_state='IDLE'),
                               play_media=lambda *args, **kw: calls.append(args))
    cast = types.SimpleNamespace(media_controller=mc)
    frame._cast_live_loads['TV'] = (cast, 'http://relay/live.m3u8', 'application/vnd.apple.mpegurl', 'LIVE')
    frame._ensure_receiver = lambda cast: frame._cast_live_loads.clear()
    monkeypatch.setattr(caster.threading, 'Thread',
                        lambda target, **kw: types.SimpleNamespace(start=target))
    frame._recover_live_casts()
    assert not calls
    assert not frame._cast_recovering


def test_live_url_recovery_tolerates_a_missing_receiver_status(frame):
    """A transient Cast disconnect must not crash the watchdog timer."""
    mc = types.SimpleNamespace(status=None)
    cast = types.SimpleNamespace(media_controller=mc)
    frame._cast_live_loads['TV'] = (
        cast, 'http://relay/live.m3u8', 'application/vnd.apple.mpegurl', 'LIVE')
    frame._recover_live_casts()
    assert not frame._cast_recovering


@pytest.mark.parametrize("kind,key,expected", [
    ("sonos", {"ip": "192.0.2.10"}, "sonos"),
    ("roku", {"base": "http://192.0.2.11:8060"}, "roku"),
    ("kodi", {"base": "http://192.0.2.12:8080"}, "kodi"),
])
def test_cast_file_uses_the_selected_receiver_protocol(
        frame, monkeypatch, kind, key, expected):
    """A local file must not fall through to AirPlay for every non-TV."""
    dev = Device(kind, "Receiver", key)
    calls = []
    server = types.SimpleNamespace(
        start=lambda: "http://192.0.2.1:9000/movie.mp4", mime="video/mp4")
    monkeypatch.setattr(caster, "FileServer", lambda path: server)
    frame.selected_device = lambda: dev
    frame.stop_silent = lambda: None
    frame.set_status = lambda *args, **kwargs: None
    frame._play_sonos = lambda *args: calls.append("sonos")
    frame._play_roku = lambda *args: calls.append("roku")
    frame._play_kodi = lambda *args: calls.append("kodi")
    frame._play_airplay = lambda *args: calls.append("airplay")
    frame.cast_file("C:/media/movie.mp4")
    assert calls == [expected]


def test_cast_file_explains_that_a_musiccast_zone_is_not_a_transport(
        frame, monkeypatch):
    dev = Device("musiccast", "Zone 2", {"host": "192.0.2.65"})
    messages = []
    server = types.SimpleNamespace(start=lambda: "http://192.0.2.1:9000/a.wav",
                                   mime="audio/wav")
    monkeypatch.setattr(caster, "FileServer", lambda path: server)
    frame.selected_device = lambda: dev
    frame.stop_silent = lambda: None
    frame.set_status = lambda message, *args, **kwargs: messages.append(message)
    frame.cast_file("C:/media/a.wav")
    assert messages[-1] == "Select a playback device; a MusicCast zone follows it."


def test_airplay_discovery_only_lists_unpaired_raop_receivers(frame, monkeypatch):
    """An AirPlay-only or pairing-required device cannot play RAOP audio."""
    def service(protocol, pairing):
        return types.SimpleNamespace(protocol=protocol, pairing=pairing)
    usable = types.SimpleNamespace(
        name="R&B Room",
        services=[service(caster.Protocol.RAOP,
                          caster.PairingRequirement.NotNeeded)],
        device_info="")
    pairing_required = types.SimpleNamespace(
        name="Living Room",
        services=[service(caster.Protocol.RAOP,
                          caster.PairingRequirement.Mandatory)],
        device_info="")
    airplay_only = types.SimpleNamespace(
        name="Samsung",
        services=[service(caster.Protocol.AirPlay,
                          caster.PairingRequirement.Mandatory)],
        device_info="")
    result = types.SimpleNamespace(
        result=lambda timeout: [usable, pairing_required, airplay_only])
    monkeypatch.setattr(caster.pyatv, "scan", lambda *args, **kwargs: object())
    monkeypatch.setattr(caster.asyncio, "run_coroutine_threadsafe",
                        lambda *args, **kwargs: result)
    frame.loop_thread = types.SimpleNamespace(loop=object())
    found = frame._scan_airplay()
    assert list(found) == ["R&B Room"]
    assert found["R&B Room"].key is usable


def test_airplay_runner_only_kills_its_own_ffmpeg_process(frame):
    """One AirPlay room ending must not silence another room's stream."""
    class Proc:
        returncode = None

        def __init__(self):
            self.kills = 0

        def kill(self):
            self.kills += 1

    first, second = Proc(), Proc()
    frame._air_ffmpeg_procs = {"First (AirPlay)": first,
                               "Second (AirPlay)": second}
    MainFrame._kill_ffmpeg(frame, "First (AirPlay)")
    assert first.kills == 1
    assert second.kills == 0
    assert frame._air_ffmpeg_procs == {"Second (AirPlay)": second}


# --------------------------------------------------------------------------
# 1. probe_media -- local files only, no network
# --------------------------------------------------------------------------

def _write(tmp_path, name: str, data: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


#: A minimal but honest MPEG-TS head: sync byte at 0 and again at 188.
TS_HEAD = b"\x47" + b"\x00" * 187 + b"\x47" + b"\x00" * 8


def test_probe_media_local_returns_the_full_contract(tmp_path, no_network):
    """probe_media's four keys are read positionally all over the play paths.

    _play_upnp, _play_chromecast and _cast_capture all index url/mime/
    is_audio/is_live straight off this dict. A local file that silently
    dropped one of them (is_audio stayed None, say) sent an audio box a
    video container and it played nothing.
    """
    path = _write(tmp_path, "clip.mp4", b"\x00\x00\x00\x18ftypisom")
    info = probe_media(path)
    assert set(info) == {"url", "mime", "is_audio", "is_live"}
    assert info["url"] == path
    assert info["is_audio"] is False
    assert info["is_live"] is False
    assert info["mime"] == "video/mp4"


def test_probe_media_local_detects_mpegts_by_magic_bytes(tmp_path, no_network):
    """A local .ts recording must be recognised as MPEG-TS, not guessed at.

    Windows' mimetypes maps .ts to TypeScript on plenty of boxes (see
    AGENTS.md), so extension alone would hand a Chromecast a text/* mime and
    the load is refused. The 0x47 sync byte is the only reliable signal.
    """
    path = _write(tmp_path, "channel.ts", TS_HEAD)
    info = probe_media(path)
    assert info["mime"] == "video/mp2t"
    assert info["is_audio"] is False
    assert info["is_live"] is False   # a file on disk is never live


def test_probe_media_local_magic_beats_a_lying_extension(tmp_path, no_network):
    """Magic bytes outrank the name.

    IPTV recordings get saved with whatever extension the portal offered.
    Believing ".mp3" for a file that is really a transport stream sent it to
    an audio-only path and produced silence.
    """
    path = _write(tmp_path, "recording.mp3", TS_HEAD)
    info = probe_media(path)
    assert info["mime"] == "video/mp2t"
    assert info["is_audio"] is False


@pytest.mark.parametrize("name,body", [
    ("notes.txt", b"GET /index.html HTTP/1.1\r\n"),
    ("caption.srt", b"Good evening, and welcome.\n"),
    ("picture.gif", b"GIF89a" + b"\x00" * 40),
])
def test_probe_media_local_needs_more_than_one_sync_byte(tmp_path, no_network,
                                                         name, body):
    """0x47 is also the letter G, and one byte is not a transport stream.

    Both branches now confirm the sync byte repeats at the packet stride --
    188 bytes, or 192 for M2TS. Without that, any local file merely beginning
    with "G" was reported as video/mp2t and sent down the live-remux path:
    a text file, a subtitle, a GIF.
    """
    path = _write(tmp_path, name, body)
    assert probe_media(path)["mime"] != "video/mp2t"


@pytest.mark.parametrize("name,body,mime,is_audio", [
    ("song.mp3", b"not really an mp3 but not a TS either", "audio/mpeg", True),
    ("tone.wav", b"RIFF____WAVEfmt ", "audio/wav", True),
    ("movie.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4", False),
    ("mystery.bin", b"nothing recognisable here", "application/octet-stream",
     False),
])
def test_probe_media_local_falls_back_to_the_extension(
        tmp_path, no_network, name, body, mime, is_audio):
    """Without a magic match the extension has to decide audio-ness.

    is_audio drives _capture_container and the wav fast path; a file whose
    is_audio came back None made `if info["is_audio"]` fall to the video
    branch and pushed a video mime at a stereo amplifier.
    """
    info = probe_media(_write(tmp_path, name, body))
    assert info["mime"] == mime
    assert info["is_audio"] is is_audio
    assert info["is_live"] is False


# --------------------------------------------------------------------------
# 2. HlsRelay.trailing_playlist -- the sequence ratchet
# --------------------------------------------------------------------------

def _playlist(seq: int, first: int, count: int, target: int = 2) -> str:
    """A synthetic live.m3u8 with `count` segments, exactly as ffmpeg lays
    one out: header, MEDIA-SEQUENCE last, then EXTINF/URI pairs."""
    lines = ["#EXTM3U",
             "#EXT-X-VERSION:3",
             f"#EXT-X-TARGETDURATION:{target}",
             f"#EXT-X-MEDIA-SEQUENCE:{seq}"]
    for n in range(first, first + count):
        lines += [f"#EXTINF:{target}.000000,", f"seg{n:05d}.ts"]
    return "\n".join(lines) + "\n"


@pytest.fixture
def relay(tmp_path):
    """A relay with a temp root and no ffmpeg, server or thread behind it.

    trail_seconds=0 leaves the receiver's own 3x TARGETDURATION floor as
    the only one, so these tests exercise segment-count trimming. The
    seconds-based cushion has its own tests.
    """
    r = HlsRelay("http://example.invalid/live.ts", hls_time=2,
                 prime_segments=3, trail_keep=8, trail_seconds=0)
    r.root = str(tmp_path)
    return r


def _serve(relay, text: str) -> str:
    """Write a playlist and return what the relay would hand the receiver."""
    with open(os.path.join(relay.root, "live.m3u8"), "w",
              encoding="utf-8", newline="") as f:
        f.write(text)
    out = relay.trailing_playlist()
    assert out is not None, "the relay refused to serve a playlist at all"
    return out.decode("utf-8")


def _seq_of(text: str) -> int:
    lines = [l for l in text.splitlines()
             if l.startswith("#EXT-X-MEDIA-SEQUENCE:")]
    assert len(lines) == 1, f"expected one MEDIA-SEQUENCE line, got {lines}"
    return int(lines[0].split(":", 1)[1])


def test_trailing_playlist_sequence_never_goes_backwards(relay):
    """The ratchet. An HLS client replays whatever it is renumbered under.

    ffmpeg is restarted in place when an IPTV source drops, and a restart
    numbers the playlist from wherever it likes -- including from zero. A
    served MEDIA-SEQUENCE lower than one the receiver has already seen is an
    instruction to play those segments again, which is heard as the stream
    jumping backwards. Nothing that leaves trailing_playlist may decrease.
    """
    seen = []
    # A normal live progression.
    for step in range(4):
        seen.append(_seq_of(_serve(relay, _playlist(100 + step, 100 + step, 12))))
    # ffmpeg restarts and renumbers backwards, with far fewer segments.
    seen.append(_seq_of(_serve(relay, _playlist(0, 0, 3))))
    # ...and then climbs again from its own new baseline.
    seen.append(_seq_of(_serve(relay, _playlist(1, 1, 4))))
    seen.append(_seq_of(_serve(relay, _playlist(2, 2, 12))))
    assert seen == sorted(seen), f"MEDIA-SEQUENCE went backwards: {seen}"


def _target_of(text: str) -> int:
    lines = [l for l in text.splitlines()
             if l.startswith("#EXT-X-TARGETDURATION:")]
    assert len(lines) == 1, f"expected one TARGETDURATION line, got {lines}"
    return int(lines[0].split(":", 1)[1])


def test_trailing_playlist_target_duration_never_shrinks(relay):
    """RFC 8216 4.3.3.1: TARGETDURATION must not change between reloads.

    ffmpeg recomputes it from whatever is in its own window, so a source
    whose GOP is irregular makes it oscillate -- measured 10 -> 6 -> 10 on
    one channel with keyframes 1.0s to 7.7s apart. A player sizes its buffer
    and its live-edge start distance from this number; handing it a smaller
    one than it has already acted on invites a rebuffer.
    """
    seen = [_target_of(_serve(relay, _playlist(100, 100, 12, target=10))),
            _target_of(_serve(relay, _playlist(101, 101, 12, target=6))),
            _target_of(_serve(relay, _playlist(102, 102, 12, target=2))),
            _target_of(_serve(relay, _playlist(103, 103, 12, target=10)))]
    assert seen == [10, 10, 10, 10], f"TARGETDURATION shrank: {seen}"


def test_trailing_playlist_target_duration_still_grows(relay):
    """The ratchet must not pin it below a genuinely longer segment.

    An EXTINF longer than TARGETDURATION is itself a violation, so when the
    source's GOP lengthens the served value has to follow it up.
    """
    seen = [_target_of(_serve(relay, _playlist(100, 100, 12, target=2))),
            _target_of(_serve(relay, _playlist(101, 101, 12, target=8)))]
    assert seen == [2, 8], f"TARGETDURATION failed to grow: {seen}"


def test_trailing_playlist_rewrites_a_short_playlist_too(relay):
    """A playlist shorter than trail_keep must still be rewritten.

    Returning None here made the handler serve the raw file instead, and the
    raw file carries ffmpeg's own sequence -- which is exactly the number
    that jumps backwards after a restart. The rewrite has to happen for
    every playlist, not only the long ones.
    """
    out = _serve(relay, _playlist(7, 7, 4))     # 4 segments, trail_keep is 8
    assert _seq_of(out) == 7
    for n in range(7, 11):
        assert f"seg{n:05d}.ts" in out, "a short playlist lost segments"


def test_trailing_playlist_offset_never_runs_off_a_shorter_playlist(relay):
    """The trailing offset is a high-water mark; the playlist can shrink.

    _trail_drop only ever grows, so after a long playlist it can point past
    the end of the short one a restarted encoder writes. Indexing segs[drop]
    with a stale offset was an IndexError inside the HTTP handler thread,
    seen by the receiver as a dead playlist.
    """
    _serve(relay, _playlist(200, 200, 12))      # drives _trail_drop up to 4
    assert relay._trail_drop == 4
    out = _serve(relay, _playlist(0, 0, 2))     # restart: only two segments
    assert "seg00001.ts" in out
    assert _seq_of(out) >= 204


def test_trailing_playlist_marks_a_restart_seam_as_discontinuous(relay):
    """A segment from a NEW encoder connection needs a DISCONTINUITY tag.

    A restarted connection continues the channel but not its timestamp
    clock. Without the tag the receiver splices the two timelines as one:
    the last few seconds play twice (the ~5s skip-back reported live on
    2026-09-05) and the decoder then fails, ending the stream.
    """
    _serve(relay, _playlist(100, 100, 12))
    relay._discont_segs.add(112)                # restart lands on 112
    out = _serve(relay, _playlist(104, 104, 12))
    lines = out.splitlines()
    # Exactly one tag, immediately before the first segment of the new
    # encoder's output -- never anywhere else.
    assert lines.count("#EXT-X-DISCONTINUITY") == 1
    idx = lines.index("#EXT-X-DISCONTINUITY")
    assert lines[idx + 1] == "#EXTINF:2.000000," or lines[idx + 1].startswith("#EXTINF:")
    assert lines[idx + 2] == "seg00112.ts"


def test_trailing_playlist_has_no_discontinuity_without_a_restart(relay):
    """A relay that never restarted serves a tag-free playlist.

    Some receivers drop A/V for a beat at every tag; sprinkling them into
    an unbroken timeline would trade one artefact for another.
    """
    out = _serve(relay, _playlist(50, 50, 12))
    assert "#EXT-X-DISCONTINUITY" not in out


def test_trailing_playlist_keeps_the_tag_through_trimming(relay):
    """The seam must survive being scrolled out of the trailing window.

    _trail_drop only grows; when the seam scrolls out of the kept window
    the tag belongs to nothing and must not be emitted, but while the seam
    is still inside the window it must stay, however far the trim moves.
    """
    _serve(relay, _playlist(100, 100, 12))
    relay._discont_segs.add(106)                # seam mid-window
    out = _serve(relay, _playlist(102, 102, 12))
    assert out.splitlines().count("#EXT-X-DISCONTINUITY") == 1
    assert "seg00106.ts" in out
    # Once the seam is gone from the playlist entirely, no tag.
    out2 = _serve(relay, _playlist(110, 110, 12))
    assert "#EXT-X-DISCONTINUITY" not in out2.splitlines()
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:1" in out2


def test_trailing_playlist_output_is_a_valid_playlist(relay):
    """Whatever the trimming does, the bytes served must still parse.

    Rebuilding the file line by line is easy to get wrong: dropping #EXTM3U
    or emitting two MEDIA-SEQUENCE lines makes every receiver reject the
    stream outright with no useful error.
    """
    out = _serve(relay, _playlist(50, 50, 12))
    assert out.startswith("#EXTM3U")
    _seq_of(out)                                 # asserts exactly one
    assert "#EXT-X-TARGETDURATION:2" in out      # header survived the rewrite
    uris = [l for l in out.splitlines() if l and not l.startswith("#")]
    assert len(uris) == relay.trail_keep
    assert out.count("#EXTINF") == len(uris)     # every URI kept its duration
    assert out.endswith("\n")


def test_variable_gop_playlist_keeps_three_target_durations(relay):
    # One 10s GOP followed by short 2s GOPs. A fixed eight-segment tail
    # has 16 seconds, far below the receiver's 30-second requirement.
    raw = _playlist(0, 0, 24).replace('#EXT-X-TARGETDURATION:2',
                                       '#EXT-X-TARGETDURATION:10')
    raw = raw.replace('#EXTINF:2.000000,', '#EXTINF:10.000000,', 1)
    out = _serve(relay, raw)
    durations = [float(l.split(':')[1].rstrip(',')) for l in out.splitlines()
                 if l.startswith('#EXTINF:')]
    assert sum(durations) >= 30
    assert len(durations) > relay.trail_keep


def test_buffer_can_grow_without_reintroducing_old_segments(relay):
    before = _serve(relay, _playlist(0, 0, 24))
    start = _seq_of(before)
    raw = _playlist(4, 4, 24).replace('#EXT-X-TARGETDURATION:2',
                                       '#EXT-X-TARGETDURATION:10')
    after = _serve(relay, raw)
    assert _seq_of(after) >= start
    assert 'seg00015.ts' not in after
    # A full 30-second window becomes available as the raw window advances.
    later = _serve(relay, _playlist(12, 12, 24).replace(
        '#EXT-X-TARGETDURATION:2', '#EXT-X-TARGETDURATION:10'))
    assert later.count('#EXTINF:') >= 15


def test_native_restart_marker_is_not_duplicated(relay):
    relay._discont_segs.add(106)
    raw = _playlist(100, 100, 12).replace(
        "#EXTINF:2.000000,\nseg00106.ts",
        "#EXT-X-DISCONTINUITY\n#EXTINF:2.000000,\nseg00106.ts")
    out = _serve(relay, raw)
    assert out.splitlines().count("#EXT-X-DISCONTINUITY") == 1


def test_restart_preserves_existing_playlist_sequence(relay, no_ffmpeg):
    relay.codecs = ["h264", "aac"]
    path = os.path.join(relay.root, "live.m3u8")
    _serve(relay, _playlist(100, 100, 12))
    cmd = relay._ffmpeg_cmd(path, start_number=113)
    assert cmd[cmd.index("-start_number") + 1] == "100"
    assert "append_list" in cmd[cmd.index("-hls_flags") + 1]


def test_native_restart_timeline_survives_sliding_window(relay):
    raw = _playlist(100, 100, 12).replace(
        "#EXTINF:2.000000,\nseg00106.ts",
        "#EXT-X-DISCONTINUITY\n#EXTINF:2.000000,\nseg00106.ts")
    before = _serve(relay, raw)
    after = _serve(relay, _playlist(104, 104, 12))
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:0" in before
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:1" in after
    assert "seg00108.ts" in before and "seg00108.ts" in after
    assert "#EXT-X-DISCONTINUITY" not in after.splitlines()


def test_trailing_playlist_returns_none_after_stop(relay):
    """A straggler request arriving after stop() must not raise.

    stop() sets root to None while the HTTP server is still winding down,
    and the handler thread calls straight into here.
    """
    relay.root = None
    assert relay.trailing_playlist() is None


def test_hls_head_has_playlist_headers_but_no_body(relay):
    """A Chromecast HEAD probe must not poison its kept-alive connection."""
    expected = _serve(relay, _playlist(7, 7, 4)).encode("utf-8")
    handler = functools.partial(HlsFileHandler, directory=relay.root)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.relay = relay
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        connection.request("HEAD", "/live.m3u8")
        head = connection.getresponse()
        assert head.status == 200
        assert int(head.getheader("Content-Length")) == len(expected)
        assert head.read() == b""
        # This reuses the connection. Any body incorrectly written for HEAD
        # becomes invalid response bytes before this GET status line.
        connection.request("GET", "/live.m3u8")
        get = connection.getresponse()
        assert get.status == 200
        assert get.read() == expected
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# --------------------------------------------------------------------------
# 3. HlsRelay._next_segment_number
# --------------------------------------------------------------------------

def test_next_segment_number_resumes_after_the_highest_segment(relay):
    """A restart that reuses seg00000 replays the start of the stream.

    The receiver has already played that name and may still hold it, so
    handing it a fresh file under the old name is heard as playback jumping
    back to the beginning of the channel.
    """
    for n in range(8):
        open(os.path.join(relay.root, f"seg{n:05d}.ts"), "wb").close()
    assert relay._next_segment_number() == 8


def test_next_segment_number_is_zero_on_an_empty_root(relay):
    """A cold relay must start at zero, not at -1 or 1."""
    assert relay._next_segment_number() == 0


def test_next_segment_number_ignores_foreign_filenames(relay):
    """Only seg#####.ts counts.

    live.m3u8, .tmp files ffmpeg writes mid-segment and a .m4s variant all
    live in the same directory; parsing a number out of one of those threw,
    and the whole restart was swallowed by _supervise's bare except.
    """
    for name in ("live.m3u8", "seg00003.ts", "segment9.ts", "seg00010.m4s",
                 "seg00004.ts.tmp", "notes.txt", "seg.ts"):
        open(os.path.join(relay.root, name), "wb").close()
    assert relay._next_segment_number() == 4


# --------------------------------------------------------------------------
# 4. HlsRelay._ffmpeg_cmd
# --------------------------------------------------------------------------

@pytest.fixture
def no_ffmpeg(monkeypatch):
    """Build commands without needing ffmpeg.exe or timing an encoder."""
    monkeypatch.setattr(caster, "_find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(caster, "pick_h264_encoder", lambda: "libx264")
    monkeypatch.setattr(caster, "_probe_codecs",
                        lambda *a, **k: pytest.fail(
                            "_ffmpeg_cmd probed the source over the network"))


def _cmd(url, tmp_path, no_ffmpeg, start_number=0, codecs=("h264", "aac"),
         hls_time=2):
    r = HlsRelay(url, hls_time=hls_time, prime_segments=3, trail_keep=8,
                 codecs=list(codecs))
    r.root = str(tmp_path)
    return r._ffmpeg_cmd(os.path.join(r.root, "live.m3u8"), start_number)


@pytest.mark.parametrize("url", ["http://iptv.invalid/live.ts",
                                 "HTTPS://iptv.invalid/live.ts"])
def test_ffmpeg_cmd_declares_http_sources_unseekable_before_the_input(
        url, tmp_path, no_ffmpeg):
    """-seekable 0, and it has to land on the demuxer.

    Without it ffmpeg reconnects to a dropped live stream with
    "Range: bytes=<offset>", the server answers with its current live edge,
    and the overlap is media the listener has already heard. Measured at
    1.88x of real time produced against 0.89x with the flag. After -i the
    option is a muxer option and does nothing at all.
    """
    cmd = _cmd(url, tmp_path, no_ffmpeg)
    assert "-seekable" in cmd
    assert cmd[cmd.index("-seekable") + 1] == "0"
    assert cmd.index("-seekable") < cmd.index("-i")


def test_ffmpeg_cmd_omits_http_options_for_a_local_path(tmp_path, no_ffmpeg):
    """-reconnect and friends belong to the HTTP protocol handler alone.

    Handed a local path ffmpeg refuses the WHOLE command with "Option
    reconnect not found" and opens no input, so relaying a local file --
    which _play_upnp does for anything that is not http(s) -- could never
    have worked.
    """
    cmd = _cmd(str(tmp_path / "movie.mkv"), tmp_path, no_ffmpeg)
    for option in ("-seekable", "-reconnect", "-reconnect_streamed",
                   "-reconnect_delay_max", "-rw_timeout"):
        assert option not in cmd, f"{option} was passed for a local file"
    assert cmd[cmd.index("-i") + 1].endswith("movie.mkv")


def test_ffmpeg_cmd_puts_genpts_on_the_demuxer(tmp_path, no_ffmpeg):
    """-fflags +genpts fills in timestamps the source omits.

    It is an input flag. It sat after -i, where it landed on the muxer and
    did nothing, so sources with broken timestamps still stuttered.
    """
    cmd = _cmd("http://iptv.invalid/live.ts", tmp_path, no_ffmpeg)
    assert cmd[cmd.index("-fflags") + 1] == "+genpts"
    assert cmd.index("-fflags") < cmd.index("-i")


def test_ffmpeg_cmd_dumps_parameter_sets_on_the_copy_path(tmp_path, no_ffmpeg):
    """An HLS segment must be decodable on its own.

    With -c copy a live TS carries SPS/PPS only occasionally, so the first
    segment a receiver joins on can describe frames with nothing to describe
    them by -- a black picture that never resolves.
    """
    cmd = _cmd("http://iptv.invalid/live.ts", tmp_path, no_ffmpeg,
               codecs=("h264", "aac"))
    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"
    assert "-bsf:v" in cmd
    assert cmd[cmd.index("-bsf:v") + 1] == "dump_extra=freq=keyframe"


def test_ffmpeg_cmd_drops_the_bitstream_filter_when_transcoding(
        tmp_path, no_ffmpeg):
    """dump_extra on a transcoded stream is wrong and ffmpeg rejects it.

    HEVC and AV1 sources are re-encoded to H.264 for Cast receivers; the
    encoder already emits parameter sets, and applying the filter to its
    output made ffmpeg exit before the first segment existed.
    """
    cmd = _cmd("http://iptv.invalid/live.ts", tmp_path, no_ffmpeg,
               codecs=("hevc", "aac"))
    assert "-bsf:v" not in cmd
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-c:a") + 1] == "copy"


@pytest.mark.parametrize("start_number,flags", [
    (0, "delete_segments"),
    (137, "delete_segments+append_list"),
])
def test_ffmpeg_cmd_appends_rather_than_truncates_after_a_restart(
        start_number, flags, tmp_path, no_ffmpeg):
    """A restart must continue the playlist, not start it over.

    Without append_list ffmpeg truncates live.m3u8, stripping the segments
    the receiver is still working through, and without -start_number the
    restarted encoder writes seg00000.ts over a name the receiver already
    played.
    """
    cmd = _cmd("http://iptv.invalid/live.ts", tmp_path, no_ffmpeg,
               start_number=start_number)
    assert cmd[cmd.index("-hls_flags") + 1] == flags
    if start_number:
        assert "-start_number" in cmd
        assert cmd[cmd.index("-start_number") + 1] == str(start_number)


@pytest.mark.parametrize("hls_time", [1, 2, 4])
def test_ffmpeg_cmd_honours_hls_time(hls_time, tmp_path, no_ffmpeg):
    """Segment length is the whole start-up latency trade.

    It comes from the quality preset via _make_relay; a hard-coded value
    here silently ignored the user's choice and put a clean source back to
    the slow default.
    """
    cmd = _cmd("http://iptv.invalid/live.ts", tmp_path, no_ffmpeg,
               hls_time=hls_time)
    assert cmd[cmd.index("-hls_time") + 1] == str(hls_time)
    assert cmd[-1].endswith("live.m3u8")


# --------------------------------------------------------------------------
# 5. MainFrame._await_playing -- stale cast status
# --------------------------------------------------------------------------

def test_await_playing_ignores_the_previous_session(
        frame, fake_status, status_sequence):
    """mc.status keeps reporting the OLD session until the new one arrives.

    IDLE/INTERRUPTED is exactly what the receiver says when a session is
    replaced -- which is what loading something else does -- so a load
    issued moments after a stop read the dying status of the previous
    session and was announced as rejected while it was in fact starting.
    """
    mc = status_sequence([
        fake_status("IDLE", "INTERRUPTED", session=1),   # the old session
        fake_status("IDLE", "ERROR", session=1),         # still the old one
        fake_status("PLAYING", None, session=2),         # the new one, finally
    ])
    assert MainFrame._await_playing(frame, mc, previous_session=1) is True


def test_await_playing_rejects_a_new_session_error_promptly(
        frame, fake_status, status_sequence):
    """A genuine rejection must be reported at once, not after LOAD_TIMEOUT.

    Twelve seconds of silence before "Could not play" is heard by a blind
    user as the app having hung.
    """
    mc = status_sequence([fake_status("IDLE", "ERROR", session=2)])
    started = time.monotonic()
    assert MainFrame._await_playing(frame, mc, previous_session=1) is False
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("reason", MainFrame.STALE_IDLE_REASONS)
def test_await_playing_does_not_treat_a_replaced_session_as_a_failure(
        reason, frame, fake_status, status_sequence):
    """INTERRUPTED and CANCELLED describe an ending, not a refusal.

    They arrive on the NEW session id too, in the moment the receiver tears
    the old playback down. Treating them as rejection reported the previous
    stream's death as this stream's failure.
    """
    mc = status_sequence([
        fake_status("IDLE", reason, session=2),
        fake_status("PLAYING", None, session=2),
    ])
    assert MainFrame._await_playing(frame, mc, previous_session=1) is True


# --------------------------------------------------------------------------
# 6. MainFrame._ensure_receiver
# --------------------------------------------------------------------------

class FakeCast:
    """Just enough Chromecast for _ensure_receiver: an app id and a launch."""

    def __init__(self, app_id):
        self.app_id = app_id
        self.launches = []

    def start_app(self, app_id, force_launch=False):
        self.launches.append((app_id, force_launch))
        self.app_id = app_id        # the receiver comes up straight away


def test_ensure_receiver_leaves_a_running_receiver_alone(frame):
    """Relaunching an app that is already up costs seconds on a TV.

    The old code called start_app(force_launch=True) then slept 2.5 s on
    every single play, tearing down a receiver that was almost always
    already the one wanted.
    """
    cast = FakeCast(MainFrame.CAST_APP_ID)
    started = time.monotonic()
    MainFrame._ensure_receiver(frame, cast)
    assert cast.launches == []
    assert time.monotonic() - started < 0.1


def test_ensure_receiver_launches_a_cold_receiver_once_and_waits(frame):
    """A cold receiver is launched exactly once and then waited for.

    play_media against a receiver that has not come up yet silently no-ops:
    the state goes LOADING then straight to IDLE and nothing plays. Two
    launches would be worse still -- the second kills the first.
    """
    cast = FakeCast("YouTube")
    MainFrame._ensure_receiver(frame, cast)
    assert cast.launches == [(MainFrame.CAST_APP_ID, True)]
    assert cast.app_id == MainFrame.CAST_APP_ID


def test_ensure_receiver_swallows_a_dead_connection(frame):
    """A cast that raises must not take the play path down with it.

    play_media reports the real failure a moment later with a message the
    user can act on; an exception escaping here reached the UI thread as a
    traceback instead.
    """
    class Broken:
        @property
        def app_id(self):
            raise RuntimeError("connection closed")

    MainFrame._ensure_receiver(frame, Broken(), timeout=0.1)


# --------------------------------------------------------------------------
# 7. Device
# --------------------------------------------------------------------------

def test_supports_video_believes_an_audio_only_sink_list():
    """The RX-V4A publishes 49 content types and every one is audio/*.

    Before the sink list was believed, a screen cast to it built a whole
    H.264 MPEG-TS and pushed video/mpeg at a device that accepts no video
    at all -- an encoder's work for something it must refuse.
    """
    amp = Device("upnp", "R&B Room", {"control_url": "http://192.168.1.65:49154/x"},
                 sinks=frozenset({"audio/mpeg", "audio/L16", "audio/x-wav"}))
    assert amp.supports_video is False


def test_supports_video_believes_a_video_sink_list():
    """A renderer that published video/* gets a picture whatever its kind."""
    tv = Device("upnp", "RB Room", {"control_url": "http://192.168.1.73:38400/x"},
                sinks=frozenset({"audio/mpeg", "video/mpeg", "video/mp4"}))
    assert tv.supports_video is True


@pytest.mark.parametrize("kind,expected", [
    ("chromecast", True), ("upnp", True), ("roku", True), ("kodi", True),
    ("airplay", False), ("sonos", False), ("musiccast", False),
])
def test_supports_video_falls_back_to_the_kind_when_nothing_was_published(
        kind, expected):
    """Silence is not a refusal.

    Plenty of renderers answer GetProtocolInfo badly or not at all, and
    refusing to send those anything would be worse than guessing from the
    kind. Only a published list may override it.
    """
    assert Device(kind, "Box", {}).supports_video is expected


@pytest.mark.parametrize("key,expected", [
    ({"host": "192.168.1.65"}, "192.168.1.65"),
    ({"ip": "192.168.1.70"}, "192.168.1.70"),
    ({"host": "192.168.1.65", "ip": "10.0.0.1"}, "192.168.1.65"),
    ({"control_url": "http://192.168.1.73:38400/upnp/control/mingusavtr"},
     "192.168.1.73"),
    ({"base": "http://192.168.1.101:9197/dmr"}, "192.168.1.101"),
    ({}, ""),
])
def test_device_host_from_every_key_shape(key, expected):
    """Six protocols record an address six different ways.

    Device.host is what the MusicCast side-channel is aimed at, and a wrong
    or empty answer there sends power/input/volume calls to nothing while
    the audio path keeps working -- a silent half-failure.
    """
    assert Device("upnp", "Box", key).host == expected


def test_device_host_from_an_object_with_an_address():
    """pychromecast and soco hand back objects, not dicts."""
    key = types.SimpleNamespace(address="192.168.1.67", port=8009)
    assert Device("chromecast", "Hisense", key).host == "192.168.1.67"


def test_device_host_is_empty_when_the_object_has_no_address():
    """Missing is "", never None: callers build URLs out of this."""
    assert Device("kodi", "Box", object()).host == ""


# --------------------------------------------------------------------------
# 8. The discovery merge rule (the loop from _discover_sync)
# --------------------------------------------------------------------------

def _merge(results: dict) -> dict:
    """The merge from _discover_sync, verbatim, minus the UI around it."""
    found: dict = {}
    for key in ("chromecast", "airplay", "upnp", "roku", "kodi"):
        for name, device in results.get(key, {}).items():
            seen = found.get(name)
            if seen is None:
                found[name] = device
            elif (device.sinks and device.supports_video
                    and not seen.supports_video):
                found[name] = device
    return found


def test_merge_lets_a_published_video_renderer_win_over_airplay():
    """One television answers AirPlay and DLNA under a single name.

    AirPlay here is RAOP -- audio only -- and it is merged first, so
    first-come silently cost that TV its picture. A published video sink
    list is proof enough to replace it.
    """
    airplay = Device("airplay", "TV", {"host": "192.168.1.73"})
    dlna = Device("upnp", "TV", {"control_url": "http://192.168.1.73:38400/x"},
                  sinks=frozenset({"video/mp4", "audio/mpeg"}))
    assert _merge({"airplay": {"TV": airplay}, "upnp": {"TV": dlna}})["TV"] is dlna


def test_merge_never_lets_a_silent_renderer_displace_an_entry():
    """A renderer that published nothing must not win on its kind alone.

    "upnp" implies video by default, so without the `device.sinks` guard the
    RX-V4A's DLNA entry displaced its working AirPlay entry on the strength
    of an assumption -- and the DLNA path to it is the worse one.
    """
    airplay = Device("airplay", "R&B Room", {"host": "192.168.1.65"})
    dlna = Device("upnp", "R&B Room",
                  {"control_url": "http://192.168.1.65:49154/x"})
    merged = _merge({"airplay": {"R&B Room": airplay},
                     "upnp": {"R&B Room": dlna}})
    assert merged["R&B Room"] is airplay


def test_merge_order_still_decides_a_tie():
    """When both entries can do the same thing, merge order wins.

    The order is fixed (chromecast, airplay, upnp, roku, kodi) precisely so
    that which scan happened to finish first cannot change the device list
    between two runs.
    """
    cast = Device("chromecast", "Hisense",
                  types.SimpleNamespace(address="192.168.1.73"))
    dlna = Device("upnp", "Hisense", {"control_url": "http://192.168.1.73:38400/x"},
                  sinks=frozenset({"video/mp4"}))
    merged = _merge({"chromecast": {"Hisense": cast}, "upnp": {"Hisense": dlna}})
    assert merged["Hisense"] is cast

    amp_air = Device("airplay", "Amp", {"host": "192.168.1.65"})
    amp_upnp = Device("upnp", "Amp", {"host": "192.168.1.65"},
                      sinks=frozenset({"audio/mpeg"}))
    merged = _merge({"airplay": {"Amp": amp_air}, "upnp": {"Amp": amp_upnp}})
    assert merged["Amp"] is amp_air     # neither does video: AirPlay is kept


# --------------------------------------------------------------------------
# 9. MainFrame._capture_container
# --------------------------------------------------------------------------

CHROMECAST = Device("chromecast", "Hisense",
                    types.SimpleNamespace(address="192.168.1.73"))
ROKU = Device("roku", "Roku", {"host": "192.168.1.99"})
KODI = Device("kodi", "Kodi", {"host": "192.168.1.98"})
TV_DLNA = Device("upnp", "Samsung", {"control_url": "http://192.168.1.101:9197/x"},
                 sinks=frozenset({"video/mpeg", "audio/mpeg"}))
AMP_DLNA = Device("upnp", "R&B Room",
                  {"control_url": "http://192.168.1.65:49154/x"},
                  sinks=frozenset({"audio/mpeg", "audio/L16"}))
SONOS = Device("sonos", "Kitchen", {"host": "192.168.1.50"})


@pytest.mark.parametrize("dev,expected", [
    (CHROMECAST, "mp4"),    # progressive fragmented MP4
    (ROKU, "mp4"),          # Roku Media Player has no MPEG-TS at all
    (KODI, "mpegts"),       # the most forgiving target here
    (TV_DLNA, "mpegts"),
    (AMP_DLNA, "wav"),      # published audio-only sinks
    (SONOS, "wav"),         # speakers
])
def test_capture_container_matches_what_the_receiver_can_play(
        frame, dev, expected):
    """The wrong container is a whole encoder's work thrown away.

    Roku silently refuses MPEG-TS, Cast wants fragmented MP4, and an
    amplifier that publishes only audio/* types gets sound -- building
    H.264 for it produces something it must reject.
    """
    assert MainFrame._capture_container(frame, dev, audio_only=False) == expected


@pytest.mark.parametrize("dev", [CHROMECAST, ROKU, KODI, TV_DLNA, AMP_DLNA,
                                 SONOS])
def test_capture_container_is_always_wav_when_audio_only(frame, dev):
    """"Cast this PC's sound" must never build a video stream.

    wav plus pcm_is_directly_usable() takes ffmpeg out of the path entirely,
    which is the lowest-latency route the app has; a video container here
    would cost an encoder and the delay that comes with it.
    """
    assert MainFrame._capture_container(frame, dev, audio_only=True) == "wav"


# --------------------------------------------------------------------------
# 3. HlsRelay under-feed rotation -- a fresh connection on a throttled source
# --------------------------------------------------------------------------

class _FakeProc:
    """A stand-in encoder: alive until killed, and it records the kill."""

    def __init__(self) -> None:
        self.killed = 0

    def poll(self):
        return None

    def kill(self) -> None:
        self.killed += 1


def _segments(root, count: int, dur: float = 2.5) -> None:
    """Materialise `count` segments and a matching playlist in `root`."""
    for n in range(count):
        with open(os.path.join(root, f"seg{n:05d}.ts"), "wb"):
            pass
    lines = ["#EXTM3U", "#EXT-X-VERSION:3",
             "#EXT-X-MEDIA-SEQUENCE:0"]
    for n in range(count):
        lines += [f"#EXTINF:{dur},", f"seg{n:05d}.ts"]
    with open(os.path.join(root, "live.m3u8"), "w",
              encoding="utf-8", newline="") as f:
        f.write("\n".join(lines) + "\n")


def _live_relay(relay):
    """A relay the caller certifies live, with an encoder that started at 100."""
    relay.live = True
    relay._proc_born = 100.0
    relay.proc = _FakeProc()
    return relay


def test_underfeed_rotation_kills_a_starved_encoder(relay):
    """Two consecutive under-fed windows rotate to a fresh connection.

    One provider's cap measured 0.44 media-seconds per wall-second: a
    A 20s window that should yield eight 2.5s segments yields six.  It is
    under the sustained 0.85x threshold but not so starved that it should
    rotate on one measurement.
    The relay must kill the encoder so the supervisor restarts it, because
    ffmpeg itself never notices -- the connection stays up, just slow.
    """
    r = _live_relay(relay)
    _segments(r.root, 40)          # newest seg00039
    r._check_underfeed(160.0)      # opens the cadence window
    _segments(r.root, 46)          # +6 in 20s: 0.75x -- under-fed
    r._check_underfeed(180.0)
    assert r._fail_streak == 1
    assert r.proc.killed == 0      # one bad window is not enough
    _segments(r.root, 52)          # +6 more: still 0.75x
    r._check_underfeed(200.0)
    assert r._fail_streak == 0
    assert r.proc.killed == 1      # rotated
    assert r._rotated == 1
    assert r._last_rotate == 200.0
    # The gap floor prevents thrashing right after a rotation.
    r._check_underfeed(215.0)
    assert r.proc.killed == 1


def test_underfeed_rotation_leaves_a_healthy_encoder_alone(relay):
    """A full-rate encoder is never rotated, no matter how long it runs."""
    r = _live_relay(relay)
    _segments(r.root, 40)
    r._check_underfeed(160.0)      # opens the window
    _segments(r.root, 48)          # +8 in 20s: 1.0x
    r._check_underfeed(180.0)
    _segments(r.root, 56)          # +8 more: still 1.0x
    r._check_underfeed(200.0)
    assert r.proc.killed == 0
    assert r._rotated == 0


def test_underfeed_rotation_immediately_replaces_a_starved_encoder(relay):
    """A 0.44x provider cap drains the Cast cushion before a second window."""
    r = _live_relay(relay)
    _segments(r.root, 40)
    r._check_underfeed(160.0)
    _segments(r.root, 43)          # +3 in 20s: 0.375x, below hard limit
    r._check_underfeed(180.0)
    assert r.proc.killed == 1
    assert r._rotated == 1


def test_underfeed_rotation_sums_actual_new_segment_durations(relay):
    """A long final GOP must not hide starvation in the preceding segments."""
    r = _live_relay(relay)
    _segments(r.root, 40)
    r._check_underfeed(160.0)
    durations = [2.5] * 40 + [0.1, 0.1, 0.1, 10.0]
    for n in range(40, 44):
        with open(os.path.join(r.root, f"seg{n:05d}.ts"), "wb"):
            pass
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-MEDIA-SEQUENCE:0"]
    for n, duration in enumerate(durations):
        lines += [f"#EXTINF:{duration},", f"seg{n:05d}.ts"]
    with open(os.path.join(r.root, "live.m3u8"), "w",
              encoding="utf-8", newline="") as f:
        f.write("\n".join(lines) + "\n")
    r._check_underfeed(190.0)
    # Wait three GOPs: the exact 10.3 media seconds are 0.343x. Multiplying
    # the four new segments by the final GOP would incorrectly call it 1.33x.
    assert r.proc.killed == 1


class _FakeSource:
    """A stand-in TsSource: records how often the socket was dropped."""

    def __init__(self) -> None:
        self.rotations = 0

    def rotate(self) -> None:
        self.rotations += 1


def test_retained_window_outlasts_the_cushion_it_serves():
    """delete_segments must not be able to eat the segment served first.

    The receiver starts at the OLDEST segment in the trailing playlist, so if
    the retained window is only as deep as the cushion, the file it needs
    next is already behind the delete the moment it slips. That is not a
    rebuffer, it is a 404 and a permanent stall: observed with a receiver
    frozen at 41.8s while the relay's own playlist advanced past sequence
    170. Counting entries hid it -- 24 of them held 120-216s at a source's
    own 5-9s GOP and only 48s once forced keyframes cut segments to 2s.
    """
    for hls_time, trail_seconds in ((2, 45.0), (2, 25.0), (2, 60.0), (4, 45.0)):
        r = HlsRelay("http://example.invalid/live.ts", hls_time=hls_time,
                     prime_segments=3, trail_keep=8,
                     trail_seconds=trail_seconds)
        retained = r._list_size() * hls_time
        assert retained >= 2 * trail_seconds, (
            f"hls_time={hls_time} trail_seconds={trail_seconds}: retained "
            f"{retained}s is not twice the {trail_seconds}s cushion")


def test_retained_window_is_stated_in_the_ffmpeg_command():
    """The sizing has to reach ffmpeg, not just the helper."""
    r = HlsRelay("http://example.invalid/live.ts", hls_time=2,
                 prime_segments=3, trail_keep=8, trail_seconds=45.0,
                 codecs=["h264", "aac"])
    r.root = "."
    cmd = r._ffmpeg_cmd("live.m3u8")
    assert int(cmd[cmd.index("-hls_list_size") + 1]) == r._list_size()


def test_piped_underfeed_drops_the_socket_and_never_the_encoder(relay):
    """On the piped path a fresh connection costs a socket, not a restart.

    ffmpeg is not holding the upstream socket there, so killing it would buy
    a discontinuity and a priming pause and change nothing upstream.
    """
    r = _live_relay(relay)
    r.ts_source = _FakeSource()
    _segments(r.root, 40)
    r._check_underfeed(160.0)      # opens the cadence window
    _segments(r.root, 41)          # 2.5s of media in 16s: 0.16x
    r._check_underfeed(176.0)
    assert r.ts_source.rotations == 1
    assert r.proc.killed == 0      # the encoder sees one unbroken stream


def test_piped_rotation_repeats_on_the_cadence_a_socket_swap_can_afford(relay):
    """A source that decays in ~10s has to be rotated on that timescale.

    Measured against one provider: held for 8s a connection yielded 2.27x
    the channel bitrate, for 15s 1.20x, for 30s 0.89x, and held indefinitely
    0.57x. The 90s floor pinned it to the last of those, so the cushion
    drained roughly once every 90 seconds. That floor exists to stop an
    encoder being killed every few seconds; a socket swap does not pay that
    price and does not need that protection.
    """
    r = _live_relay(relay)
    r.ts_source = _FakeSource()
    _segments(r.root, 40)
    r._check_underfeed(160.0)
    _segments(r.root, 41)
    r._check_underfeed(176.0)
    assert r.ts_source.rotations == 1
    # 8s after rotating is still inside the piped floor: no second window.
    r._check_underfeed(184.0)
    assert r._eval_t0 is None
    # 11s after, the floor is clear and a still-starved source rotates again.
    r._check_underfeed(187.0)      # opens the window
    _segments(r.root, 42)
    r._check_underfeed(203.0)
    assert r.ts_source.rotations == 2


def test_underfeed_will_not_judge_a_window_shorter_than_the_eval_period(relay):
    """A burst-delivery source is idle between bursts, so a short window lies.

    One provider bursts at up to 131 Mb/s with gaps of 10.6s while tracking
    real time exactly. A window that lands inside a gap reads 0x; it must not
    be allowed to close and rotate a healthy connection.
    """
    r = _live_relay(relay)
    r.ts_source = _FakeSource()
    _segments(r.root, 40)
    r._check_underfeed(160.0)
    # 12s later the source has published nothing: mid-burst-gap, not starved.
    r._check_underfeed(172.0)
    assert r.ts_source.rotations == 0
    assert r._eval_t0 == (160.0, 39)   # the window is still open, not judged


def test_encoder_path_keeps_the_long_floor_between_rotations(relay):
    """Killing ffmpeg stays rare: a restart is a seam the receiver can see."""
    r = _live_relay(relay)          # no ts_source: the encoder-kill path
    _segments(r.root, 40)
    r._check_underfeed(160.0)
    _segments(r.root, 41)
    r._check_underfeed(176.0)
    assert r.proc.killed == 1
    # The same timeline that rotates twice on the piped path rotates once here.
    r._check_underfeed(187.0)
    _segments(r.root, 42)
    r._check_underfeed(203.0)
    assert r.proc.killed == 1


def test_underfeed_counts_segment_that_was_in_progress_at_window_start(relay):
    r = _live_relay(relay)
    _segments(r.root, 40, dur=10)
    # ffmpeg opens the next file before publishing it in the playlist.
    with open(os.path.join(r.root, 'seg00040.ts'), 'wb'):
        pass
    r._check_underfeed(160)
    assert r._eval_t0 == (160, 39)
    _segments(r.root, 43, dur=10)
    r._check_underfeed(190)
    _segments(r.root, 46, dur=10)
    r._check_underfeed(220)
    assert r.proc.killed == 0
    assert r._fail_streak == 0


def test_underfeed_does_not_judge_a_healthy_long_gop_between_keyframes(relay):
    r = _live_relay(relay)
    _segments(r.root, 40, dur=20)
    r._check_underfeed(160)
    r._check_underfeed(176)  # no keyframe yet, but not a stalled source
    assert r.proc.killed == 0
    _segments(r.root, 43, dur=20)
    r._check_underfeed(220)
    assert r.proc.killed == 0


def test_underfeed_missing_window_is_not_counted_as_zero(relay):
    r = _live_relay(relay)
    _serve(r, _playlist(0, 0, 12, target=1))
    r._check_underfeed(160)
    # A healthy source published 20 seconds. Eight already scrolled out of
    # the raw 12-segment window: measuring only the remaining 12 is wrong.
    _serve(r, _playlist(20, 20, 12, target=1))
    r._check_underfeed(180)
    _serve(r, _playlist(40, 40, 12, target=1))
    r._check_underfeed(200)
    assert r.proc.killed == 0


def test_underfeed_rotation_requires_certified_live_http(relay):
    """Rotation restarts from the live edge, so VOD/local must never rotate.

    A bare HlsRelay (the default) has live=None and is never touched, and
    neither is a live-coded relay whose source is a local path.
    """
    _segments(relay.root, 40)
    relay.proc = _FakeProc()
    relay._proc_born = 100.0       # live stays None
    relay._check_underfeed(160.0)
    relay._check_underfeed(200.0)  # span long enough, no segments gained
    assert relay.proc.killed == 0

    local = HlsRelay(str(relay.root) + "/movie.mkv", live=True)
    local.root = str(relay.root)
    local.proc = _FakeProc()
    local._proc_born = 100.0
    _segments(local.root, 40)
    local._check_underfeed(160.0)
    local._check_underfeed(200.0)
    assert local.proc.killed == 0


def test_underfeed_rotation_waits_for_a_window_and_encoder_age(relay):
    """The cadence window needs ROTATE_EVAL seconds and the encoder needs
    ROTATE_MIN_AGE before it is judged at all."""
    r = _live_relay(relay)
    _segments(r.root, 40)
    r._check_underfeed(120.0)      # age 20s < minimum: not judged
    assert r._eval_t0 is None
    _segments(r.root, 40)
    r._proc_born = 100.0
    r._check_underfeed(160.0)      # opens the window
    r._check_underfeed(165.0)      # only 5s in: no evaluation yet
    assert r._eval_t0 is not None
    assert r.proc.killed == 0


# --------------------------------------------------------------------------
# TsSource -- hiding a provider that reconnects constantly and replays
# --------------------------------------------------------------------------

def _ts(seed: int, packets: int) -> bytes:
    """Bytes that look enough like MPEG-TS to be searched like it."""
    out = bytearray()
    for i in range(packets):
        out += b"\x47" + ((seed + i) % 251).to_bytes(1, "big") + \
            bytes((i * 7 + seed + j) % 256 for j in range(186))
    return bytes(out)


class _Replaying(threading.Thread):
    """A live TS server that closes early and resends what it already sent.

    Modelled on the measured behaviour: connections lasting a few seconds,
    each starting several seconds behind where the last one stopped.
    """

    def __init__(self, stream: bytes, serve: int, replay: int):
        super().__init__(daemon=True)
        self.stream, self.serve, self.replay = stream, serve, replay
        self.sent_from = []
        self.position = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/live.ts"

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.recv(4096)
                    start = max(0, self.position - self.replay)
                    self.sent_from.append(start)
                    body = self.stream[start:start + self.serve]
                    self.position = start + len(body)
                    conn.sendall(b"HTTP/1.1 200 OK\r\n"
                                 b"Content-Type: video/mp2t\r\n\r\n" + body)
                except OSError:
                    pass


class _FakePipe:
    """A stand-in for an encoder's stdin that records being closed."""

    def __init__(self, fail_on_close: bool = False) -> None:
        self.closed = 0
        self.written = b""
        self._fail_on_close = fail_on_close

    def write(self, data: bytes) -> None:
        self.written += data

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed += 1
        if self._fail_on_close:
            raise OSError(22, "Invalid argument")


def test_sink_closes_the_pipe_it_replaces():
    """A replaced encoder's stdin is closed here, not by the collector.

    Its process has already been killed, so the flush that finalisation
    attempts fails outside any handler -- an unraisable OSError at
    interpreter shutdown rather than an error anything can catch.
    """
    sink = caster._Sink()
    first, second = _FakePipe(), _FakePipe()
    sink.attach(first)
    sink.attach(second)
    assert first.closed == 1
    assert second.closed == 0
    sink.write(b"x")
    assert second.written == b"x"


def test_sink_survives_a_pipe_that_fails_to_close():
    """The dead pipe is the expected case, so its error cannot propagate."""
    sink = caster._Sink()
    sink.attach(_FakePipe(fail_on_close=True))
    sink.attach(_FakePipe())          # must not raise
    sink.write(b"y")


def test_ts_source_drops_the_replayed_bytes_and_leaves_no_hole():
    """The whole point: one continuous stream out of a replaying source."""
    stream = _ts(3, 4000)          # ~750 KiB
    server = _Replaying(stream, serve=200_000, replay=60_000)
    server.start()
    received = bytearray()

    class Collector:
        def write(self, data):
            received.extend(data)

    source = caster.TsSource(server.url(), Collector())
    source.PROBE = 4096
    source.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and len(received) < len(stream) - 200_000:
        time.sleep(0.05)
    source.stop()
    source.join(timeout=5)
    server.stop()

    assert len(server.sent_from) > 2, "the source never reconnected"
    assert source.deduped > 0, "no replay was recognised"
    # Continuity is the test: what came out must be an unbroken run of the
    # original stream. A duplicate or a hole both break this.
    assert bytes(received) in stream, "the forwarded stream was not continuous"
    assert len(received) > 300_000, "far too little got through"


def test_ts_source_forwards_everything_when_nothing_matches():
    """A real gap must not be papered over by guessing at an overlap."""
    sink = types.SimpleNamespace(written=bytearray())
    sink.write = sink.written.extend
    source = caster.TsSource("http://example.invalid/live.ts", sink)
    source._forward(_ts(1, 100))
    fresh = _ts(200, 100)
    assert source._overlap(fresh) == 0


def test_ts_source_overlap_prefers_the_smallest_honest_claim():
    """Repeating null packets must not inflate the overlap into a hole."""
    sink = types.SimpleNamespace(write=lambda data: None)
    source = caster.TsSource("http://example.invalid/live.ts", sink)
    repeated = b"\xff" * 4096
    source._forward(repeated + _ts(9, 40) + repeated)
    # The tail ends with the repeated block, so the honest overlap is that
    # block alone -- not the whole span back to its first appearance.
    assert source._overlap(repeated) == len(repeated)


def test_piped_ingest_only_for_a_live_raw_http_stream():
    def relay_for(url, live=True):
        return HlsRelay(url, codecs=["h264", "aac"], live=live)
    assert relay_for("http://host/live.ts").piped_source()
    assert relay_for("http://host/stream?token=1").piped_source()
    assert not relay_for("http://host/live.m3u8").piped_source()
    assert not relay_for("http://host/live.ts", live=False).piped_source()
    assert not relay_for("C:/media/movie.ts").piped_source()


def test_piped_encoder_reads_the_pipe_and_gets_no_socket_timeout(tmp_path):
    """A quiet pipe is TsSource reconnecting, not a dead source.

    An -rw_timeout on the piped path would make ffmpeg exit through exactly
    the gap the reader exists to cover.
    """
    r = HlsRelay("http://host/live.ts", codecs=["h264", "aac"], live=True)
    r.root = str(tmp_path)
    cmd = r._ffmpeg_cmd(os.path.join(str(tmp_path), "live.m3u8"))
    assert cmd[cmd.index("-i") + 1] == "pipe:0"
    assert "-rw_timeout" not in cmd
    assert "-seekable" not in cmd
    assert cmd[cmd.index("-f") + 1] == "mpegts"


def test_sink_survives_the_encoder_being_replaced():
    """The reader must never write into the pipe of a dead ffmpeg."""
    sink = caster._Sink()
    sink.write(b"before any encoder")      # must not raise

    class Pipe:
        def __init__(self):
            self.data = bytearray()
            self.closed = False

        def write(self, data):
            if self.closed:
                raise ValueError("I/O operation on closed file")
            self.data.extend(data)

        def flush(self):
            pass

        def close(self):
            # A real BufferedWriter has this, and _Sink closes the pipe it
            # replaces rather than leaving it to the garbage collector.
            self.closed = True

    first, second = Pipe(), Pipe()
    sink.attach(first)
    sink.write(b"one")
    first.closed = True
    sink.write(b"lost")                    # must not raise
    sink.attach(second)
    sink.write(b"two")
    assert bytes(first.data) == b"one"
    assert bytes(second.data) == b"two"


def test_encoder_last_words_never_repeats_the_source_url():
    """ffmpeg names its input in its errors, and that names the password."""
    r = HlsRelay("http://host/live.ts", codecs=["h264", "aac"], live=True)
    r._stderr_tail.append(caster._redact_urls(
        "[http @ 0] Stream ends prematurely: http://host:8080/user/secret/1"))
    words = r.encoder_last_words()
    assert "secret" not in words
    assert "<source>" in words


# --------------------------------------------------------------------------
# The cushion is a duration, not a segment count
# --------------------------------------------------------------------------

def _deep_relay(tmp_path, seconds):
    r = HlsRelay("http://example.invalid/live.ts", hls_time=2,
                 prime_segments=3, trail_keep=8, trail_seconds=seconds)
    r.root = str(tmp_path)
    return r


def test_cushion_holds_its_seconds_when_segments_are_short(tmp_path):
    """Eight one-second segments is eight seconds, and that is not a cushion.

    Measured on a live channel: the source cuts at its own keyframes, so a
    fixed count of them bought anywhere from 25 to 40 seconds while the input
    arrived in bursts up to 13 seconds apart. Whoever is short wins, and it
    has to be the seconds.
    """
    r = _deep_relay(tmp_path, 45)
    out = _serve(r, _playlist(0, 0, 60, target=1))
    held = sum(float(l.split(":")[1].rstrip(",")) for l in out.splitlines()
               if l.startswith("#EXTINF:"))
    assert held >= 45
    assert out.count("#EXTINF:") > r.trail_keep


def test_cushion_never_undercuts_the_receivers_own_floor(tmp_path):
    """3x TARGETDURATION is the receiver's rule, not a preference.

    A shallow preset must not talk the relay below the depth a cast receiver
    refuses to start at.
    """
    r = _deep_relay(tmp_path, 0)
    raw = _playlist(0, 0, 40).replace("#EXT-X-TARGETDURATION:2",
                                      "#EXT-X-TARGETDURATION:10")
    out = _serve(r, raw)
    held = sum(float(l.split(":")[1].rstrip(",")) for l in out.splitlines()
               if l.startswith("#EXTINF:"))
    assert held >= 30


def test_cushion_serves_what_there_is_before_it_is_deep_enough(tmp_path):
    """A channel that has only just started must still play.

    Waiting for the full cushion before serving anything would turn every
    start into a minute of silence.
    """
    r = _deep_relay(tmp_path, 45)
    out = _serve(r, _playlist(0, 0, 6, target=1))
    assert out.count("#EXTINF:") == 6
    assert _seq_of(out) == 0


def test_deeper_cushion_is_configured_by_the_quality_preset():
    """The delay/robustness trade stays the user's, as it is for hls_trail."""
    import caster_config
    depths = {name: p["hls_trail_seconds"]
              for name, p in caster_config.QUALITY_PRESETS.items()}
    assert depths["latency"] < depths["balanced"] < depths["quality"]
    assert all(d >= 13 for d in depths.values()), \
        "a cushion under the measured 13s input burst is no cushion"
