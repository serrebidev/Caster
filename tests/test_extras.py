"""UPnP parsing, the grabber cache, ffmpeg command building and the audio tap.

No ffmpeg is ever spawned and no socket is ever opened here: `_find_ffmpeg`,
`urlopen`, `subprocess` and `caster.pick_h264_encoder` are all stubbed. What
is under test is the reasoning -- which inputs get built, in what order, and
what the module concludes from a device's own answers -- because that is the
part that fails silently on a real receiver and looks like "it just does not
play".
"""
from __future__ import annotations

import io
import queue
import socket
import struct

import pytest

import caster_extras
from caster_extras import (CONTAINERS, AudioTap, ScreenSource,
                           _upnp_fetch_control, grabber_machine_key,
                           mdns_host, pick_screen_grabber, ssdp_sockets,
                           upnp_discover, upnp_sink_mimes, wav_header)

DESC_URL = "http://192.168.1.65:49154/desc/device.xml"


class _Response(io.BytesIO):
    """Just enough of an HTTPResponse for `with urlopen(...) as r:`."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def soap_protocol_info(sink: str) -> bytes:
    """A GetProtocolInfo response shaped the way a renderer really answers."""
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body>"
        '<u:GetProtocolInfoResponse xmlns:u="urn:schemas-upnp-org:service:'
        'ConnectionManager:1">'
        "<Source></Source>"
        f"<Sink>{sink}</Sink>"
        "</u:GetProtocolInfoResponse>"
        "</s:Body></s:Envelope>"
    ).encode()


# ---------------------------------------------------------------------------
# SSDP interface selection
# ---------------------------------------------------------------------------

def test_ssdp_opens_a_socket_on_every_non_loopback_ipv4_interface(
        monkeypatch, no_network):
    """A VPN must not make discovery send only through its virtual adapter."""
    class FakeSocket:
        def __init__(self):
            self.bound = None
            self.multicast_interface = None
            self.timeout = None
            self.closed = False

        def settimeout(self, value):
            self.timeout = value

        def bind(self, address):
            self.bound = address

        def setsockopt(self, _level, _option, value):
            self.multicast_interface = socket.inet_ntoa(value)

        def close(self):
            self.closed = True

    made = []
    monkeypatch.setattr(
        caster_extras.socket, "getaddrinfo",
        lambda *_: [
            (socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("192.0.2.10", 0)),
            (socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("127.0.0.1", 0)),
            (socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("198.51.100.7", 0)),
            (socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("192.0.2.10", 0)),
        ])
    monkeypatch.setattr(caster_extras.socket, "socket",
                        lambda *_: made.append(FakeSocket()) or made[-1])

    sockets = ssdp_sockets()

    assert sockets == made
    assert [sock.bound for sock in sockets] == [
        ("192.0.2.10", 0), ("198.51.100.7", 0)]
    assert [sock.multicast_interface for sock in sockets] == [
        "192.0.2.10", "198.51.100.7"]
    assert all(sock.timeout == 0.5 for sock in sockets)
    for sock in sockets:
        sock.close()
    assert all(sock.closed for sock in sockets)


def test_mdns_host_prefers_ipv4_and_keeps_ipv6_only_services():
    """A first IPv6 record must not make a Cast or Kodi service disappear."""
    dual_stack = type("Info", (), {
        "parsed_addresses": lambda self: ["2001:db8::20", "192.0.2.20"],
    })()
    ipv6_only = type("Info", (), {
        "parsed_addresses": lambda self: ["2001:db8::21"],
    })()

    assert mdns_host(dual_stack) == "192.0.2.20"
    assert mdns_host(ipv6_only) == "2001:db8::21"


def test_upnp_sends_each_search_on_every_ssdp_interface(monkeypatch):
    """Receiving on one adapter cannot make us skip another LAN interface."""
    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.closed = False

        def sendto(self, message, destination):
            self.sent.append((message, destination))

        def close(self):
            self.closed = True

    sockets = [FakeSocket(), FakeSocket()]
    monotonic = iter([0.0, 0.0, 0.0, 1.0])
    monkeypatch.setattr(caster_extras, "ssdp_sockets", lambda: sockets)
    monkeypatch.setattr(caster_extras.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(caster_extras.select, "select",
                        lambda *_: ([], [], []))

    assert upnp_discover(timeout=1) == []
    assert all(len(sock.sent) == 1 for sock in sockets)
    assert all(sock.sent[0][1] == ("239.255.255.250", 1900)
               for sock in sockets)
    assert all(sock.closed for sock in sockets)


# ---------------------------------------------------------------------------
# upnp_sink_mimes
# ---------------------------------------------------------------------------

def test_sink_mimes_are_lowercased_and_stripped_of_parameters(monkeypatch,
                                                              no_network):
    """Guards against a renderer's own format list failing to match anything.

    A protocolInfo entry is `protocol:network:contentFormat:additionalInfo`,
    and the content format carries MIME parameters: the RX-V4A answers
    `audio/L16;rate=48000;channels=2`. A caller asking "does this device
    accept audio/l16" compares against a bare, lower-case type, so the
    parameters and the capitals have to come off here or every comparison
    misses and a device that does accept the format is sent nothing.
    """
    sink = ",".join([
        "http-get:*:audio/L16;rate=48000;channels=2:*",
        "http-get:*:AUDIO/MPEG:*",
        "http-get:*:video/mp4:DLNA.ORG_PN=AVC_MP4_BL_CIF15_AAC_520",
        " http-get:*:audio/wav:* ",                 # padded, as some answer
        "internal:*:*:*",                           # no MIME at all: skipped
        "rtsp-rtp-udp:*:*:*",                       # ditto
    ])
    monkeypatch.setattr(caster_extras._urlreq, "urlopen",
                        lambda req, timeout=None: _Response(
                            soap_protocol_info(sink)))

    mimes = upnp_sink_mimes("http://192.168.1.65:49154/cm/ctrl")
    assert isinstance(mimes, frozenset)
    assert mimes == {"audio/l16", "audio/mpeg", "video/mp4", "audio/wav"}
    assert not any(";" in m for m in mimes)
    assert not any(m != m.lower() for m in mimes)


def test_sink_mimes_finds_the_video_types_a_tv_publishes(monkeypatch,
                                                         no_network):
    """Guards against a TV being treated as audio-only.

    supports_video believes this list when it is non-empty. A parse that lost
    the video entries would make the app build a WAV for a television.
    """
    monkeypatch.setattr(caster_extras._urlreq, "urlopen",
                        lambda req, timeout=None: _Response(
                            soap_protocol_info(
                                "http-get:*:video/mpeg:*,"
                                "http-get:*:video/mp4:*,"
                                "http-get:*:audio/mpeg:*")))
    mimes = upnp_sink_mimes("http://tv/cm")
    assert {"video/mpeg", "video/mp4"} <= mimes


def test_empty_control_url_returns_empty_without_asking(monkeypatch,
                                                        no_network):
    """Guards against a SOAP call to nowhere on the discovery path.

    Plenty of renderers publish no ConnectionManager at all, so the control
    URL is often "". An empty result here means "the device did not say",
    NOT "the device accepts nothing" -- callers must fall back to guessing
    from the device kind, because refusing to send such a renderer anything
    would be worse than guessing wrong.
    """
    def boom(*a, **k):
        raise AssertionError("must not issue a request for an empty URL")

    monkeypatch.setattr(caster_extras._urlreq, "urlopen", boom)
    assert upnp_sink_mimes("") == frozenset()


@pytest.mark.parametrize("failure", [
    OSError("connection refused"),
    TimeoutError("no answer"),
    ValueError("garbage"),
])
def test_network_error_returns_empty_meaning_did_not_say(monkeypatch,
                                                         no_network, failure):
    """Guards against one silent renderer aborting the whole scan.

    Discovery fetches these in parallel for every responder. A renderer that
    answers SSDP and then refuses its own ConnectionManager must contribute an
    empty set -- read by callers as "unknown", not as "accepts nothing" -- so
    it still appears in the device list and still gets a sensible guess.
    """
    def raiser(*a, **k):
        raise failure

    monkeypatch.setattr(caster_extras._urlreq, "urlopen", raiser)
    assert upnp_sink_mimes("http://192.168.1.65:49154/cm/ctrl") == frozenset()


def test_a_response_with_no_sink_element_returns_empty(monkeypatch,
                                                       no_network):
    """Guards against a KeyError-shaped crash on a minimal SOAP answer."""
    monkeypatch.setattr(
        caster_extras._urlreq, "urlopen",
        lambda req, timeout=None: _Response(b"<s:Envelope></s:Envelope>"))
    assert upnp_sink_mimes("http://h/cm") == frozenset()


# ---------------------------------------------------------------------------
# _upnp_fetch_control
# ---------------------------------------------------------------------------

def description(services: str, name: str = "R&amp;B Room",
                maker: str = "Yamaha Corporation") -> bytes:
    return (
        '<?xml version="1.0"?>'
        '<root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
        f"<friendlyName>{name}</friendlyName>"
        f"<manufacturer>{maker}</manufacturer>"
        f"<serviceList>{services}</serviceList>"
        "</device></root>"
    ).encode()


AV_SERVICE_ROOT_ABSOLUTE = (
    "<service>"
    "<serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>"
    "<serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>"
    "<controlURL>/AVTransport/ctrl</controlURL>"
    "</service>")
CM_SERVICE_RELATIVE = (
    "<service>"
    "<serviceType>urn:schemas-upnp-org:service:ConnectionManager:1"
    "</serviceType>"
    "<controlURL>cm/ctrl</controlURL>"
    "</service>")


@pytest.fixture
def fetch(monkeypatch):
    """Serve one description document and stub the ConnectionManager call."""
    state = {"body": b"", "mimes": frozenset({"audio/wav"}), "cm_urls": []}

    monkeypatch.setattr(caster_extras._urlreq, "urlopen",
                        lambda url, timeout=None: _Response(state["body"]))

    def fake_mimes(cm_url):
        state["cm_urls"].append(cm_url)
        return state["mimes"]

    monkeypatch.setattr(caster_extras, "upnp_sink_mimes", fake_mimes)
    return state


def test_fetch_control_parses_name_and_manufacturer(fetch, no_network):
    """Guards against a renderer that has its own better protocol being missed.

    The manufacturer is how a caller recognises a Sonos answering plain DLNA,
    and the friendly name is the only thing the user hears in the device list.
    Both arrive XML-escaped -- the receiver on this LAN is literally called
    "R&B Room" -- so an unescaped name would be read aloud as "R and amp semi
    colon B Room".
    """
    fetch["body"] = description(AV_SERVICE_ROOT_ABSOLUTE + CM_SERVICE_RELATIVE)
    name, av_url, maker, mimes = _upnp_fetch_control(DESC_URL)
    assert name == "R&B Room"
    assert maker == "Yamaha Corporation"
    assert mimes == frozenset({"audio/wav"})


@pytest.mark.parametrize("control, expected", [
    # root-absolute: everything before the path is kept, the path replaced
    ("/AVTransport/ctrl", "http://192.168.1.65:49154/AVTransport/ctrl"),
    # relative: resolved against the description's own directory
    ("ctrl", "http://192.168.1.65:49154/desc/ctrl"),
    ("sub/ctrl", "http://192.168.1.65:49154/desc/sub/ctrl"),
    # already absolute: left alone
    ("http://192.168.1.65:8080/ctrl", "http://192.168.1.65:8080/ctrl"),
])
def test_control_urls_resolve_against_the_description_url(fetch, no_network,
                                                          control, expected):
    """Guards against SOAP being posted to a URL that does not exist.

    UPnP allows a controlURL to be root-absolute, relative or absolute, and
    real devices use all three. Concatenating instead of resolving turns
    "/AVTransport/ctrl" into ".../desc//AVTransport/ctrl" and every transport
    command 404s -- the device appears in the list and then does nothing.
    """
    service = (
        "<service>"
        "<serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>"
        f"<controlURL>{control}</controlURL>"
        "</service>")
    fetch["body"] = description(service)
    _, av_url, _, _ = _upnp_fetch_control(DESC_URL)
    assert av_url == expected


def test_connection_manager_url_is_resolved_too(fetch, no_network):
    """Guards against the sink-format query going to the wrong path.

    A misresolved ConnectionManager URL fails quietly and returns an empty
    set, which reads as "the device did not say" -- so the app falls back to
    guessing and nobody ever sees the mistake. Asserting the resolved URL is
    the only way to catch it.
    """
    fetch["body"] = description(AV_SERVICE_ROOT_ABSOLUTE + CM_SERVICE_RELATIVE)
    _upnp_fetch_control(DESC_URL)
    assert fetch["cm_urls"] == ["http://192.168.1.65:49154/desc/cm/ctrl"]


def test_no_avtransport_service_is_not_a_renderer(fetch, no_network):
    """Guards against a printer or a router in the device list.

    Plenty of things answer the MediaRenderer M-SEARCH loosely, and anything
    without AVTransport cannot be told to play. Listing it gives the user a
    row that can only ever fail.
    """
    fetch["body"] = description(CM_SERVICE_RELATIVE)
    assert _upnp_fetch_control(DESC_URL) is None


def test_a_service_block_with_no_control_url_is_skipped(fetch, no_network):
    """Guards against a malformed service block breaking the whole document.

    One service without a controlURL must not stop the AVTransport that comes
    after it from being found; a device with several services is the norm.
    """
    broken = ("<service><serviceType>urn:schemas-upnp-org:service:"
              "RenderingControl:1</serviceType></service>")
    fetch["body"] = description(broken + AV_SERVICE_ROOT_ABSOLUTE)
    result = _upnp_fetch_control(DESC_URL)
    assert result is not None
    assert result[1] == "http://192.168.1.65:49154/AVTransport/ctrl"


def test_unreachable_description_returns_none(monkeypatch, no_network):
    """Guards against one stalled renderer aborting the parallel fetch."""
    def raiser(*a, **k):
        raise OSError("connection reset")

    monkeypatch.setattr(caster_extras._urlreq, "urlopen", raiser)
    assert _upnp_fetch_control(DESC_URL) is None


def test_missing_friendly_name_falls_back_to_the_location(fetch, no_network):
    """Guards against a nameless device becoming an empty row in the list.

    An unlabelled entry in a CheckListBox is unusable with a screen reader:
    NVDA reads nothing at all and the user cannot tell which device it is.
    """
    fetch["body"] = (
        '<?xml version="1.0"?><root><device><serviceList>'
        + AV_SERVICE_ROOT_ABSOLUTE +
        "</serviceList></device></root>").encode()
    name, _, maker, _ = _upnp_fetch_control(DESC_URL)
    assert name == DESC_URL
    assert maker == ""


# ---------------------------------------------------------------------------
# pick_screen_grabber
# ---------------------------------------------------------------------------

@pytest.fixture
def grabber(monkeypatch):
    """Reset the module-level grabber cache and its persistence hooks.

    _grabber_cache is a process global that survives between tests, so
    whichever test ran first would otherwise decide the answer for all of
    them -- and the "never probes" assertion would pass for the wrong reason.
    """
    monkeypatch.setattr(caster_extras, "_grabber_cache", "")
    monkeypatch.setattr(caster_extras, "grabber_cache_load", None)
    monkeypatch.setattr(caster_extras, "grabber_cache_store", None)
    yield
    monkeypatch.setattr(caster_extras, "_grabber_cache", "")


def never_probe(timeout):
    raise AssertionError("probed when the answer was already known")


@pytest.mark.parametrize("remembered", ["gdigrab", "ddagrab"])
def test_a_remembered_answer_is_returned_without_probing(monkeypatch, grabber,
                                                         remembered):
    """Guards against the ten-second hang coming back to every launch.

    ddagrab hangs rather than failing on this box, so the probe used to cost a
    hard timeout EVERY launch, in series on the connect path. The answer
    cannot change while the machine and session stay the same, so a remembered
    answer must short-circuit the probe entirely -- not merely make it faster.
    """
    monkeypatch.setattr(caster_extras, "grabber_cache_load",
                        lambda: remembered)
    monkeypatch.setattr(caster_extras, "_probe_screen_grabber", never_probe)
    assert pick_screen_grabber() == remembered


@pytest.mark.parametrize("remembered", ["", "weasel", None, "DDAGRAB"])
def test_a_meaningless_remembered_answer_is_probed_again(monkeypatch, grabber,
                                                         remembered):
    """Guards against a corrupt settings value being used as a grabber name.

    screen_grabber comes out of a JSON file. Anything that is not one of the
    two real grabbers must be re-probed, because passing it to ffmpeg as `-f`
    fails the whole capture rather than falling back.
    """
    monkeypatch.setattr(caster_extras, "grabber_cache_load",
                        lambda: remembered)
    monkeypatch.setattr(caster_extras, "_probe_screen_grabber",
                        lambda timeout: "gdigrab")
    assert pick_screen_grabber() == "gdigrab"


def test_a_load_hook_that_raises_falls_back_to_probing(monkeypatch, grabber):
    """Guards against an unreadable profile making the app unable to capture.

    The hooks are installed by caster.py and read the settings file. If that
    read throws, the right answer is a slow launch, not a dead capture path.
    """
    def raiser():
        raise OSError("profile is unreadable")

    monkeypatch.setattr(caster_extras, "grabber_cache_load", raiser)
    monkeypatch.setattr(caster_extras, "_probe_screen_grabber",
                        lambda timeout: "ddagrab")
    assert pick_screen_grabber() == "ddagrab"


def test_the_probe_result_is_handed_to_the_store_hook(monkeypatch, grabber):
    """Guards against a probe that is paid for and then thrown away.

    Without the store call the answer lives only in the process global, so the
    hang is paid again on the next launch -- which is exactly the bug the
    settings cache was added to fix.
    """
    stored = []
    monkeypatch.setattr(caster_extras, "grabber_cache_load", lambda: "")
    monkeypatch.setattr(caster_extras, "grabber_cache_store", stored.append)
    monkeypatch.setattr(caster_extras, "_probe_screen_grabber",
                        lambda timeout: "ddagrab")

    assert pick_screen_grabber() == "ddagrab"
    assert stored == ["ddagrab"]


def test_an_unwritable_store_still_returns_the_answer(monkeypatch, grabber):
    """Guards against a read-only profile breaking screen capture outright.

    Failing to remember the answer costs one slow launch. Raising out of
    pick_screen_grabber would cost the cast.
    """
    def raiser(value):
        raise OSError("read-only profile")

    monkeypatch.setattr(caster_extras, "grabber_cache_load", lambda: "")
    monkeypatch.setattr(caster_extras, "grabber_cache_store", raiser)
    monkeypatch.setattr(caster_extras, "_probe_screen_grabber",
                        lambda timeout: "gdigrab")
    assert pick_screen_grabber() == "gdigrab"


def test_the_probe_runs_at_most_once_per_process(monkeypatch, grabber):
    """Guards against re-probing on every connection.

    ffmpeg is spawned per connection and _video_input() asks this each time.
    With no hooks installed at all (the standalone case), the process global
    is the only thing stopping a probe per cast.
    """
    calls = []

    def probe_once(timeout):
        calls.append(timeout)
        return "gdigrab"

    monkeypatch.setattr(caster_extras, "_probe_screen_grabber", probe_once)
    assert pick_screen_grabber() == "gdigrab"
    monkeypatch.setattr(caster_extras, "_probe_screen_grabber", never_probe)
    assert pick_screen_grabber() == "gdigrab"
    assert len(calls) == 1


def test_grabber_machine_key_is_stable_and_describes_the_session(monkeypatch):
    """Guards against a cache key that invalidates itself every launch.

    The key is what says the remembered answer still applies. If it were not
    stable -- a timestamp, a pid, a random id -- the cached answer would never
    match and the probe would run every time, silently undoing the fix. It
    must, however, still change for an RDP session, where ddagrab has neither
    a console nor a GPU behind it and the answer really is different.
    """
    assert grabber_machine_key() == grabber_machine_key()

    monkeypatch.setenv("COMPUTERNAME", "TESTBOX")
    monkeypatch.setenv("SESSIONNAME", "Console")
    console = grabber_machine_key()
    assert console == "TESTBOX|Console"

    monkeypatch.setenv("SESSIONNAME", "RDP-Tcp#1")
    assert grabber_machine_key() != console

    monkeypatch.delenv("COMPUTERNAME", raising=False)
    monkeypatch.delenv("SESSIONNAME", raising=False)
    assert grabber_machine_key() == "?|?"       # no crash without the vars


# ---------------------------------------------------------------------------
# CONTAINERS
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("container, expected", [
    ("mp4", ("live.mp4", "video/mp4", False)),
    ("mpegts", ("live.ts", "video/mpeg", False)),
    ("wav", ("live.wav", "audio/wav", True)),
])
def test_containers_map_to_path_mime_and_audio_only(container, expected):
    """Guards against a receiver being handed the wrong content type.

    The MIME goes into the DIDL protocolInfo and the HTTP Content-Type, and
    the suffix is what several receivers actually sniff. The audio_only flag
    decides whether a video encoder is built at all, so getting it wrong for
    wav means running an H.264 encoder to send a speaker nothing it can use.
    """
    assert CONTAINERS[container] == expected


def test_only_the_three_known_containers_exist():
    """Guards against a fourth container reaching code that handles three.

    _output_args() has a branch for mp4 and one for mpegts and treats
    everything else as wav, so an added container would silently be muxed as
    WAV rather than failing.
    """
    assert set(CONTAINERS) == {"mp4", "mpegts", "wav"}


def test_an_unknown_container_is_rejected_at_construction():
    """Guards against the failure surfacing later as a wrong-format stream."""
    with pytest.raises(ValueError):
        ScreenSource(container="mkv")


# ---------------------------------------------------------------------------
# ScreenSource.ffmpeg_cmd
# ---------------------------------------------------------------------------

@pytest.fixture
def ffmpeg(monkeypatch):
    """Stub everything ffmpeg_cmd() reaches for outside its own logic."""
    import caster

    monkeypatch.setattr(caster_extras, "_find_ffmpeg", lambda: "ffmpeg.exe")
    monkeypatch.setattr(caster, "pick_h264_encoder", lambda: "libx264")
    # _video_input() and _video_filter() both consult the grabber; without
    # this they would probe, which means spawning ffmpeg.
    monkeypatch.setattr(caster_extras, "_grabber_cache", "gdigrab")
    monkeypatch.setattr(caster_extras, "_probe_screen_grabber", never_probe)
    yield
    monkeypatch.setattr(caster_extras, "_grabber_cache", "")


def index_of(cmd, needle):
    return next(i for i, part in enumerate(cmd) if needle in str(part))


def test_wav_builds_no_video_input_and_no_encoder(ffmpeg):
    """Guards against running an H.264 encoder to feed a pair of speakers.

    wav is the audio-only route: a MusicCast amplifier or a Sonos accepts no
    video at all, and the whole reason that path is near-realtime is that
    there is no screen grabber, no scaler and no encoder in it. A stray video
    input here would burn a core and add hundreds of milliseconds for output
    the receiver must discard.
    """
    cmd = ScreenSource(container="wav").ffmpeg_cmd()
    assert cmd.count("-i") == 1                    # the PCM pipe, nothing else
    assert cmd[cmd.index("-i") + 1] == "pipe:0"
    assert "-c:v" not in cmd
    assert "libx264" not in cmd
    assert "gdigrab" not in cmd and "desktop" not in cmd
    assert not any("ddagrab" in str(p) for p in cmd)
    assert "-vf" not in cmd
    # ... and it is still a WAV stream on the wire.
    assert cmd[cmd.index("-f", cmd.index("pipe:0")):][:2] == ["-f", "wav"]
    assert "pcm_s16le" in cmd
    assert cmd[-1] == "pipe:1"


def test_wav_maps_the_system_audio_as_input_zero(ffmpeg):
    """Guards against an off-by-one in the -map indices.

    Input order decides every -map: with no video, the PCM pipe is input 0,
    and mapping 1:a there would fail to open a stream that does not exist.
    """
    cmd = ScreenSource(container="wav").ffmpeg_cmd()
    assert cmd[index_of(cmd, "-map") + 1] == "0:a"


def test_mp4_output_is_a_fragmented_live_mp4(ffmpeg):
    """Guards against a Chromecast waiting forever for a moov atom.

    A normal MP4 writes its index at the END of the file, which never comes
    for a live capture -- so the receiver buffers and shows nothing. The
    empty_moov/frag_keyframe combination puts an empty index up front and
    emits short self-contained fragments as they are encoded.
    """
    cmd = ScreenSource(container="mp4").ffmpeg_cmd()
    flags = cmd[cmd.index("-movflags") + 1]
    for flag in ("frag_keyframe", "empty_moov", "default_base_moof",
                 "omit_tfhd_offset"):
        assert flag in flags
    assert int(cmd[cmd.index("-frag_duration") + 1]) > 0
    assert cmd[cmd.index("-f", cmd.index("pipe:0")) + 1] == "mp4"
    # A video path really was built.
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "libx264"
    assert "gdigrab" in cmd
    assert cmd.count("-i") == 2


def test_mpegts_resends_headers_for_a_mid_stream_join(ffmpeg):
    """Guards against a DLNA renderer joining a TS and seeing nothing.

    A receiver that connects mid-stream needs the PAT/PMT to find the
    programme. Without +resend_headers it waits for the next natural cycle,
    which on a live capture reads as a cast that did not start.
    """
    cmd = ScreenSource(container="mpegts").ffmpeg_cmd()
    assert cmd[cmd.index("-mpegts_flags") + 1] == "+resend_headers"
    assert "mpegts" in cmd


def test_a_mic_device_is_mixed_into_the_system_audio(ffmpeg):
    """Guards against narration landing in a second audio track nobody hears.

    Receivers play the first audio stream and ignore the rest, so a
    microphone has to be mixed INTO the system audio rather than added beside
    it. dropout_transition=0 is part of the same fix: without it amix ducks
    the system audio every time the speaker stops talking.
    """
    cmd = ScreenSource(container="mp4",
                       mic_device="Microphone (USB Audio)").ffmpeg_cmd()
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=2" in graph
    assert "dropout_transition=0" in graph
    assert "[1:a][2:a]" in graph          # video, system audio, then the mic
    assert cmd[cmd.index("-map", cmd.index("-filter_complex")) + 1] == "[aout]"
    assert "dshow" in cmd
    assert "audio=Microphone (USB Audio)" in cmd
    assert cmd.count("-i") == 3


def test_no_mic_means_no_filter_complex_but_still_a_resampler(ffmpeg):
    """Guards against capture jitter accumulating as audio/video drift.

    Without a mic there is no filter graph to resample inside, so the -af
    aresample has to be there instead. Losing it lets the loopback clock and
    the output clock drift apart over a long cast, which is heard as lip sync
    slowly going wrong.
    """
    cmd = ScreenSource(container="mp4").ffmpeg_cmd()
    assert "-filter_complex" not in cmd
    assert "aresample=async=1:first_pts=0" in cmd[cmd.index("-af") + 1]


@pytest.mark.parametrize("offset_ms, expected", [(-300, "0.300"),
                                                 (-1000, "1.000")])
def test_a_negative_offset_delays_the_video_input(ffmpeg, offset_ms, expected):
    """Guards against an A/V correction applied to the wrong input.

    -itsoffset shifts whichever input FOLLOWS it. A negative offset means the
    picture must be held back, so the flag has to land before the video input;
    putting it on the audio side would double the error the user was trying to
    correct rather than removing it.
    """
    cmd = ScreenSource(container="mp4", av_offset_ms=offset_ms).ffmpeg_cmd()
    assert cmd[cmd.index("-itsoffset") + 1] == expected
    assert cmd.index("-itsoffset") < index_of(cmd, "gdigrab")


@pytest.mark.parametrize("offset_ms, expected", [(300, "0.300"),
                                                 (1000, "1.000")])
def test_a_positive_offset_delays_the_audio_input(ffmpeg, offset_ms, expected):
    """Guards against the same mistake in the other direction.

    A positive offset delays the sound behind the picture, so the flag belongs
    between the video input and the PCM pipe -- after gdigrab and before
    pipe:0, which is exactly what fixes a receiver that runs audio early.
    """
    cmd = ScreenSource(container="mp4", av_offset_ms=offset_ms).ffmpeg_cmd()
    assert cmd[cmd.index("-itsoffset") + 1] == expected
    assert index_of(cmd, "gdigrab") < cmd.index("-itsoffset")
    assert cmd.index("-itsoffset") < cmd.index("pipe:0")


def test_zero_offset_adds_no_itsoffset_at_all(ffmpeg):
    """Guards against a no-op flag changing timing anyway.

    -itsoffset 0.000 is not free: it changes how ffmpeg handles the input's
    start time. The default has to be no flag, not a zero one.
    """
    assert "-itsoffset" not in ScreenSource(container="mp4").ffmpeg_cmd()


def test_a_window_capture_uses_the_handle_not_the_title(ffmpeg):
    """Guards against capturing the wrong window, or none.

    Titles change while you watch them (a browser tab, a terminal spinner) and
    two windows can share one; gdigrab resolves a title only at open time. The
    handle is the honest identifier, so it must be what is tried first.
    """
    src = ScreenSource(hwnd=12345, container="mp4")
    cmd = src.ffmpeg_cmd()
    assert "hwnd=12345" in cmd
    assert "desktop" not in cmd
    assert src._window_specs()[0] == "hwnd=12345"


def test_keyframes_are_pinned_to_the_clock_not_the_frame_count(ffmpeg):
    """Guards against a long wait before the first picture.

    Screen grabbers routinely deliver under the requested frame rate, so a GOP
    expressed as a frame count stretches to whatever half a second of ACHIEVED
    frames turns out to be. -force_key_frames on an expression of t pins it to
    real time, which is the floor on how long a receiver waits to show
    anything.
    """
    cmd = ScreenSource(container="mp4", fps=30,
                       keyframe_seconds=0.5).ffmpeg_cmd()
    assert cmd[cmd.index("-g") + 1] == "15"
    assert cmd[cmd.index("-keyint_min") + 1] == "15"
    assert cmd[cmd.index("-force_key_frames") + 1] == "expr:gte(t,n_forced*0.5)"
    assert cmd[cmd.index("-bf") + 1] == "0"        # reordering is delay


def test_a_low_frame_rate_never_asks_for_a_zero_length_gop(ffmpeg):
    """Guards against -g 0, which ffmpeg rejects outright.

    fps // 2 is 0 for any rate below two, and a preset or a settings file can
    carry one. The floor of 1 is what keeps the command valid.
    """
    cmd = ScreenSource(container="mp4", fps=1).ffmpeg_cmd()
    assert cmd[cmd.index("-g") + 1] == "1"


def test_the_video_filter_caps_the_size_and_forces_even_dimensions(ffmpeg):
    """Guards against an encoder failing on an odd-sized window.

    H.264 4:2:0 needs both dimensions even and a window can be any odd size at
    all, so the scaler has to truncate. The cap is separate: a 4K desktop at
    60fps buries any encoder here and no receiver benefits from it.
    """
    src = ScreenSource(container="mp4", max_width=1280, max_height=720)
    vf = src.ffmpeg_cmd()[src.ffmpeg_cmd().index("-vf") + 1]
    assert "min(iw,1280)" in vf and "min(ih,720)" in vf
    assert "force_original_aspect_ratio=decrease" in vf
    assert "trunc(iw/2)*2:trunc(ih/2)*2" in vf
    assert "hwdownload" not in vf        # gdigrab frames are already in RAM


def test_ddagrab_frames_are_downloaded_before_the_encoder(monkeypatch,
                                                          ffmpeg):
    """Guards against handing GPU surfaces to a software encoder.

    ddagrab delivers frames still on the GPU; libx264 and friends read system
    memory. Without the hwdownload prefix the whole capture fails at the first
    frame with a format error rather than falling back.
    """
    monkeypatch.setattr(caster_extras, "_grabber_cache", "ddagrab")
    cmd = ScreenSource(container="mp4").ffmpeg_cmd()
    assert any("ddagrab=output_idx=0" in str(p) for p in cmd)
    assert cmd[cmd.index("-vf") + 1].startswith("hwdownload,format=bgra,")


def test_low_delay_flags_are_on_every_command(ffmpeg):
    """Guards against ffmpeg's default buffering being reintroduced.

    +nobuffer and +low_delay are what stop ffmpeg holding input before it
    starts producing output. They belong on the audio-only path too, where
    they are a larger share of the total delay.
    """
    for container in CONTAINERS:
        cmd = ScreenSource(container=container).ffmpeg_cmd()
        assert cmd[cmd.index("-fflags") + 1] == "+nobuffer"
        assert cmd[cmd.index("-flags") + 1] == "+low_delay"
        assert cmd[0] == "ffmpeg.exe"


# ---------------------------------------------------------------------------
# PCM passthrough
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("channels, rate, mic, expected", [
    (2, 48000, "", True),
    (2, 44100, "", True),
    (1, 48000, "", True),
    (1, 44100, "", True),
    (6, 48000, "", False),        # surround: receivers reject it
    (8, 48000, "", False),
    (2, 96000, "", False),        # exotic rate: same
    (2, 32000, "", False),
    (2, 192000, "", False),
    (2, 48000, "Microphone", False),   # a mic needs a mixer, so ffmpeg
    (6, 44100, "Microphone", False),
])
def test_pcm_is_directly_usable_only_for_plain_stereo(channels, rate, mic,
                                                      expected):
    """Guards against the no-ffmpeg fast path being taken when it cannot work.

    When this is true the loopback PCM goes on the wire untouched: no process,
    no copy, no encoder, and the lowest delay this app can reach. Saying true
    for 5.1 or 96 kHz sends a receiver something it rejects, and the user
    hears silence with no error anywhere.
    """
    src = ScreenSource(container="wav", mic_device=mic)
    src.tap.channels = channels
    src.tap.rate = rate
    assert src.pcm_is_directly_usable() is expected


@pytest.mark.parametrize("channels, rate, mic, wire_r, wire_c", [
    (2, 48000, "", 48000, 2),      # passthrough: the tap's own rate is kept
    (2, 44100, "", 44100, 2),
    (1, 44100, "", 44100, 1),
    (6, 48000, "", 48000, 2),      # normalised: folded down to stereo
    (6, 96000, "", 48000, 2),      # and resampled to something universal
    (2, 96000, "", 48000, 2),
    (2, 48000, "Mic", 48000, 2),
])
def test_wire_format_follows_the_passthrough_decision(channels, rate, mic,
                                                      wire_r, wire_c):
    """Guards against the WAV header disagreeing with the samples behind it.

    wire_rate/wire_channels describe what actually goes out. If they said
    44100 while ffmpeg resampled to 48000, the receiver would play the stream
    at the wrong speed -- audible as a pitch shift, not as a failure.
    """
    src = ScreenSource(container="wav", mic_device=mic)
    src.tap.channels = channels
    src.tap.rate = rate
    assert src.wire_rate() == wire_r
    assert src.wire_channels() == wire_c


def test_wav_output_args_declare_the_wire_format(ffmpeg):
    """Guards against ffmpeg being told a different format from the header."""
    src = ScreenSource(container="wav")
    src.tap.channels = 6
    src.tap.rate = 96000
    cmd = src.ffmpeg_cmd()
    assert cmd[cmd.index("-ac", cmd.index("-i")) + 1] == "2"
    assert cmd[cmd.index("-ar", cmd.index("-i")) + 1] == "48000"


# ---------------------------------------------------------------------------
# AudioTap
# ---------------------------------------------------------------------------

def test_publish_drops_the_oldest_chunk_when_a_subscriber_is_full():
    """Guards against a slow consumer turning into permanent lag.

    A backlog is not a buffer: every chunk queued behind a slow receiver is
    delay the listener hears for the rest of the session, and it never
    recovers because the tap keeps producing at real time. Dropping the OLDEST
    chunk keeps the subscriber at the live edge; dropping the newest (or
    blocking) would stall the capture thread and every other subscriber with
    it.
    """
    tap = AudioTap()
    tap.QUEUE_CHUNKS = 2                 # a tiny queue, so it fills at once
    q = tap.subscribe()

    tap._publish(b"oldest")
    tap._publish(b"middle")
    tap._publish(b"newest")

    drained = [q.get_nowait() for _ in range(q.qsize())]
    assert drained == [b"middle", b"newest"], "the newest audio must survive"
    assert q.empty()


def test_publish_never_blocks_and_never_grows_past_the_bound():
    """Guards against the capture thread being held up by one subscriber.

    _publish runs on the single WASAPI read loop that feeds everyone. If it
    blocked on a full queue, one stalled receiver would stop the audio
    reaching every other device in a multi-room cast.
    """
    tap = AudioTap()
    tap.QUEUE_CHUNKS = 3
    q = tap.subscribe()
    for n in range(50):
        tap._publish(bytes([n]))         # must return immediately, 50 times
    assert q.qsize() == 3
    assert [q.get_nowait() for _ in range(3)] == [bytes([47]), bytes([48]),
                                                  bytes([49])]


def test_publish_feeds_every_subscriber():
    """Guards against a multi-room cast where only one device gets audio.

    One tap serves every container in a mixed selection -- a Chromecast and a
    DLNA renderer share the capture. Publishing to only the first subscriber
    would leave the second silent for the whole session.
    """
    tap = AudioTap()
    first, second = tap.subscribe(), tap.subscribe()
    tap._publish(b"chunk")
    assert first.get_nowait() == b"chunk"
    assert second.get_nowait() == b"chunk"


def test_unsubscribe_stops_delivery_and_wakes_a_blocked_reader():
    """Guards against a pump thread parked forever in queue.get().

    The sentinel None is how a reader learns the tap is done with it. Without
    it, a subscriber blocked in get() would never notice it had been removed
    and its thread would outlive the cast.
    """
    tap = AudioTap()
    q = tap.subscribe()
    tap.unsubscribe(q)
    assert q.get_nowait() is None        # the wake-up sentinel
    tap._publish(b"after")
    with pytest.raises(queue.Empty):
        q.get_nowait()


# ---------------------------------------------------------------------------
# wav_header
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rate, channels", [
    (48000, 2), (44100, 2), (48000, 1), (44100, 1), (96000, 6),
])
def test_wav_header_is_44_bytes_of_canonical_riff(rate, channels):
    """Guards against a header a receiver will not parse.

    Several receivers accept only the canonical 44-byte PCM header and stop
    before the first sample on anything else. The rate and channel count in it
    have to match what is actually sent, or the stream plays at the wrong
    speed -- which sounds like a bad capture rather than a bad header.
    """
    header = wav_header(rate, channels, 1000)
    assert len(header) == 44
    assert header[0:4] == b"RIFF"
    assert header[8:12] == b"WAVE"
    assert header[12:16] == b"fmt "
    assert header[36:40] == b"data"

    size, fmt, ch, sr, byte_rate, block, bits = struct.unpack(
        "<IHHIIHH", header[16:36])
    assert size == 16                     # PCM fmt chunk length
    assert fmt == 1                       # 1 == uncompressed PCM
    assert ch == channels
    assert sr == rate
    assert bits == 16
    assert block == channels * 2
    assert byte_rate == rate * channels * 2

    assert struct.unpack("<I", header[4:8])[0] == 1000 + 36
    assert struct.unpack("<I", header[40:44])[0] == 1000


def test_wav_header_sizes_are_clamped_to_32_bits():
    """Guards against a struct.error on the endless-stream size.

    A live capture has no length, so the header declares a very large one.
    data_bytes + 36 must not be allowed to overflow the unsigned 32-bit field
    -- packing it would raise inside the pump and kill the stream at its first
    byte.
    """
    header = wav_header(48000, 2, caster_extras.ENDLESS_WAV_BYTES)
    assert len(header) == 44
    assert struct.unpack("<I", header[40:44])[0] == caster_extras.ENDLESS_WAV_BYTES

    huge = wav_header(48000, 2, 0xFFFFFFFF)
    assert struct.unpack("<I", huge[4:8])[0] == 0xFFFFFFFF
    assert struct.unpack("<I", huge[40:44])[0] == 0xFFFFFFFF


def test_endless_wav_size_is_the_signed_ceiling():
    """Guards against Sonos reading the declared length as negative.

    Receivers that parse the RIFF sizes into a SIGNED int -- Sonos among them
    -- see anything above 0x7FFFFFFF as a negative length and refuse the
    stream. The unsigned ceiling looks like the obvious choice here and is the
    wrong one.  The constant is 44 bytes short of the signed ceiling so that
    the RIFF field (data + header) stays non-negative too.
    """
    assert caster_extras.ENDLESS_WAV_BYTES == 0x7FFFFFFF - 44
