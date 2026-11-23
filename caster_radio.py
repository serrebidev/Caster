"""Small, best-effort ICY station metadata reader for Sonos radio URLs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from http.client import HTTPException
from pathlib import Path
from time import monotonic
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen


_TIMEOUT = 2.0
_MAX_METAINT = 64 * 1024
_RADIO_MIMES = {"audio/mpeg", "audio/aac", "audio/aacp"}


@dataclass(frozen=True)
class StationMetadata:
    name: str = ""
    logo_url: str = ""
    mime: str = ""


def _logo(value: str, base: str, *, header: bool = False) -> str:
    value = value.strip().strip("\"'")
    if not value:
        return ""
    try:
        parts = urlsplit(urljoin(base, value))
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            return ""
        if not header and Path(parts.path.lower()).suffix not in (".jpg", ".jpeg", ".png"):
            return ""
        return parts.geturl()
    except (TypeError, ValueError):
        return ""


def _stream_logo(block: bytes, base: str) -> str:
    text = block.rstrip(b"\0").decode("latin-1", errors="ignore")
    for item in text.split(";"):
        key, sep, value = item.partition("=")
        if sep and key.strip().lower() == "streamurl":
            return _logo(value, base)
    return ""


def radio_candidate(url: str) -> bool:
    """Limit station probing to plausible direct HTTP audio URLs."""
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return False
    host = (parts.hostname or "").lower()
    if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
        return False
    if Path(parts.path.lower()).suffix in (".m3u8", ".m3u", ".pls", ".ts"):
        return False
    if len(parts.path.strip("/").split("/")) >= 3 and parts.path.rstrip("/").split("/")[-1].isdigit():
        return False  # Common extensionless IPTV portal channel URL.
    return Path(parts.path.lower()).suffix in ("", ".mp3", ".aac", ".aacp")


def read_station_metadata(url: str) -> StationMetadata | None:
    """Return confirmed ICY station metadata; failures leave normal playback intact."""
    deadline = monotonic() + _TIMEOUT
    station = None
    try:
        parts = urlsplit(str(url))
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            return None
        request = Request(url, headers={"Icy-MetaData": "1"})
        with urlopen(request, timeout=_TIMEOUT) as response:
            headers = response.headers
            name = headers.get("icy-name", "").strip()
            if not name:
                return None
            try:
                name = name.encode("latin-1").decode("utf-8")
            except UnicodeError:
                pass
            mime = headers.get("content-type", "").partition(";")[0].strip().lower()
            if mime not in _RADIO_MIMES:
                return None
            station = StationMetadata(name=name, mime=mime)
            base = response.geturl() or url
            artwork = _logo(headers.get("icy-logo", ""), base, header=True)
            if not artwork:
                artwork = _logo(headers.get("icy-logo-url", ""), base, header=True)
            if artwork:
                return replace(station, logo_url=artwork)
            try:
                metaint = int(headers.get("icy-metaint", ""))
            except (TypeError, ValueError):
                return station
            if metaint <= 0 or metaint > _MAX_METAINT:
                return station

            def read_bytes(size: int) -> bytes:
                data = bytearray()
                while len(data) < size and monotonic() < deadline:
                    try:
                        chunk = response.read1(size - len(data))
                    except (OSError, TimeoutError):
                        break
                    if not chunk:
                        break
                    data.extend(chunk)
                return bytes(data)

            if len(read_bytes(metaint)) != metaint:
                return station
            length = read_bytes(1)
            if len(length) != 1 or not length[0]:
                return station
            block = read_bytes(length[0] * 16)
            if len(block) != length[0] * 16:
                return station
            return replace(station, logo_url=_stream_logo(block, base))
    except (OSError, ValueError, HTTPException, TimeoutError):
        return station
