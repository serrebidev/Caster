# Copyright (c) serrebidev and contributors
# This file is part of Caster
# SPDX-License-Identifier: MIT
"""Tests that need the real world: a live stream, a real receiver, ffmpeg.

All marked `live` and skipped by default. Run them with:

    py -m pytest -m live

They are configured from the environment so nobody's addresses are baked in:

    CASTER_TEST_STREAM     an http(s) live MPEG-TS URL
    CASTER_TEST_MUSICCAST  the IP of a Yamaha MusicCast receiver

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
needs_receiver = pytest.mark.skipif(not MUSICCAST,
                                    reason="set CASTER_TEST_MUSICCAST")


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
