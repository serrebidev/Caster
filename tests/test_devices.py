"""Sonos, Roku and MusicCast helpers, with every HTTP call faked.

Nothing here touches the network. The YXC helpers are driven through a stub
urlopen that records the URL of every request, because most of what matters
about them is not the answer they return but *which* calls they make, in what
order, and -- for the guards -- that they make none at all.

The `no_network` fixture is layered underneath as a tripwire: if a helper ever
grows a real socket, the test says so instead of hanging on a timeout.
"""
from __future__ import annotations

import io
import json

import pytest

import caster_devices
from caster_devices import (LINK_AUDIO_DELAYS, LINK_CONTROLS,
                            ROKU_AUDIO_FORMATS, ROKU_MEDIA_PLAYER,
                            ROKU_VIDEO_FORMATS, looks_like_sonos,
                            musiccast_group, sonos_didl, yxc_available,
                            yxc_can, yxc_features, yxc_max_volume,
                            yxc_request, yxc_set_input,
                            yxc_set_link_audio_delay, yxc_set_link_control,
                            yxc_set_volume, yxc_try)

HOST = "192.168.1.65"

#: A getFeatures document shaped like a real AVENTAGE-era receiver's: volume
#: runs 0-161, and main advertises things zone2 does not. Everything optional
#: in the app is gated on this list rather than on a model name, so the
#: asymmetry between the zones is the point of the fixture.
FEATURES = {
    "response_code": 0,
    "zone": [
        {"id": "main",
         "func_list": ["power", "volume", "mute", "sound_program",
                       "link_control", "link_audio_delay",
                       "prepare_input_change"],
         "range_step": [{"id": "volume", "min": 0, "max": 161, "step": 1}]},
        {"id": "zone2",
         "func_list": ["power", "volume", "mute"],
         "range_step": [{"id": "volume", "min": 0, "max": 100, "step": 1}]},
        # A zone that cannot do volume at all: a fixed-output zone is a real
        # MusicCast configuration, not an invented one.
        {"id": "zone3",
         "func_list": ["power"],
         "range_step": []},
    ],
}


