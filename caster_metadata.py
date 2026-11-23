"""Read embedded metadata from local audio files for receiver playback."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from tinytag import TinyTag


@dataclass(frozen=True)
class TrackMetadata:
    title: str
    artist: str = ""
    album: str = ""
    artwork: bytes = b""
    artwork_mime: str = ""
    artwork_url: str = ""


def xml_text(value: str) -> str:
    """Escape text and remove characters XML 1.0 cannot represent."""
    safe = "".join(
        char for char in str(value)
        if char in "\t\n\r"
        or 0x20 <= ord(char) <= 0xD7FF
        or 0xE000 <= ord(char) <= 0xFFFD
        or 0x10000 <= ord(char) <= 0x10FFFF
    )
    return escape(safe)


def read_local_track(path: str | Path) -> TrackMetadata:
    """Read common embedded audio tags, falling back to the file basename."""
    path = Path(path)
    try:
        tag = TinyTag.get(path, image=True, duration=False)
    except Exception:
        return TrackMetadata(path.name)

    images = getattr(tag, "images", None)
    image = (getattr(images, "front_cover", None)
             or getattr(images, "any", None)) if images else None
    data = image.data if image else b""
    if data.startswith(bytes.fromhex("ffd8ff")):
        mime = "image/jpeg"
    elif data.startswith(bytes.fromhex("89504e470d0a1a0a")):
        mime = "image/png"
    else:
        data, mime = b"", ""
    return TrackMetadata((tag.title or "").strip() or path.name,
                         (tag.artist or "").strip(), (tag.album or "").strip(),
                         artwork=data, artwork_mime=mime)


def didl_metadata_fields(track: TrackMetadata | None) -> str:
    """Return escaped optional artist/album DIDL properties."""
    if track is None:
        return ""
    fields = []
    if track.artist:
        fields.append(f"<upnp:artist>{xml_text(track.artist)}</upnp:artist>")
    if track.album:
        fields.append(f"<upnp:album>{xml_text(track.album)}</upnp:album>")
    if track.artwork_url:
        fields.append(
            f"<upnp:albumArtURI>{xml_text(track.artwork_url)}</upnp:albumArtURI>")
    return "".join(fields)
