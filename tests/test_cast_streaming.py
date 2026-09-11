# Copyright (c) serrebidev and contributors
# This file is part of Caster
# SPDX-License-Identifier: MIT
"""Startup and receiver-freeze regressions, without external services."""
import types
import subprocess

import pytest

import caster


def test_unresponsive_encoder_drivers_have_one_startup_budget(monkeypatch):
    clock = [0.0]
    waits = []

    def hangs(cmd, **kwargs):
        waits.append(kwargs["timeout"])
        clock[0] += kwargs["timeout"]
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(caster, "_encoder_cache", None)
    monkeypatch.setattr(caster, "_find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(caster.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(caster.subprocess, "run", hangs)
    assert caster.pick_h264_encoder() == "libx264"
    assert sum(waits) <= caster.ENCODER_PROBE_BUDGET
    assert max(waits) <= caster.ENCODER_PROBE_TIMEOUT


def playlist(path, durations, target=2):
    lines = ["#EXTM3U", f"#EXT-X-TARGETDURATION:{target}",
             "#EXT-X-MEDIA-SEQUENCE:0"]
    for i, duration in enumerate(durations):
        lines += [f"#EXTINF:{duration},", f"seg{i:05d}.ts"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_fractional_framerate_can_start_with_three_segments(tmp_path, monkeypatch):
    relay = caster.HlsRelay("http://example.invalid/live.ts", codecs=["h264", "aac"])
    relay.root = str(tmp_path)
    path = tmp_path / "live.m3u8"
    playlist(path, [2.002] * 3)
    monkeypatch.setattr(caster.time, "sleep", lambda _: pytest.fail("unnecessary startup wait"))
    assert relay._prime(str(path), 3) is False


def test_prime_respects_the_declared_target_duration(tmp_path, monkeypatch):
    relay = caster.HlsRelay("http://example.invalid/live.ts", codecs=["h264", "aac"])
    relay.root = str(tmp_path)
    relay.proc = types.SimpleNamespace(poll=lambda: None)
    path = tmp_path / "live.m3u8"
    playlist(path, [2.002] * 3, target=3)
    waited = []

    def advance(_):
        waited.append(True)
        playlist(path, [2.002] * 5, target=3)

    monkeypatch.setattr(caster.time, "sleep", advance)
    assert relay._prime(str(path), 3) is False
    assert waited == [True]


def test_throttled_connection_is_rotated_during_startup(tmp_path, monkeypatch):
    relay = caster.HlsRelay("http://example.invalid/live.ts", codecs=["h264", "aac"], live=True)
    relay.root = str(tmp_path)
    relay.proc = types.SimpleNamespace(poll=lambda: None)
    relay._proc_born = 0
    path = tmp_path / "live.m3u8"
    playlist(path, [2.002])
    clock = [12.0]
    rotations = []

    def rotate():
        rotations.append(clock[0])
        playlist(path, [2.002] * 3)

    relay.ts_source = types.SimpleNamespace(rotate=rotate)
    monkeypatch.setattr(caster.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(caster.time, "sleep", lambda _: clock.__setitem__(0, clock[0] + 1))
    assert relay._prime(str(path), 3) is False
    assert rotations == [27.0]


def test_served_cushion_uses_the_ratchet_target(tmp_path):
    relay = caster.HlsRelay("http://example.invalid/live.ts", trail_seconds=0)
    relay.root = str(tmp_path)
    path = tmp_path / "live.m3u8"
    playlist(path, [10] * 3, target=10)
    relay.trailing_playlist()
    playlist(path, [2] * 20, target=2)
    body = relay.trailing_playlist().decode()
    assert "#EXT-X-TARGETDURATION:10" in body
    durations = [float(line.split(":")[1].rstrip(",")) for line in body.splitlines()
                 if line.startswith("#EXTINF:")]
    assert sum(durations) >= 30


@pytest.mark.parametrize("live,expected_wait", [(True, True), (False, False)])
def test_startup_cushion_counts_seconds_only_for_live_media(tmp_path, monkeypatch, live, expected_wait):
    relay = caster.HlsRelay("http://example.invalid/live.ts", codecs=["h264", "aac"],
                            live=live, startup_seconds=12)
    relay.root = str(tmp_path)
    relay.proc = types.SimpleNamespace(poll=lambda: None)
    path = tmp_path / "live.m3u8"
    playlist(path, [2.002] * 3)
    waits = []

    def advance(_):
        waits.append(True)
        playlist(path, [2.002] * 6)

    monkeypatch.setattr(caster.time, "sleep", advance)
    assert relay._prime(str(path), 3) is False
    assert bool(waits) is expected_wait


def test_cast_uses_buffer_only_for_its_own_live_relay(frame):
    url = "http://192.0.2.1:1234/live.m3u8"
    relay = types.SimpleNamespace(play_url=url, live=True)
    frame._relays = [relay]
    assert frame._cast_load_options(url) == {"current_time": 0}
    assert frame._cast_load_options("https://example.invalid/live.m3u8") == {}
    relay.live = False
    assert frame._cast_load_options(url) == {}


def test_long_gop_is_promoted_before_waiting_for_startup_cushion(tmp_path, monkeypatch):
    relay = caster.HlsRelay("http://example.invalid/live.ts", codecs=["h264", "aac"],
                            live=True, startup_seconds=16)
    relay.root = str(tmp_path)
    path = tmp_path / "live.m3u8"
    playlist(path, [7.5], target=8)
    monkeypatch.setattr(caster.time, "sleep", lambda _: pytest.fail("waited for more long GOPs"))
    assert relay._prime(str(path), 3) is True


@pytest.fixture
def watchdog(frame, monkeypatch):
    clock = [100.0]
    calls = []
    status = types.SimpleNamespace(player_state="BUFFERING", current_time=16.0, media_session_id=7)
    mc = types.SimpleNamespace(status=status, play_media=lambda *a, **kw: calls.append((a, kw)))
    cast = types.SimpleNamespace(media_controller=mc)
    frame._cast_live_loads["TV"] = (cast, "http://example.invalid/live.m3u8",
                                     "application/vnd.apple.mpegurl", "LIVE")
    frame._ensure_receiver = lambda _: None
    frame._await_playing = lambda *a: True
    frame._ui = lambda *a, **kw: None
    monkeypatch.setattr(caster.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(caster.threading, "Thread", lambda target, **kw:
                        types.SimpleNamespace(start=target))
    return frame, status, clock, calls


@pytest.mark.parametrize("state", ["BUFFERING", "PLAYING"])
def test_frozen_receiver_reloads_existing_media_with_cooldown(watchdog, state):
    frame, status, clock, calls = watchdog
    status.player_state = state
    frame._recover_live_casts()
    clock[0] += frame.CAST_STALL_TIMEOUT - 1
    frame._recover_live_casts()
    assert not calls
    clock[0] += 1
    frame._recover_live_casts()
    assert len(calls) == 1
    assert calls[0][0][0] == "http://example.invalid/live.m3u8"
    clock[0] += 1
    frame._recover_live_casts()
    assert len(calls) == 1


def test_progress_pause_and_new_session_reset_freeze_detection(watchdog):
    frame, status, clock, calls = watchdog
    for i in range(4):
        status.current_time += 1
        clock[0] += 21
        frame._recover_live_casts()
    status.player_state = "PAUSED"
    clock[0] += 50
    frame._recover_live_casts()
    status.player_state = "BUFFERING"
    frame._recover_live_casts()
    clock[0] += 25
    status.media_session_id += 1
    frame._recover_live_casts()
    assert not calls


def test_stopped_frozen_load_is_not_restarted(watchdog):
    frame, status, clock, calls = watchdog
    frame._recover_live_casts()
    clock[0] += 21
    frame._ensure_receiver = lambda _: frame._cast_live_loads.clear()
    frame._recover_live_casts()
    assert not calls


def test_idle_recovery_is_rate_limited_and_uses_owned_relay_buffer(watchdog):
    frame, status, clock, calls = watchdog
    url = frame._cast_live_loads["TV"][1]
    frame._relays = [types.SimpleNamespace(play_url=url, live=True)]
    status.player_state = "IDLE"
    frame._recover_live_casts()
    assert calls[0][1]["current_time"] == 0
    clock[0] += frame.CAST_RECOVERY_INTERVAL - 1
    frame._recover_live_casts()
    assert len(calls) == 1
    clock[0] += 1
    frame._recover_live_casts()
    assert len(calls) == 2


def test_prime_can_be_cancelled_before_any_media(tmp_path):
    relay = caster.HlsRelay("http://example.invalid/live.ts", live=True)
    relay.root = str(tmp_path)
    with pytest.raises(RuntimeError, match="relay stopped while starting"):
        relay._prime(str(tmp_path / "live.m3u8"), 3)