class _Response(io.BytesIO):
    """Just enough of an http.client.HTTPResponse for `with urlopen(...)`."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@pytest.fixture(autouse=True)
def clear_yxc_cache():
    """getFeatures is cached per host for the life of the process.

    Without this, the first test to call yxc_can() would decide what every
    later test sees, and the caching test would pass or fail on ordering.
    """
    caster_devices._yxc_features_cache.clear()
    yield
    caster_devices._yxc_features_cache.clear()


@pytest.fixture
def yxc(monkeypatch):
    """A stub urlopen that records requested URLs and answers from a table.

    Returns the recording list plus a hook to change the answers, so a test
    can assert on the calls made as easily as on the value returned.
    """
    state = {"answers": {}, "default": {"response_code": 0}, "fail": False}
    urls: list = []

    def fake_urlopen(req, timeout=None, **kwargs):
        url = getattr(req, "full_url", req)
        urls.append(url)
        if state["fail"]:
            raise OSError("host did not answer")
        for needle, payload in state["answers"].items():
            if needle in url:
                if isinstance(payload, Exception):
                    raise payload
                return _Response(json.dumps(payload).encode())
        return _Response(json.dumps(state["default"]).encode())

    monkeypatch.setattr(caster_devices._urlreq, "urlopen", fake_urlopen)
    state["answers"]["system/getFeatures"] = FEATURES
    state["urls"] = urls
    return state


def paths(urls) -> list:
    """The YXC command out of each URL, for readable order assertions."""
    return [u.rsplit("/v1/", 1)[-1] for u in urls]


# ---------------------------------------------------------------------------
# YXC transport helpers
# ---------------------------------------------------------------------------

def test_yxc_request_raises_on_a_non_zero_response_code(yxc, no_network):
    """Guards against a receiver's own error being read as success.

    YXC answers HTTP 200 with an error inside the JSON body. Without this
    check, "input is not available now" (code 3) would come back as a dict the
    caller treats as a working call, and the cast would be pushed to a
    receiver that never switched input -- silently, which is the MusicCast
    failure mode the whole module exists to avoid.
    """
    yxc["answers"]["main/setInput"] = {"response_code": 3}
    with pytest.raises(RuntimeError) as err:
        yxc_request(HOST, "main/setInput?input=server")
    assert "3" in str(err.value)


def test_yxc_request_accepts_zero_and_a_missing_code(yxc, no_network):
    """Guards against rejecting a valid answer that omits response_code."""
    yxc["answers"]["main/getStatus"] = {"power": "on"}      # no code at all
    assert yxc_request(HOST, "main/getStatus") == {"power": "on"}
    yxc["answers"]["main/getStatus"] = {"response_code": 0, "power": "on"}
    assert yxc_request(HOST, "main/getStatus")["power"] == "on"


@pytest.mark.parametrize("boom", [
    OSError("no route to host"),
    ValueError("not JSON"),
    RuntimeError("MusicCast error 5"),
])
def test_yxc_try_swallows_every_failure(yxc, no_network, boom):
    """Guards against a decoration killing the cast it was decorating.

    Every yxc_try caller is doing something optional next to a cast that works
    regardless -- waking the amp, nudging volume, switching input. A receiver
    that is asleep, gone, or answering rubbish must cost the feature and
    nothing else, so the empty dict has to cover transport errors, JSON
    errors and YXC's own error codes alike.
    """
    yxc["answers"]["main/getStatus"] = boom
    assert yxc_try(HOST, "main/getStatus") == {}


def test_yxc_available_is_false_when_the_host_does_not_answer(yxc,
                                                              no_network):
    """Guards against every non-Yamaha device being probed as a MusicCast.

    Discovery asks this of each address that answered SSDP or mDNS. A True
    from a host that never replied would attach a MusicCast control panel to a
    TV and put a dead volume slider in front of the user.
    """
    yxc["fail"] = True
    assert yxc_available(HOST) is False
    assert yxc["urls"], "it must actually have tried"


def test_yxc_available_is_true_for_a_receiver_that_answers(yxc, no_network):
    """Guards against a working receiver being dropped as not MusicCast."""
    yxc["answers"]["main/getStatus"] = {"response_code": 0, "power": "on"}
    assert yxc_available(HOST) is True


def test_yxc_features_is_cached_per_host(yxc, no_network):
    """Guards against re-fetching a big static document on the connect path.

    getFeatures is large and slow and cannot change while the unit is running,
    yet yxc_can() consults it for every optional feature -- several times per
    cast. Uncached, that is a round trip per question on the one path where
    seconds are audible as the app hanging.
    """
    first = yxc_features(HOST)
    before = len(yxc["urls"])
    assert before == 1

    second = yxc_features(HOST)
    assert second == first
    assert len(yxc["urls"]) == before, "second call must issue no HTTP"

    # Several yxc_can() questions must also ride on the one cached answer.
    yxc_can(HOST, "volume", "main")
    yxc_can(HOST, "link_control", "main")
    yxc_can(HOST, "mute", "zone2")
    assert len(yxc["urls"]) == before

    # A different host is a different cache entry, so it does fetch.
    yxc_features("192.168.1.99")
    assert len(yxc["urls"]) == before + 1


def test_yxc_features_refresh_asks_again(yxc, no_network):
    """Guards against refresh=True being quietly served from the cache."""
    yxc_features(HOST)
    yxc_features(HOST, refresh=True)
    assert len(yxc["urls"]) == 2


# ---------------------------------------------------------------------------
# Per-zone capability gating
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("zone, func, expected", [
    ("main", "link_control", True),
    ("main", "sound_program", True),
    ("main", "prepare_input_change", True),
    ("zone2", "link_control", False),      # the unit says zone2 has none
    ("zone2", "sound_program", False),
    ("zone2", "volume", True),
    ("zone3", "volume", False),
    ("main", "teleport", False),           # not a YXC function at all
    ("nosuchzone", "volume", False),       # a zone the unit does not have
])
def test_yxc_can_reads_func_list_per_zone(yxc, no_network, zone, func,
                                          expected):
    """Guards against a feature being offered on a zone that lacks it.

    Everything optional is gated on the unit's own func_list rather than on a
    model name, and the list differs BETWEEN ZONES on one unit: main has
    link_control here and zone2 does not. Gating on the unit instead of the
    zone would put a Link Control control in front of a zone that answers it
    with an error code.
    """
    assert yxc_can(HOST, func, zone) is expected


# ---------------------------------------------------------------------------
# Volume: the receiver's own scale, not a percentage
# ---------------------------------------------------------------------------

def test_yxc_max_volume_reads_range_step_not_one_hundred(yxc, no_network):
    """Guards against treating the volume as a 0-100 percentage.

    An AVENTAGE-era receiver reports range_step 0-161 step 1 and shows real dB
    on its front panel. Assuming 100 steps throws away a third of the
    resolution and lands the volume between real steps, so the number the app
    speaks and the number on the unit disagree.
    """
    assert yxc_max_volume(HOST, "main") == 161
    assert yxc_max_volume(HOST, "main") != 100
    assert yxc_max_volume(HOST, "zone2") == 100      # this zone really is 100


def test_yxc_max_volume_falls_back_to_status_then_to_one_hundred(yxc,
                                                                 no_network):
    """Guards against a zone with no range_step returning zero steps.

    A max of 0 would make every volume set land on 0 -- silence that looks
    like a broken cast. Falling back to getStatus and then to 100 keeps the
    slider usable on a unit that does not publish its range.
    """
    yxc["answers"]["zone3/getStatus"] = {"response_code": 0, "max_volume": 60}
    assert yxc_max_volume(HOST, "zone3") == 60
    yxc["answers"]["zone3/getStatus"] = {"response_code": 0}
    assert yxc_max_volume(HOST, "zone3") == 100


@pytest.mark.parametrize("percent, expected", [
    (0, 0),
    (20, 32),        # round(20 * 161 / 100) == 32, not 20
    (50, 80),        # 80.5 rounds to even, which is 80
    (100, 161),
    (-10, 0),        # clamped, not negative
    (250, 161),      # clamped, not past the top of the scale
])
def test_yxc_set_volume_maps_the_slider_onto_the_zone_scale(
        yxc, no_network, percent, expected):
    """Guards against sending a percentage to a device that counts in steps.

    The app's slider is 0-100 because that is what a user (and NVDA) can work
    with. The receiver counts 0-161. Sending 20 straight through would be a
    fifth of the volume the user asked for.
    """
    assert yxc_set_volume(HOST, percent, "main") is True
    assert f"main/setVolume?volume={expected}" in paths(yxc["urls"])[-1]


def test_yxc_set_volume_refuses_a_zone_with_no_volume_function(yxc,
                                                               no_network):
    """Guards against commanding a fixed-output zone.

    zone3 does not list "volume", so a setVolume there returns a YXC error the
    user would hear as nothing happening. Refusing before the request is sent
    is what lets the UI hide the control instead.
    """
    assert yxc_set_volume(HOST, 50, "zone3") is False
    assert not any("setVolume" in p for p in paths(yxc["urls"]))


# ---------------------------------------------------------------------------
# Input switching: prepare, then set
# ---------------------------------------------------------------------------

def test_prepare_input_change_is_sent_before_set_input(yxc, no_network):
    """Guards against asking a receiver to switch and stream in one breath.

    The spec makes prepareInputChange a requirement whenever the unit lists
    prepare_input_change, and MusicCast's own controller sends it immediately
    before setInput. It lets the receiver spin up the network client first. If
    the order ever inverted, or the prepare were dropped, the unit would take
    the input change but ignore the pushed URL -- and give no error at all,
    which is the exact failure this app already has notes about.
    """
    assert yxc_set_input(HOST, "server", "main") is True
    calls = [p for p in paths(yxc["urls"]) if "Input" in p]
    assert len(calls) == 2
    assert calls[0].startswith("main/prepareInputChange?input=server")
    assert calls[1].startswith("main/setInput?input=server")
    assert (yxc["urls"].index(next(u for u in yxc["urls"]
                                   if "prepareInputChange" in u))
            < yxc["urls"].index(next(u for u in yxc["urls"]
                                     if "setInput" in u)))


def test_prepare_is_skipped_when_the_zone_does_not_advertise_it(yxc,
                                                                no_network):
    """Guards against sending a command the zone never said it supports.

    zone2 has no prepare_input_change in its func_list. Sending it anyway
    would earn a YXC error code, and yxc_set_input would then be one failed
    request slower on every cast to that zone for no gain.
    """
    assert yxc_set_input(HOST, "server", "zone2") is True
    calls = [p for p in paths(yxc["urls"]) if "Input" in p]
    assert len(calls) == 1
    assert calls[0].startswith("zone2/setInput?input=server")


# ---------------------------------------------------------------------------
# Link Control / Link Audio Delay: documented value sets only
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "fast", "STABILITY", "stability boost",
                                 "balanced", None, 0])
def test_link_control_rejects_undocumented_values_without_asking(yxc,
                                                                 no_network,
                                                                 bad):
    """Guards against a settings-file value reaching the receiver unchecked.

    musiccast_link_control comes out of settings.json, which a user can edit.
    Anything outside the documented set has to be refused here: sending it
    costs a round trip on the connect path and earns an error, and the value
    check must short-circuit BEFORE the getFeatures fetch so a bad value costs
    nothing at all.
    """
    assert yxc_set_link_control(HOST, bad, "main") is False
    assert yxc["urls"] == [], "a rejected value must issue no request"


@pytest.mark.parametrize("good", LINK_CONTROLS)
def test_link_control_accepts_every_documented_value(yxc, no_network, good):
    """Guards against the guard rejecting values the receiver does support."""
    assert yxc_set_link_control(HOST, good, "main") is True
    assert f"main/setLinkControl?control={good}" in paths(yxc["urls"])[-1]


def test_link_control_refuses_a_zone_that_lacks_it(yxc, no_network):
    """Guards against offering Link Control on zone2, which has none."""
    assert yxc_set_link_control(HOST, "standard", "zone2") is False
    assert not any("setLinkControl" in p for p in paths(yxc["urls"]))


@pytest.mark.parametrize("bad", ["", "lipsync", "LIP_SYNC", "stability",
                                 None, 3])
def test_link_audio_delay_rejects_undocumented_values_without_asking(
        yxc, no_network, bad):
    """Guards against the same settings-file hole on the delay setting.

    Note "stability" in the list: it is a valid LINK_CONTROLS value and an
    invalid LINK_AUDIO_DELAYS one, so a copy-paste between the two settings
    must be caught rather than sent.
    """
    assert yxc_set_link_audio_delay(HOST, bad, "main") is False
    assert yxc["urls"] == []


@pytest.mark.parametrize("good", LINK_AUDIO_DELAYS)
def test_link_audio_delay_accepts_every_documented_value(yxc, no_network,
                                                         good):
    """Guards against the guard rejecting values the receiver does support."""
    assert yxc_set_link_audio_delay(HOST, good, "main") is True
    assert f"main/setLinkAudioDelay?delay={good}" in paths(yxc["urls"])[-1]


def test_the_two_value_sets_do_not_overlap():
    """Guards against one setting's values silently validating for the other."""
    assert not set(LINK_CONTROLS) & set(LINK_AUDIO_DELAYS)


