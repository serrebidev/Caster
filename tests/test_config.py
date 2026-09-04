"""Settings persistence and the capture presets.

Everything here is pure data and file IO: no network, no devices, no
subprocesses. The settings file is the only thing that survives a restart,
so a bug in it is a bug the user meets every launch, and QUALITY_PRESETS
carries a receiver rule (3x TARGETDURATION) that is invisible in the numbers
themselves -- these tests are where it is written down as an assertion.
"""
from __future__ import annotations

import json
import os

import pytest

import caster_config
from caster_config import DEFAULTS, QUALITY_PRESETS, Settings, preset


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@pytest.fixture
def appdata(tmp_path, monkeypatch):
    """Point config_dir() at a temp tree.

    Settings.save() writes its temp file into config_dir(), not into the
    directory of self.path, so a save from a test would otherwise create
    and write inside the real %APPDATA%\\Caster.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    return tmp_path


def write_settings_file(path, payload) -> str:
    with open(path, "w", encoding="utf-8") as handle:
        if isinstance(payload, str):
            handle.write(payload)
        else:
            json.dump(payload, handle)
    return str(path)


def test_defaults_are_all_readable(settings):
    """Guards against a key existing in DEFAULTS but not reaching a reader.

    Every caller does settings["some_key"]; a key that came back None because
    the read path forgot the defaults would silently disable a feature (a
    False-y "speak_status" makes the app stop talking to NVDA).
    """
    assert DEFAULTS, "DEFAULTS must not be empty"
    for key, value in DEFAULTS.items():
        assert settings[key] == value
        assert settings.get(key) == value


def test_load_merges_an_older_file_rather_than_replacing(tmp_path):
    """Guards against an upgrade wiping every setting added since.

    A settings.json written by an older build has no key for a feature added
    later. If load() replaced the dict instead of merging into the defaults,
    that key would read as missing and the new feature would be dead on an
    upgraded install but fine on a fresh one -- the worst kind of bug report.
    """
    path = write_settings_file(tmp_path / "settings.json",
                               {"volume": 40, "muted": True})
    s = Settings(path=path)
    assert s["volume"] == 40           # what the old file said is kept
    assert s["muted"] is True
    # ... and a key added since still has its default rather than nothing.
    assert s["capture_quality"] == DEFAULTS["capture_quality"]
    assert s["auto_reconnect"] is DEFAULTS["auto_reconnect"]
    for key in DEFAULTS:
        assert s[key] is not None or DEFAULTS[key] is None


@pytest.mark.parametrize("key, bad", [
    ("volume", "loud"),                 # str where an int is wanted
    ("muted", "yes"),                   # str where a bool is wanted
    ("recent_urls", "http://a/"),       # str where a list is wanted
    ("favourites", {"name": "x"}),      # dict where a list is wanted
    ("capture_quality", 3),             # int where a str is wanted
    ("sleep_timer_minutes", None),      # null where an int is wanted
    ("kodi_username", ["me"]),          # list where a str is wanted
])
def test_wrong_type_in_the_file_is_rejected(tmp_path, key, bad):
    """Guards against a hand-edited or corrupted value crashing the app later.

    The settings file is plain JSON a user can open. A string where an int is
    expected does not fail at load -- it fails much later, inside
    max(0, min(100, int(level))) on the volume path or when a list is
    iterated. Rejecting the value at the door keeps the failure local.
    """
    path = write_settings_file(tmp_path / "settings.json", {key: bad})
    s = Settings(path=path)
    assert s[key] == DEFAULTS[key]


def test_a_bool_is_rejected_for_an_int_setting(tmp_path):
    """A JSON `true` in a numeric field must not survive as 1.

    In Python a bool IS an int, so isinstance(True, int) passes and a
    hand-edited `"volume": true` used to load as True -- which the volume path
    then treats as 1, i.e. near-silence, with no sign anything was wrong.
    """
    path = write_settings_file(tmp_path / "settings.json", {"volume": True})
    s = Settings(path=path)
    assert s["volume"] == DEFAULTS["volume"]
    assert not isinstance(s["volume"], bool)


def test_a_number_is_rejected_for_a_bool_setting(tmp_path):
    """And the reverse: 1 is not True for a flag."""
    path = write_settings_file(tmp_path / "settings.json",
                               {"discover_on_launch": 0})
    s = Settings(path=path)
    assert s["discover_on_launch"] is DEFAULTS["discover_on_launch"]


def test_save_writes_beside_the_file_it_is_saving(tmp_path, monkeypatch):
    """The temp file belongs next to the target, not in %APPDATA%.

    Writing it into config_dir() regardless meant a caller who passed path=
    still had %APPDATA%\\Caster created for them, and an os.replace across two
    volumes raises OSError -- which save() swallows, so the settings would
    silently never persist.
    """
    import caster_config

    called = []
    monkeypatch.setattr(caster_config, "config_dir",
                        lambda: called.append(True) or str(tmp_path / "nope"))
    target = tmp_path / "elsewhere" / "settings.json"
    s = Settings(path=str(target))
    s.set("volume", 42)
    assert target.exists(), "settings were not written where asked"
    assert not (tmp_path / "nope").exists(), "fell back to the default folder"
    assert Settings(path=str(target))["volume"] == 42


@pytest.mark.parametrize("body", [
    "{not json at all",
    "",
    "[1, 2, 3]",                       # valid JSON, wrong shape
    '"just a string"',
    "null",
])
def test_corrupt_or_unreadable_json_falls_back_to_defaults(tmp_path, body):
    """Guards against the app refusing to start over a half-written file.

    A power cut during a save, or a file truncated by anything else, must cost
    the user their preferences and nothing more. Raising here would mean an
    app that cannot launch until the user finds and deletes a JSON file --
    which a blind user cannot be expected to do by touch.
    """
    path = write_settings_file(tmp_path / "settings.json", body)
    s = Settings(path=path)             # must not raise
    assert s["volume"] == DEFAULTS["volume"]
    assert s["capture_quality"] == DEFAULTS["capture_quality"]


def test_missing_file_falls_back_to_defaults(tmp_path):
    """Guards against first run being a crash rather than a default profile."""
    s = Settings(path=str(tmp_path / "never-written.json"))
    assert s["volume"] == DEFAULTS["volume"]


def test_unreadable_path_falls_back_to_defaults(tmp_path):
    """Guards against a directory (or any OSError) at the settings path.

    open() on a directory raises OSError, not ValueError; both have to be
    caught or a profile with a stray folder there would never launch.
    """
    (tmp_path / "settings.json").mkdir()
    s = Settings(path=str(tmp_path / "settings.json"))
    assert s["volume"] == DEFAULTS["volume"]


def test_save_writes_a_temp_file_then_replaces(appdata, monkeypatch):
    """Guards against a save that truncates the live file before writing it.

    Saves are frequent and tiny -- every volume nudge is one. A save that
    wrote in place would leave a zero-length settings.json if the machine went
    down mid-write, and that reads back as "no settings at all". The temp file
    must be complete and valid BEFORE it takes the real path's place.
    """
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        assert os.path.exists(src), "replace() ran before the temp was written"
        with open(src, "r", encoding="utf-8") as handle:
            seen.append((src, dst, json.load(handle)))   # complete JSON
        return real_replace(src, dst)

    monkeypatch.setattr(caster_config.os, "replace", spy)
    path = str(appdata / "settings.json")
    s = Settings(path=path)
    s.set("volume", 42)

    assert len(seen) == 1
    src, dst, payload = seen[0]
    assert src != dst, "must not write over the live file in place"
    assert dst == path
    assert payload["volume"] == 42
    assert not os.path.exists(src), "the temp file must not be left behind"


def test_save_round_trips_through_a_new_instance(appdata):
    """Guards against settings that appear to save but are gone next launch."""
    path = str(appdata / "settings.json")
    s = Settings(path=path)
    s.set("volume", 37)
    s.set("capture_quality", "quality")
    s.add_recent_url("http://example.invalid/stream.m3u8")
    s.add_favourite("Radio", "http://example.invalid/radio")
    s.note_device("Living Room")

    again = Settings(path=path)
    assert again["volume"] == 37
    assert again["capture_quality"] == "quality"
    assert again["recent_urls"] == ["http://example.invalid/stream.m3u8"]
    assert again["favourites"] == [{"name": "Radio",
                                    "url": "http://example.invalid/radio"}]
    assert again["last_devices"] == ["Living Room"]


def test_set_with_an_unchanged_value_does_not_rewrite(settings):
    """Guards against a settings file rewritten on every UI event.

    The volume slider and the device list fire set() constantly with the value
    already stored. Saving each one is a file write per keystroke, and every
    one of those is a window in which a crash truncates the file.
    """
    calls = []
    settings.save = lambda: calls.append(1)

    settings.set("volume", settings["volume"])       # same value
    assert calls == []

    settings.set("volume", 11)                       # genuinely different
    assert len(calls) == 1

    settings.set("volume", 11)                       # same again
    assert len(calls) == 1


def test_set_with_save_false_does_not_write(settings):
    """Guards against save=False silently still hitting the disk."""
    calls = []
    settings.save = lambda: calls.append(1)
    settings.set("volume", 5, save=False)
    assert calls == []
    assert settings["volume"] == 5


def test_update_writes_once_for_many_changed_keys(settings):
    """Guards against a batch update costing one file write per key."""
    calls = []
    settings.save = lambda: calls.append(1)
    settings.update(volume=8, muted=True, auto_reconnect=False)
    assert len(calls) == 1
    settings.update(volume=8, muted=True)            # nothing new
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# QUALITY_PRESETS
# ---------------------------------------------------------------------------

REQUIRED_PRESET_KEYS = ("fps", "bitrate", "max_width", "max_height",
                        "keyframe_seconds", "hls_time", "hls_prime",
                        "hls_trail", "label")


@pytest.mark.parametrize("name", sorted(QUALITY_PRESETS))
def test_every_preset_defines_every_key(name):
    """Guards against a KeyError deep in the connect path.

    _make_relay() and ScreenSource both read these by name at cast time. A
    preset missing a key does not fail at import; it fails when the user picks
    that quality and presses cast, which is the worst place to find out.
    """
    values = QUALITY_PRESETS[name]
    for key in REQUIRED_PRESET_KEYS:
        assert key in values, f"{name} preset is missing {key!r}"
    assert isinstance(values["label"], str) and values["label"]
    assert isinstance(values["bitrate"], str) and values["bitrate"]
    assert values["fps"] > 0
    assert values["max_width"] > 0 and values["max_height"] > 0
    assert values["keyframe_seconds"] > 0
    assert values["hls_time"] > 0


@pytest.mark.parametrize("name", sorted(QUALITY_PRESETS))
def test_hls_prime_is_exactly_the_three_segment_floor(name):
    """Guards both ways against retuning hls_prime.

    A Chromecast refuses to start playing an HLS stream until it has at least
    3x TARGETDURATION buffered, so priming fewer than three segments produces
    a cast that simply never starts.

    It is not raised above 3 either, and that is the half people get wrong:
    with `-c copy` ffmpeg can only cut at a keyframe, so a segment is as long
    as the SOURCE keyframe interval however short hls_time is. On a channel
    with a 7.5s GOP, six segments is a 45-second wait, not six seconds. Three
    segments satisfy the receiver whatever their length, so more segments buy
    a longer wait, not a bigger cushion.
    """
    assert QUALITY_PRESETS[name]["hls_prime"] >= 3
    assert QUALITY_PRESETS[name]["hls_prime"] == 3, (
        "raising hls_prime does not deepen the cushion, it only delays the "
        "start on a long-GOP source")


@pytest.mark.parametrize("name", sorted(QUALITY_PRESETS))
def test_hls_trail_also_clears_the_three_segment_rule(name):
    """Guards against a served window smaller than the receiver's minimum.

    hls_trail is how many segments stay in the playlist. Priming three and
    then keeping fewer than three means the receiver is handed a window that
    breaks the same 3x TARGETDURATION rule the prime exists to satisfy, and it
    stalls at the point the first segment ages out.
    """
    values = QUALITY_PRESETS[name]
    assert values["hls_trail"] >= 3
    assert values["hls_trail"] >= values["hls_prime"], (
        "the window served must not be shallower than the window primed")


def test_latency_preset_is_actually_the_shallowest():
    """Guards against the three presets drifting until the labels lie.

    hls_trail is delay second for second, so "Lowest delay" has to hold the
    least and "Best picture" the most. A copy-paste edit that left them equal
    would make the quality choice do nothing the user can hear.
    """
    trail = {n: QUALITY_PRESETS[n]["hls_trail"]
             for n in ("latency", "balanced", "quality")}
    assert trail["latency"] < trail["balanced"] < trail["quality"]
    assert (QUALITY_PRESETS["latency"]["max_height"]
            <= QUALITY_PRESETS["balanced"]["max_height"])


@pytest.mark.parametrize("name", ["latency", "balanced", "quality"])
def test_preset_returns_the_named_preset(name):
    """Guards against preset() losing the caller's choice."""
    assert preset(name) is QUALITY_PRESETS[name]


@pytest.mark.parametrize("name", ["", "nonsense", "LATENCY", None, "Balanced"])
def test_unknown_preset_name_falls_back_to_balanced(name):
    """Guards against an unknown quality name crashing the cast.

    The name comes out of the settings file, so it can be anything a user
    typed or an older build wrote. Falling back to balanced means a bad value
    costs the user their preferred quality, not their cast.
    """
    assert preset(name) is QUALITY_PRESETS["balanced"]


def test_capture_quality_default_names_a_real_preset():
    """Guards against the default setting pointing at a preset that is gone."""
    assert DEFAULTS["capture_quality"] in QUALITY_PRESETS
