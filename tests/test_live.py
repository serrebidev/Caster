# Copyright (c) serrebidev and contributors
# This file is part of Caster
# SPDX-License-Identifier: MIT
"""Tests that need the real world: a live stream, a real receiver, ffmpeg.

All marked `live` and skipped by default. Run them with:

    py -m pytest -m live

They are configured from the environment so nobody's addresses are baked in:

    CASTER_TEST_STREAM     an http(s) live MPEG-TS URL
    CASTER_TEST_MUSICCAST  the IP of a Yamaha MusicCast receiver
    CASTER_TEST_CHROMECAST the friendly name of a Cast receiver
    CASTER_TEST_AIRPLAY    the name of an AirPlay (RAOP) receiver

Anything not configured is skipped rather than failed.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time

import pytest

pytestmark = pytest.mark.live

STREAM = os.environ.get("CASTER_TEST_STREAM", "")
MUSICCAST = os.environ.get("CASTER_TEST_MUSICCAST", "")

needs_stream = pytest.mark.skipif(not STREAM, reason="set CASTER_TEST_STREAM")
CHROMECAST = os.environ.get("CASTER_TEST_CHROMECAST", "")
AIRPLAY = os.environ.get("CASTER_TEST_AIRPLAY", "")

needs_receiver = pytest.mark.skipif(not MUSICCAST,
                                    reason="set CASTER_TEST_MUSICCAST")
#: How long to watch the receiver. It has to outlast the deep cushion the
#: relay holds it behind (45s at the balanced preset) several times over,
#: or a stall that only shows up once the cushion drains is simply missed.
CAST_WATCH = 240

needs_cast = pytest.mark.skipif(not (STREAM and CHROMECAST),
                                reason="set CASTER_TEST_STREAM and "
                                       "CASTER_TEST_CHROMECAST")
needs_airplay = pytest.mark.skipif(not AIRPLAY,
                                   reason="set CASTER_TEST_AIRPLAY")


def _port_open(host, port, timeout=1.0):
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# The relay against a real live source
# ---------------------------------------------------------------------------

@needs_stream
def test_relay_does_not_duplicate_the_source():
    """The stream must not be delivered to us twice over.

    This is the regression guard for the bug that made playback jump
    backwards every ten to twenty seconds. IPTV servers close the connection
    constantly; ffmpeg used to reconnect by asking for `Range: bytes=<offset>`,
    which a live stream cannot honour, so the server replied with its current
    live edge and ffmpeg spliced it in as though it followed on. The overlap
    was media the listener had already heard.

    The tell is arithmetic and needs no ear: a live source cannot PRODUCE more
    than one second of media per second. Measured on a real channel this was
    1.88x before `-seekable 0` and 0.89x after. Anything comfortably above 1.0
    means material is arriving twice.
    """
    import caster

    relay = caster.HlsRelay(STREAM, hls_time=2, prime_segments=3,
                            trail_keep=8, codecs=["h264", "aac"])
    watch = 60
    try:
        relay.start()
        assert relay.root, "relay.start() left no working directory"
        playlist = os.path.join(relay.root, "live.m3u8")
        time.sleep(watch)
        body = open(playlist, encoding="utf-8", errors="replace").read()
    finally:
        relay.stop()

    durations = [float(x) for x in re.findall(r"#EXTINF:([\d.]+)", body)]
    rolled = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", body)
    assert durations, "the relay produced no segments"
    average = sum(durations) / len(durations)
    produced = sum(durations) + (int(rolled.group(1)) if rolled else 0) * average
    ratio = produced / watch
    assert ratio <= 1.15, (
        f"{ratio:.2f}x of real time produced -- the source is being "
        f"delivered more than once (see -seekable 0)")


@needs_stream
def test_relay_ffmpeg_never_resumes_at_a_byte_offset():
    """ffmpeg must not ask a live server to resume at a byte position.

    "Will reconnect at <offset>" in the log is the fault itself: the request
    goes out as a byte range, the server answers with live data, and the two
    are joined into a stream containing the same audio twice, with a seam of
    corrupt packets.
    """
    import caster
    import caster_extras as ce

    cmd = [ce._find_ffmpeg(), "-hide_banner", "-loglevel", "warning"]
    relay = caster.HlsRelay(STREAM, codecs=["h264", "aac"])
    relay.root = os.environ.get("TEMP", ".")
    built = relay._ffmpeg_cmd(os.devnull)
    assert "-seekable" in built and built[built.index("-seekable") + 1] == "0"

    # And prove it against the real server, not just in the argument list.
    cmd = built[:built.index("-f")] + ["-t", "45", "-f", "null", "-"]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                          **ce._no_window_kwargs())
    resumes = re.findall(r"Will reconnect at \d+", done.stderr)
    assert not resumes, f"{len(resumes)} byte-offset resumes: {resumes[:3]}"


@needs_stream
def test_relay_starts_serves_and_leaves_nothing_behind():
    """A relay must free its port, its thread and its temp directory.

    Leaking any of them costs one per cast, for the life of the app.
    """
    import urllib.request as ur

    import caster

    before = {d for d in os.listdir(os.environ.get("TEMP", "."))
              if d.startswith("caster_hls_")}
    relay = caster.HlsRelay(STREAM, hls_time=2, prime_segments=3,
                            trail_keep=8, codecs=["h264", "aac"])
    url = relay.start()
    port = relay.port
    with ur.urlopen(url, timeout=10) as response:
        body = response.read().decode()
    assert body.startswith("#EXTM3U")
    assert _port_open("127.0.0.1", port), "relay is not listening"

    relay.stop()
    time.sleep(1.5)
    assert not _port_open("127.0.0.1", port), "relay port stayed open"
    assert relay.root is None
    after = {d for d in os.listdir(os.environ.get("TEMP", "."))
             if d.startswith("caster_hls_")}
    assert after <= before, f"temp directories left behind: {after - before}"


# ---------------------------------------------------------------------------
# A real MusicCast receiver
# ---------------------------------------------------------------------------

@pytest.fixture
def receiver():
    """The receiver, with its volume and input restored afterwards."""
    import caster_devices as cd
    if not cd.yxc_available(MUSICCAST):
        pytest.skip(f"no MusicCast receiver answering at {MUSICCAST}")
    status = cd.yxc_status(MUSICCAST)
    volume, source = status.get("volume"), status.get("input")
    try:
        yield cd
    finally:
        if volume is not None:
            cd.yxc_try(MUSICCAST, f"main/setVolume?volume={volume}")
        if source:
            cd.yxc_set_input(MUSICCAST, source)


@needs_receiver
def test_receiver_reports_its_own_volume_scale(receiver):
    """Volume is the receiver's scale, not a percentage.

    An AVENTAGE-era receiver has 161 steps. Treating the slider as if the
    receiver used 0-100 lands between real steps and throws away most of the
    resolution -- and 100 means maximum output on something wired to real
    speakers.
    """
    top = receiver.yxc_max_volume(MUSICCAST, "main")
    assert top > 100, f"expected a scale finer than 0-100, got {top}"
    percent = receiver.yxc_get_volume(MUSICCAST, "main")
    assert 0 <= percent <= 100
    assert receiver.yxc_volume_db(MUSICCAST, "main"), "no dB readback"


@needs_receiver
def test_caster_never_moves_the_volume_on_its_own(frame, receiver):
    """Only a change the user asked for may reach the amplifier.

    Caster's remembered volume defaults to 100. Replaying that at a receiver
    on a 161-step scale is maximum output, unannounced. The receiver's own
    level is the authority; the app follows it.
    """
    import caster

    device = caster.Device("airplay", "receiver", None)
    device.musiccast = {"host": MUSICCAST, "zone": "main", "model": "test"}
    frame._targets = [device]

    class Recorder(dict):
        def set(self, key, value, save=True):
            self[key] = value

        def __getitem__(self, key):
            return self.get(key, "")
    frame.settings = Recorder(volume=100)

    before = receiver.yxc_status(MUSICCAST).get("volume")
    frame.apply_volume(100)                      # implicit: must not apply
    time.sleep(2.5)
    assert receiver.yxc_status(MUSICCAST).get("volume") == before

    frame.apply_volume(20, user=True)            # deliberate: must apply
    time.sleep(2.5)
    top = receiver.yxc_max_volume(MUSICCAST, "main")
    assert abs(receiver.yxc_status(MUSICCAST).get("volume")
               - round(20 * top / 100)) <= 2


@needs_receiver
def test_input_switching_prepares_first(receiver):
    """prepareInputChange precedes setInput, as the spec requires.

    MusicCast's own controller does this before every input change, and the
    documentation makes it a requirement whenever the unit advertises
    prepare_input_change.
    """
    assert receiver.yxc_can(MUSICCAST, "prepare_input_change", "main")
    receiver.yxc_set_input(MUSICCAST, "tv")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if receiver.yxc_current_input(MUSICCAST) == "tv":
            break
        time.sleep(0.5)
    assert receiver.yxc_current_input(MUSICCAST) == "tv"


@needs_receiver
def test_features_are_read_from_the_unit_not_assumed(receiver):
    """Capabilities come from getFeatures, never from a model name.

    Zones differ: main has tone and link controls that zone2 does not, and
    the unit says so itself.
    """
    zones = receiver.yxc_zones(MUSICCAST)
    assert zones and zones[0] == "main"
    assert receiver.yxc_can(MUSICCAST, "volume", "main")
    for zone in zones[1:]:
        assert receiver.yxc_can(MUSICCAST, "volume", zone)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_discovery_is_repeatable():
    """SSDP is lossy; one M-SEARCH is not enough.

    A single search missed a receiver in roughly one scan out of two, because
    it is on Wi-Fi and multicast replies that collide are simply gone. The
    search is repeated across the window, so a device found once should be
    found every time.
    """
    import caster_extras as ce

    scans = [ {name for name, _, _, _ in ce.upnp_discover(timeout=5)}
              for _ in range(2) ]
    if not any(scans):
        pytest.skip("no UPnP renderers on this network")
    stable = scans[0] & scans[1]
    assert stable, (
        f"no device survived two scans -- discovery is unreliable: {scans}")


@pytest.mark.live
def test_ffmpeg_is_available():
    """Everything downstream assumes ffmpeg can be found."""
    import caster_extras as ce
    path = ce._find_ffmpeg()
    assert os.path.exists(path) or shutil.which(path)


# ---------------------------------------------------------------------------
# A real Cast receiver, measured from the receiver's own side
# ---------------------------------------------------------------------------

CAST_APP_ID = "CC1AD845"


def _relay_for(url):
    """A relay built the way _make_relay builds one, at the saved preset."""
    import caster
    from caster_config import preset
    chosen = preset("balanced")
    return caster.HlsRelay(url, hls_time=chosen["hls_time"],
                           prime_segments=chosen["hls_prime"],
                           trail_keep=chosen["hls_trail"],
                           trail_seconds=chosen["hls_trail_seconds"],
                           codecs=["h264", "aac"], live=True)


def _watch_receiver(mc, seconds, every=2.0):
    """Sample the receiver.

    Returns (samples, freezes), where a freeze is a run longer than eight
    seconds during which current_time did not move.
    """
    samples, freezes = [], []
    t0 = last_change = time.monotonic()
    last, frozen_since = None, None
    while time.monotonic() - t0 < seconds:
        time.sleep(every)
        mc.update_status()
        now = time.monotonic()
        cur, state = mc.status.current_time, mc.status.player_state
        if cur != last:
            if frozen_since is not None:
                freezes.append((round(frozen_since - t0),
                                round(now - frozen_since, 1)))
                frozen_since = None
            last, last_change = cur, now
        elif now - last_change > 8 and frozen_since is None:
            frozen_since = last_change
        samples.append((round(now - t0), state, cur))
    if frozen_since is not None:
        freezes.append((round(frozen_since - t0),
                        round(time.monotonic() - frozen_since, 1)))
    return samples, freezes


@needs_cast
def test_the_receiver_keeps_playing_a_live_channel():
    """The verdict that relay measurements cannot give.

    A healthy relay proves nothing: a receiver was once stuck BUFFERING with
    its position frozen at 41.8s while the relay it was reading advanced past
    sequence 172. player_state and a current_time that advances are the only
    things that settle it, so this is the test that gets to say whether a
    change to the relay helped.

    It is written to REFUSE a verdict rather than give a false one. An IPTV
    source is not a fixed quantity: across four 360s casts of one channel on
    2026-09-09 the same code scored 145/180 samples PLAYING on one run and
    2/180 on another, purely because the channel degraded over the hour -- by
    the end it was serving 0.41x with segments up to 10.4s. Comparing two
    builds across that is measuring the weather. So the relay's own output is
    measured alongside the receiver and an under-delivering source SKIPS,
    because a red test here has to mean the relay broke.
    """
    import pychromecast

    relay = _relay_for(STREAM)
    cast = None
    casts, browser = pychromecast.get_chromecasts(timeout=10)
    try:
        cast = next((c for c in casts
                     if c.cast_info.friendly_name == CHROMECAST), None)
        if cast is None:
            pytest.skip(f"no Cast receiver named {CHROMECAST!r} on this network")
        cast.wait(timeout=15)
        play_url = relay.start()
        if cast.app_id != CAST_APP_ID:
            # play_media silently no-ops unless the receiver app is running.
            cast.start_app(CAST_APP_ID, force_launch=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and cast.app_id != CAST_APP_ID:
                time.sleep(0.05)
            time.sleep(0.25)

        mc = cast.media_controller
        mc.play_media(play_url, "application/vnd.apple.mpegurl",
                      stream_type="LIVE")
        mc.block_until_active(20)
        before = sum(relay._completed_segments().values())
        samples, freezes = _watch_receiver(mc, CAST_WATCH)
        produced = sum(relay._completed_segments().values()) - before
        rotated = relay._rotated
        try:
            mc.stop()
        except Exception:
            pass
    finally:
        relay.stop()
        if cast is not None:
            try:
                cast.disconnect()
            except Exception:
                pass
        browser.stop_discovery()

    states = [s[1] for s in samples]
    playing = states.count("PLAYING")
    advanced = (samples[-1][2] or 0) - next((s[2] for s in samples if s[2]), 0)
    report = (f"{playing}/{len(states)} PLAYING, advanced {advanced:.1f}s of "
              f"media in {CAST_WATCH}s, freezes: {freezes}")

    # Was the SOURCE good enough to judge the relay by? The relay cannot
    # serve media it was never sent, and a starved upstream is not a bug in
    # anything this test is allowed to fail.
    if rotated or produced < CAST_WATCH * 0.5:
        pytest.skip(f"source under-delivered, no verdict available -- {report}")

    assert playing >= 0.8 * len(states), f"receiver was not playing: {report}"
    assert advanced >= 0.75 * CAST_WATCH, f"receiver fell behind: {report}"
    assert not [f for _, f in freezes if f > 30], (
        f"a freeze this long does not recover on its own: {report}")


@needs_stream
def test_a_stretching_gop_never_reaches_the_receiver():
    """Whatever the source's keyframes do, the served segments stay short.

    With -c copy a segment can only end on a keyframe, so segment length IS
    the source GOP and a receiver at the live edge simply waits for the next
    one. Priming measures that once; _check_gop keeps measuring, because the
    spacing is not a constant -- one channel primed at 1.001s, 1.043s and
    0.959s and was writing 9.509s segments a hundred seconds later, which is
    a 9.5s stall no playlist tuning can shorten.
    """
    import caster

    relay = _relay_for(STREAM)
    seen = {}
    try:
        relay.start()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            time.sleep(3)
            seen.update(relay._completed_segments())
        promoted = relay.force_keyframes or relay.video_transcoded
    finally:
        relay.stop()

    assert seen, "the relay produced no segments"
    longest = max(seen.values())
    if longest > caster.HlsRelay.GOP_COPY_LIMIT:
        assert promoted, (
            f"a {longest:.1f}s segment reached the playlist while still on a "
            f"copy: _check_gop did not promote it (limit "
            f"{caster.HlsRelay.GOP_COPY_LIMIT}s)")


@needs_stream
def test_a_segment_stays_fetchable_after_it_leaves_the_playlist():
    """hls_delete_threshold: the grace a lagging receiver actually gets.

    The receiver starts at the OLDEST segment it is shown, so with ffmpeg's
    default threshold of 1 it begins one slip away from a 404 -- and a 404
    there is not a rebuffer but a permanent freeze, its position stuck while
    the playlist runs away from it.
    """
    import glob
    import re as _re

    relay = _relay_for(STREAM)
    listed, on_disk = [], []
    try:
        relay.start()
        # Deletion cannot start until the playlist has filled to list_size.
        deadline = (time.monotonic()
                    + (relay._list_size() + 20) * relay.hls_time)
        while time.monotonic() < deadline:
            time.sleep(5)
            listed = sorted(relay._completed_segments())
            on_disk = sorted(
                int(m.group(1)) for m in
                (_re.fullmatch(r"seg(\d+)\.ts", os.path.basename(f))
                 for f in glob.glob(os.path.join(relay.root, "seg*.ts"))) if m)
            if listed and on_disk and min(on_disk) > 0:
                break   # something has been deleted; the window is meaningful
    finally:
        grace = (min(listed) - min(on_disk)) if (listed and on_disk) else -1
        relay.stop()

    if grace < 0:
        pytest.skip("deletion never started; source too slow to fill the list")
    assert grace > 1, (
        f"only {grace} segments survive leaving the playlist -- a receiver at "
        f"the oldest advertised URI has no grace at all")


# ---------------------------------------------------------------------------
# A real AirPlay receiver
# ---------------------------------------------------------------------------

@needs_airplay
def test_the_airplay_receiver_accepts_a_raop_session():
    """RAOP still connects. Audio only -- pyatv cannot mirror a screen.

    This path never touches HlsRelay: _play_airplay builds its own ffmpeg
    pipe in _raop_source and hands PCM straight to pyatv, so relay work
    cannot fix it and must not break it. This is the guard for the latter.
    """
    import asyncio

    import pyatv
    from pyatv.const import Protocol

    async def connect_and_close():
        loop = asyncio.get_running_loop()
        found = await pyatv.scan(loop, timeout=8)
        conf = next((a for a in found if a.name == AIRPLAY), None)
        if conf is None:
            return None
        atv = await pyatv.connect(conf, loop, protocol=Protocol.RAOP)
        try:
            return str(conf.address)
        finally:
            # pyatv's close() returns the set of tasks it spawned to shut
            # down, not a coroutine; awaiting it directly is a TypeError.
            await asyncio.gather(*atv.close())

    address = asyncio.run(connect_and_close())
    if address is None:
        pytest.skip(f"no AirPlay receiver named {AIRPLAY!r} on this network")
    assert address


@needs_stream
def test_the_airplay_audio_pipe_produces_pcm():
    """The RAOP source pipe yields real PCM from a live TS channel.

    ffmpeg EXITING here is normal and not a failure: there are no reconnect
    flags on this path -- a byte-offset resume spliced already-heard media
    back in -- so a drop ends the pipe and _play_airplay's runner reopens it
    at the live edge. What must not happen is producing nothing at all.
    """
    import subprocess
    import threading

    import caster_extras as ce

    cmd = [ce._find_ffmpeg(), "-hide_banner", "-loglevel", "error",
           "-seekable", "0", "-rw_timeout", "5000000",
           "-analyzeduration", "1000000", "-probesize", "1000000",
           "-fflags", "+genpts+nobuffer", "-flags", "+low_delay",
           "-i", STREAM, "-vn", "-map", "a:0?",
           "-af", "aresample=44100:async=1000:first_pts=0",
           "-f", "wav", "-c:a", "pcm_s16le", "-flush_packets", "1", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, **ce._no_window_kwargs())
    read = {"n": 0}

    def pump():
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            read["n"] += len(chunk)

    threading.Thread(target=pump, daemon=True).start()
    try:
        time.sleep(20)
    finally:
        proc.kill()

    # 44100 Hz * 2 channels * 2 bytes = 176400 B/s. Ask for a few seconds'
    # worth rather than real time: the pipe is allowed to end early and be
    # reopened, and this test is only asking whether audio comes out at all.
    assert read["n"] > 3 * 176400, (
        f"only {read['n']} bytes of PCM in 20s -- the audio pipe is dry")