# ---------------------------------------------------------------------------
# MusicCast grouping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hosts, expected", [
    ([], ""),
    ([HOST], HOST),
    (["", None], ""),                  # nothing real in the list
    ([HOST, HOST], HOST),              # duplicates collapse to one unit
])
def test_musiccast_group_no_ops_below_two_units(yxc, no_network, hosts,
                                                expected):
    """Guards against a distribution session set up for a single receiver.

    Grouping one unit with itself puts it into a distribution session it then
    has to be taken back out of, and on the connect path that is several
    wasted round trips before any audio moves. One unit is already in sync
    with itself.
    """
    assert musiccast_group(list(hosts)) == expected
    assert yxc["urls"] == [], "one unit must cost no requests at all"


def test_musiccast_group_nominates_the_first_host_as_server(yxc, no_network):
    """Guards against streaming to each unit separately.

    Only the server's address should then be given something to play; pushing
    the same stream to every unit independently leaves them audibly out of
    step, exactly as it does with Sonos. Only main can serve on these units,
    so that is what the client and server calls have to name.
    """
    yxc["answers"]["dist/getDistributionInfo"] = {"response_code": 0,
                                                  "group_id": "abc123"}
    hosts = [HOST, "192.168.1.66", "192.168.1.67"]
    assert musiccast_group(hosts) == HOST

    calls = yxc["urls"]
    assert any("dist/setServerInfo" in u and HOST in u for u in calls)
    assert any("dist/startDistribution" in u for u in calls)
    for client in hosts[1:]:
        assert any("dist/setClientInfo" in u and client in u for u in calls)
    server_call = next(u for u in calls if "setServerInfo" in u)
    assert "group_id=abc123" in server_call
    assert "zone=main" in server_call
    assert "192.168.1.66,192.168.1.67" in server_call


