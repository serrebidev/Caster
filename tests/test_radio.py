"""Best-effort Sonos station lookup and routing, with no real network."""

from io import BytesIO
from unittest.mock import Mock
from xml.etree import ElementTree

import caster
import caster_devices
import caster_radio
from caster_radio import StationMetadata, read_station_metadata


class _Response(BytesIO):
    def __init__(self, body=b"", headers=None, final_url="http://radio.test/live"):
        super().__init__(body)
        self.headers = headers or {}
        self.final_url = final_url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def geturl(self):
        return self.final_url


def _block(value):
    data = value.encode("latin-1")
    size = (len(data) + 15) // 16 * 16
    return bytes([size // 16]) + data.ljust(size, b"\0")


def test_reads_icy_name_and_logo_headers(monkeypatch):
    response = _Response(headers={"icy-name": "Jazz FM",
                                  "content-type": "Audio/MPEG; charset=utf-8",
                                  "icy-logo": "/images/logo.png"},
                         final_url="https://cdn.test/live")
    monkeypatch.setattr(caster_radio, "urlopen", lambda req, timeout: response)
    assert read_station_metadata("http://radio.test/live") == StationMetadata(
        "Jazz FM", "https://cdn.test/images/logo.png", "audio/mpeg")


def test_reads_only_image_streamurl_from_icy_block(monkeypatch):
    block = _block("StreamTitle='Now playing';StreamUrl='covers/station.jpg';")
    monkeypatch.setattr(caster_radio, "urlopen", lambda *a, **k: _Response(
        b"x" * 8 + block,
        {"icy-name": "Station", "icy-metaint": "8",
         "content-type": "audio/aacp"}, "https://cdn.test/path/live"))
    assert read_station_metadata("http://radio.test/live") == StationMetadata(
        "Station", "https://cdn.test/path/covers/station.jpg", "audio/aacp")


def test_timeout_and_non_radio_mime_fall_back(monkeypatch):
    monkeypatch.setattr(caster_radio, "urlopen", lambda *a, **k: (_ for _ in ()).throw(
        TimeoutError("offline")))
    assert read_station_metadata("http://radio.test/live") is None
    monkeypatch.setattr(caster_radio, "urlopen", lambda *a, **k: _Response(headers={
        "icy-name": "Not a supported stream", "content-type": "audio/ogg"}))
    assert read_station_metadata("http://radio.test/live") is None


def test_artwork_timeout_preserves_station_name(monkeypatch):
    class Slow(_Response):
        def read1(self, size):
            raise TimeoutError("quiet stream")

    monkeypatch.setattr(caster_radio, "urlopen", lambda *a, **k: Slow(headers={
        "icy-name": "Still a station", "icy-metaint": "16",
        "content-type": "audio/mpeg"}))
    assert read_station_metadata("http://radio.test/live") == StationMetadata(
        "Still a station", mime="audio/mpeg")


def test_sonos_uses_radio_transport_only_for_confirmed_http_station(monkeypatch):
    zone = Mock()
    zone.group.coordinator = zone
    monkeypatch.setattr(caster_devices, "_sonos_device", lambda ip: zone)
    station = StationMetadata("Jazz & Soul", "https://img.test/logo.png", "audio/aac")
    caster_devices.sonos_play("speaker", "http://radio.test/live", station=station)
    args = dict(zone.avTransport.SetAVTransportURI.call_args.args[0])
    assert args["CurrentURI"] == "x-rincon-mp3radio://radio.test/live"
    xml = ElementTree.fromstring(args["CurrentURIMetaData"])
    assert xml.findtext(".//{*}title") == "Jazz & Soul"
    assert xml.findtext(".//{*}albumArtURI") == "https://img.test/logo.png"


def test_only_pasted_sonos_url_flow_opts_into_radio_worker(frame, monkeypatch):
    calls = []
    frame._play_sonos = lambda *args, **kwargs: calls.append(
        "radio" if kwargs.get("radio_session") is not None else "sonos")
    frame._play_airplay = lambda *args, **kwargs: calls.append("airplay")
    frame._dispatch(caster.Device("sonos", "Kitchen", {"ip": "192.0.2.2"}),
                    "http://radio.test/live")
    frame._dispatch(caster.Device("sonos", "Kitchen", {"ip": "192.0.2.2"}),
                    "http://radio.test/live", radio_session=object())
    frame._dispatch(caster.Device("airplay", "Room", {}),
                    "http://radio.test/live", radio_session=object())
    assert calls == ["sonos", "radio", "airplay"]


def test_sonos_lookup_timeout_uses_original_play_call(frame, monkeypatch):
    session = []
    frame._targets = session
    frame._stop_flag = False
    frame.set_status = lambda *args, **kwargs: None
    frame._ui = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    monkeypatch.setattr(caster_radio, "read_station_metadata", lambda url: None)
    monkeypatch.setattr(caster, "read_station_metadata", lambda url: None)
    calls = []
    monkeypatch.setattr(caster, "sonos_play", lambda *args, **kwargs: calls.append(
        (args, kwargs)))

    class ImmediateThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(caster.threading, "Thread", ImmediateThread)
    frame._play_sonos(caster.Device("sonos", "Kitchen", {"ip": "192.0.2.2"}),
                      "http://radio.test/live", radio_session=session)
    assert calls == [(("192.0.2.2", "http://radio.test/live", "Caster",
                       caster.guess_mime("http://radio.test/live")), {})]


def test_stale_sonos_lookup_does_not_start_playback(frame, monkeypatch):
    old_session = []
    frame._targets = []
    frame._stop_flag = False
    frame.set_status = lambda *args, **kwargs: None
    frame._ui = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    monkeypatch.setattr(caster, "read_station_metadata", lambda url: StationMetadata(
        "Old station", mime="audio/mpeg"))
    calls = []
    monkeypatch.setattr(caster, "sonos_play", lambda *args, **kwargs: calls.append(args))

    class ImmediateThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(caster.threading, "Thread", ImmediateThread)
    frame._play_sonos(caster.Device("sonos", "Kitchen", {"ip": "192.0.2.2"}),
                      "http://radio.test/live", radio_session=old_session)
    assert calls == []


def test_new_session_during_receiver_prepare_discards_queued_play(frame, monkeypatch):
    dev = caster.Device("sonos", "Kitchen", {"ip": "192.0.2.2"})
    frame.selected_devices = lambda: [dev]
    frame.url_box = Mock()
    frame.url_box.GetValue.return_value = "http://radio.test/live"
    frame.stop_silent = lambda: setattr(frame, "_stop_flag", True)
    frame._stop_flag = False
    frame.settings = Mock()
    frame._refresh_recent = lambda: None
    frame.set_status = lambda *args, **kwargs: None
    frame._group_musiccast = lambda devices: devices
    frame._group_sonos = lambda devices: devices
    dispatched = []
    frame._dispatch = lambda *args, **kwargs: dispatched.append(args)
    frame._musiccast_prepare = lambda devices: setattr(frame, "_targets", [])
    frame._ui = lambda callback: callback()

    class ImmediateThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(caster.threading, "Thread", ImmediateThread)
    frame.play()
    assert dispatched == []


def test_radio_candidate_skips_youtube_playlists_and_portal_channels():
    assert caster_radio.radio_candidate("http://radio.test/live")
    assert caster_radio.radio_candidate("http://radio.test/song.mp3")
    assert not caster_radio.radio_candidate("https://youtube.com/watch?v=abc")
    assert not caster_radio.radio_candidate("http://iptv.test/user/pass/123")
    assert not caster_radio.radio_candidate("http://radio.test/live.m3u8")
