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

import os
import time
import types

import pytest

import caster
from caster import Device, HlsRelay, MainFrame, probe_media


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
    """A relay with a temp root and no ffmpeg, server or thread behind it."""
    r = HlsRelay("http://example.invalid/live.ts", hls_time=2,
                 prime_segments=3, trail_keep=8)
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
    assert out.count("#EXT-X-DISCONTINUITY") == 1
    assert "seg00106.ts" in out
    # Once the seam is gone from the playlist entirely, no tag.
    out2 = _serve(relay, _playlist(110, 110, 12))
    assert "#EXT-X-DISCONTINUITY" not in out2


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


def test_trailing_playlist_returns_none_after_stop(relay):
    """A straggler request arriving after stop() must not raise.

    stop() sets root to None while the HTTP server is still winding down,
    and the handler thread calls straight into here.
    """
    relay.root = None
    assert relay.trailing_playlist() is None


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
             f"#EXT-X-MEDIA-SEQUENCE:0"]
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

    The gohyperspeed cap measured 0.44 media-seconds per wall-second: a
    20s window that should yield eight 2.5s segments yields three or four.
    The relay must kill the encoder so the supervisor restarts it, because
    ffmpeg itself never notices -- the connection stays up, just slow.
    """
    r = _live_relay(relay)
    _segments(r.root, 40)          # newest seg00039
    r._check_underfeed(160.0)      # opens the cadence window
    _segments(r.root, 43)          # +3 in 20s: 0.375x -- under-fed
    r._check_underfeed(180.0)
    assert r._fail_streak == 1
    assert r.proc.killed == 0      # one bad window is not enough
    _segments(r.root, 46)          # +3 more: still 0.375x
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
    r._check_underfeed(120.0)      # age 20s < 45s: not judged
    assert r._eval_t0 is None
    _segments(r.root, 40)
    r._proc_born = 100.0
    r._check_underfeed(160.0)      # opens the window
    r._check_underfeed(165.0)      # only 5s in: no evaluation yet
    assert r._eval_t0 is not None
    assert r.proc.killed == 0