def test_musiccast_group_invents_a_group_id_when_the_unit_has_none(yxc,
                                                                   no_network):
    """Guards against joining the all-zeroes "not grouped" id.

    A unit that is not in a group reports a group_id of all zeroes. Reusing it
    as the new session's id asks every unit to join the null group, which is
    how a group ends up looking created but silent.
    """
    yxc["answers"]["dist/getDistributionInfo"] = {
        "response_code": 0, "group_id": "0" * 32}
    musiccast_group([HOST, "192.168.1.66"])
    server_call = next(u for u in yxc["urls"] if "setServerInfo" in u)
    group_id = server_call.split("group_id=")[1].split("&")[0]
    assert group_id and set(group_id) != {"0"}


# ---------------------------------------------------------------------------
# Sonos
# ---------------------------------------------------------------------------

def test_sonos_didl_is_well_formed_and_a_music_track(no_network):
    """Guards against Sonos hiding its tone controls mid-cast.

    SoCo's play_uri shortcut tags whatever it builds as a TuneIn broadcast,
    which tells the speaker this is internet radio rather than audio from a
    device on the LAN -- and Sonos then withholds bass, treble and loudness
    for the whole session. The plain musicTrack class is what keeps them.
    """
    from xml.etree import ElementTree

    xml = sonos_didl("Caster", "http://192.168.1.2:8000/live.wav", "audio/wav")
    root = ElementTree.fromstring(xml)               # must parse at all
    assert root.tag.endswith("DIDL-Lite")
    text = "".join(root.itertext())
    assert "object.item.audioItem.musicTrack" in xml
    assert "tuneIn" not in xml and "SA_RINCON" not in xml
    assert "http-get:*:audio/wav:*" in xml
    assert "http://192.168.1.2:8000/live.wav" in text


