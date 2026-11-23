from types import SimpleNamespace
import threading
from xml.etree import ElementTree

import caster
import caster_extras
from caster import Device
from caster_metadata import TrackMetadata, didl_metadata_fields, read_local_track
from caster_devices import sonos_didl


def test_read_local_track_fields_and_artwork(monkeypatch, tmp_path):
    image = b"\xff\xd8\xffjpeg"
    monkeypatch.setattr(
        "caster_metadata.TinyTag.get",
        lambda path, **kwargs: SimpleNamespace(
            title=" Song ", artist=" Artist ", album=" Album ",
            images=SimpleNamespace(front_cover=SimpleNamespace(data=image), any=None)),
    )
    path = tmp_path / "fallback.mp3"
    result = read_local_track(path)
    assert result == TrackMetadata("Song", "Artist", "Album", image, "image/jpeg")


def test_reads_id3_text_fields_from_real_local_file(tmp_path):
    def frame(frame_id, value):
        body = b"\x00" + value.encode("utf-8")
        return (frame_id.encode("ascii") + len(body).to_bytes(4, "big")
                + b"\x00\x00" + body)

    frames = (frame("TIT2", "Night & Day") + frame("TPE1", "A & B")
              + frame("TALB", "Live <set>"))
    size = len(frames)
    syncsafe = bytes(((size >> 21) & 0x7f, (size >> 14) & 0x7f,
                      (size >> 7) & 0x7f, size & 0x7f))
    path = tmp_path / "fallback-name.mp3"
    path.write_bytes(b"ID3\x03\x00\x00" + syncsafe + frames + b"\xff\xfb\x90\x64")
    assert read_local_track(path) == TrackMetadata(
        "Night & Day", "A & B", "Live <set>")


def test_reads_id3_front_cover_from_real_local_file(tmp_path):
    jpeg = b"\xff\xd8\xff\xe0sample-cover\xff\xd9"
    body = b"\x00image/jpeg\x00\x03\x00" + jpeg
    apic = b"APIC" + len(body).to_bytes(4, "big") + b"\x00\x00" + body
    size = len(apic)
    syncsafe = bytes(((size >> 21) & 0x7f, (size >> 14) & 0x7f,
                      (size >> 7) & 0x7f, size & 0x7f))
    path = tmp_path / "cover.mp3"
    path.write_bytes(b"ID3\x03\x00\x00" + syncsafe + apic + b"\xff\xfb\x90\x64")
    metadata = read_local_track(path)
    assert metadata.artwork == jpeg
    assert metadata.artwork_mime == "image/jpeg"


def test_cast_and_didl_metadata_fields_are_escaped(frame):
    track = TrackMetadata("Song", "Artist & Co", "A < B",
                          artwork_url="http://host/art?a=1&b=2")
    assert frame._cast_load_options("http://media/file.mp3", track) == {
        "metadata": {
            "metadataType": caster.METADATA_TYPE_MUSICTRACK,
            "title": "Song", "artist": "Artist & Co", "albumName": "A < B",
            "images": [{"url": "http://host/art?a=1&b=2"}],
        }
    }
    fields = didl_metadata_fields(track)
    assert "Artist &amp; Co" in fields
    assert "A &lt; B" in fields
    assert "a=1&amp;b=2" in fields


def test_sonos_and_upnp_didl_embed_escaped_track_fields(monkeypatch):
    track = TrackMetadata("Night & Day", "A & B", "Live <set>",
                          artwork_url="http://host/art?a=1&b=2")
    root = ElementTree.fromstring(sonos_didl(
        "fallback", "http://host/song?a=1&b=2", "audio/mpeg", track=track))
    assert next(n.text for n in root.iter() if n.tag.endswith("artist")) == "A & B"
    assert next(n.text for n in root.iter() if n.tag.endswith("album")) == "Live <set>"
    calls = []
    monkeypatch.setattr(caster_extras, "_soap",
                        lambda url, action, body: calls.append((action, body)))
    caster_extras.upnp_play("http://host/control", "http://host/song?a=1&b=2",
                            "fallback", "audio/mpeg",
                            "object.item.audioItem.musicTrack", track=track)
    soap = ElementTree.fromstring(calls[0][1])
    metadata = next(n.text for n in soap.iter()
                    if n.tag.endswith("CurrentURIMetaData"))
    didl = ElementTree.fromstring(metadata)
    assert next(n.text for n in didl.iter() if n.tag.endswith("title")) == "Night & Day"
    assert next(n.text for n in didl.iter() if n.tag.endswith("albumArtURI")) == \
        "http://host/art?a=1&b=2"


def test_cast_file_airplay_does_not_read_tags_and_keeps_call(frame, monkeypatch):
    dev = Device("airplay", "Receiver", {"host": "192.0.2.20"})
    server = SimpleNamespace(start=lambda: "http://192.0.2.1:9000/song.mp3",
                             mime="audio/mpeg")
    calls = []
    monkeypatch.setattr(caster, "FileServer", lambda path: server)
    monkeypatch.setattr(caster, "read_local_track",
                        lambda path: (_ for _ in ()).throw(AssertionError("tag read")))
    frame.selected_device = lambda: dev
    frame.stop_silent = lambda: None
    frame.set_status = lambda *args, **kwargs: None
    frame._play_airplay = lambda *args, **kwargs: calls.append((args, kwargs))
    frame.cast_file("C:/media/song.mp3")
    assert calls == [((dev, "C:/media/song.mp3"), {"is_live": False})]


def test_musiccast_audio_file_still_prepares_network_input(frame, monkeypatch):
    dev = Device("upnp", "Receiver", {"control_url": "http://192.0.2.20/control"})
    dev.musiccast = {"host": "192.0.2.20", "zone": "main"}
    server = SimpleNamespace(start=lambda: "http://192.0.2.1:9000/song.mp3",
                             mime="audio/mpeg")
    track = TrackMetadata("Song", "Artist")
    ready = threading.Event()
    calls = []
    monkeypatch.setattr(caster, "FileServer", lambda path: server)
    monkeypatch.setattr(caster, "read_local_track", lambda path: track)
    frame.selected_device = lambda: dev
    frame.stop_silent = lambda: None
    frame.set_status = lambda *args, **kwargs: None
    frame._musiccast_prepare = lambda devices: calls.append("prepare")
    frame._play_upnp = lambda *args, **kwargs: (calls.append(kwargs["track"]), ready.set())
    frame._ui = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    frame.cast_file("C:/media/song.mp3")
    assert ready.wait(2)
    assert calls == ["prepare", track]