@pytest.mark.parametrize("title", [
    "Rock & Roll",
    "<script>",
    "AT&T & <b>bold</b>",
    'a "quoted" title',
    "R&B Room",
])
def test_sonos_didl_escapes_the_title(no_network, title):
    """Guards against a device or track name breaking the DIDL document.

    The title is a device or stream name the user can set: "R&B Room" is the
    literal name of the receiver on this LAN. An unescaped ampersand makes the
    XML unparseable, and Sonos rejects the whole SetAVTransportURI -- so a cast
    fails purely because of what something was named.
    """
    from xml.etree import ElementTree

    xml = sonos_didl(title, "http://h/live.wav", "audio/wav")
    root = ElementTree.fromstring(xml)               # unescaped & fails here
    found = next(el for el in root.iter() if el.tag.endswith("title"))
    assert found.text == title                       # survives the round trip
    assert "&amp;" in xml or "&" not in title


def test_sonos_didl_escapes_an_ampersand_in_the_url(no_network):
    """Guards against a query-string URL breaking the same document."""
    from xml.etree import ElementTree

    url = "http://h:8000/live.wav?a=1&b=2"
    root = ElementTree.fromstring(sonos_didl("Caster", url, "audio/wav"))
    res = next(el for el in root.iter() if el.tag.endswith("res"))
    assert res.text == url


@pytest.mark.parametrize("name, extra", [
    ("Sonos One", ""),
    ("sonos one", ""),
    ("SONOS ONE", ""),
    ("Kitchen", "Sonos, Inc."),
    ("Kitchen", "RINCON_B8E937"),
    ("RINCON_B8E937B0F01400", ""),
    ("Kitchen", "rincon"),
])
def test_looks_like_sonos_matches_either_argument_case_insensitively(name,
                                                                     extra):
    """Guards against a Sonos appearing three times and two of them failing.

    Sonos answers SSDP as a ZonePlayer and advertises AirPlay 2 as well, so it
    turns up in the AirPlay and UPnP lists too. Neither of those is usable:
    the AirPlay path wants MFi hardware authentication no Python client can
    do, and the UPnP one wants Sonos' own transport handling. A missed match
    means the user picks a duplicate row and the cast fails.
    """
    assert looks_like_sonos(name, extra) is True


@pytest.mark.parametrize("name, extra", [
    ("Living Room TV", "Samsung"),
    ("R&B Room", "Yamaha Corporation"),
    ("Chromecast", ""),
    ("", ""),
    ("Sonic the Hedgehog", "Sega"),     # "sonos" is not a substring of this
])
def test_looks_like_sonos_does_not_match_other_devices(name, extra):
    """Guards against a real renderer being hidden as a phantom Sonos.

    A false positive removes a working device from the list entirely, and the
    user has no way to tell it apart from the device being switched off.
    """
    assert looks_like_sonos(name, extra) is False


# ---------------------------------------------------------------------------
# Roku
# ---------------------------------------------------------------------------

def test_roku_media_player_channel_id():
    """Guards against launching some other channel with a media URL.

    Roku has no general "play this URL" API. 2213 is the Roku Media Player
    channel, which is the only thing that takes a `u=` parameter; any other id
    launches an unrelated app and the cast simply does not happen.
    """
    assert ROKU_MEDIA_PLAYER == "2213"
    assert isinstance(ROKU_MEDIA_PLAYER, str)


def test_roku_video_formats_cover_the_two_containers_it_is_sent():
    """Guards against handing Roku a container it cannot play.

    Roku Media Player has no MPEG-TS support, so a live capture must reach it
    as MP4 or HLS -- those two are the whole reason a Roku target builds a
    different ScreenSource from a DLNA one.
    """
    assert ROKU_VIDEO_FORMATS["video/mp4"] == "mp4"
    assert ROKU_VIDEO_FORMATS["application/vnd.apple.mpegurl"] == "hls"
    assert "video/mpeg" not in ROKU_VIDEO_FORMATS


def test_roku_audio_formats_cover_the_audio_containers():
    """Guards against an audio cast being labelled with the wrong format.

    videoFormat/audioFormat is a hint Roku Media Player trusts; wav is what a
    system-audio capture actually sends, and mp3 is the fallback for anything
    else, so both have to be present and correct.
    """
    assert ROKU_AUDIO_FORMATS["audio/wav"] == "wav"
    assert ROKU_AUDIO_FORMATS["audio/mpeg"] == "mp3"


@pytest.mark.parametrize("mime, expect_type, expect_format", [
    ("video/mp4", "v", "mp4"),
    ("application/vnd.apple.mpegurl", "v", "hls"),
    ("audio/wav", "a", "wav"),
    ("audio/mpeg", "a", "mp3"),
    ("video/mpeg", "v", "mp4"),      # unsupported TS falls back, not crashes
    ("audio/weird", "a", "mp3"),
])
def test_roku_play_builds_a_media_player_launch_url(monkeypatch, no_network,
                                                    mime, expect_type,
                                                    expect_format):
    """Guards against a malformed ECP launch that Roku answers with a no-op.

    ECP takes the URL and the format as query parameters on a channel launch.
    A missing `t` or a format Roku does not recognise leaves the media player
    open on an error screen, which a blind user experiences as silence.
    """
    import urllib.parse

    sent = []
    monkeypatch.setattr(caster_devices, "_post",
                        lambda url, timeout=6: sent.append(url) or b"")
    caster_devices.roku_play("http://192.168.1.5:8060",
                             "http://192.168.1.2:8000/live.mp4", mime)

    assert len(sent) == 1
    parts = urllib.parse.urlsplit(sent[0])
    assert parts.path == f"/launch/{ROKU_MEDIA_PLAYER}"
    query = urllib.parse.parse_qs(parts.query)
    assert query["t"] == [expect_type]
    assert query["u"] == ["http://192.168.1.2:8000/live.mp4"]
    key = "audioFormat" if expect_type == "a" else "videoFormat"
    assert query[key] == [expect_format]


def test_roku_stop_goes_home_because_ecp_has_no_stop(monkeypatch, no_network):
    """Guards against a stop that leaves the stream playing.

    There is no stop verb in ECP. Home is what leaves the media player, and if
    it were ever replaced by a "stop" that Roku ignores, pressing Stop in the
    app would say "Stopped." while the Roku kept playing.
    """
    sent = []
    monkeypatch.setattr(caster_devices, "_post",
                        lambda url, timeout=6: sent.append(url) or b"")
    caster_devices.roku_stop("http://192.168.1.5:8060")
    assert sent == ["http://192.168.1.5:8060/keypress/Home"]
