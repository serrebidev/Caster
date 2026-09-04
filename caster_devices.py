"""Receiver protocols beyond Chromecast, AirPlay and plain UPnP/DLNA.

Sonos, Roku and Kodi each need their own discovery and their own way of
being told to play a URL. Kept out of caster.py so the GUI is not carrying
four protocol clients, and free of caster imports so there is no cycle.

A note on what is deliberately absent. Miracast is a Windows display
feature rather than a network protocol this app could speak; Spotify
Connect needs a Premium account and a proprietary client; Bluetooth is not
casting. DLNA already reaches most smart TVs (Samsung, LG, Sony), Xbox and
almost every AV receiver, so those are not separate integrations.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.parse
import urllib.request as _urlreq

# ---------------------------------------------------------------------------
# Sonos
# ---------------------------------------------------------------------------
#
# Sonos speaks UPnP AVTransport, but is not reachable through ordinary DLNA
# discovery: it answers SSDP as a ZonePlayer, not a MediaRenderer. It also
# advertises AirPlay 2, which is a trap -- that path needs MFi hardware
# authentication no Python client can do, so an AirPlay cast to a Sonos
# fails with the speaker refusing the audio port. Both of those are why
# Sonos gets its own discovery here and is filtered out of the AirPlay and
# UPnP lists (see looks_like_sonos).

_SONOS_TIMEOUT = 4.0        # SoCo defaults to 20s; one asleep speaker then
                            # stalls every code path that touches it.
_soco_lock = threading.Lock()
_soco_ready = False


def _soco():
    """The soco module, configured, or None when it is not installed.

    Imported lazily: it is only needed if there is a Sonos on the network,
    and importing it costs a noticeable fraction of a second at startup.
    """
    global _soco_ready
    try:
        import soco
        import soco.config as soco_config
    except ImportError:
        return None
    with _soco_lock:
        if not _soco_ready:
            soco_config.REQUEST_TIMEOUT = _SONOS_TIMEOUT
            _soco_ready = True
    return soco


def sonos_available() -> bool:
    return _soco() is not None


def sonos_discover(timeout: int = 5, seed_ips: list | None = None) -> list:
    """Sonos zones as [(name, ip), ...].

    Seed addresses are for a household on a subnet or VLAN that multicast
    cannot reach: touching any one speaker yields the whole household
    through its zone group topology, so one address is enough per system.
    """
    soco = _soco()
    if soco is None:
        return []
    zones = set()
    try:
        zones |= soco.discover(timeout=timeout) or set()
    except Exception:
        pass
    for ip in (seed_ips or []):
        ip = str(ip).strip()
        if not ip:
            continue
        try:
            device = soco.SoCo(ip)
            _ = device.player_name          # warm the topology cache
            zones |= (device.visible_zones or {device})
        except Exception:
            pass
    found = []
    for zone in zones:
        try:
            found.append((zone.player_name, zone.ip_address))
        except Exception:
            pass
    return sorted(set(found))


def _sonos_device(ip: str):
    soco = _soco()
    if soco is None:
        raise RuntimeError("Sonos support needs the 'soco' package")
    return soco.SoCo(ip)


def _sonos_coordinator(device):
    """The zone that actually accepts transport commands for this group.

    Sonos rejects play and stop sent to a group member that is not the
    coordinator. Grouped speakers all replicate the coordinator anyway, so
    routing through it means acting on any member controls the whole group,
    which is what the Sonos app itself shows as one unit.
    """
    try:
        group = device.group
        if group is not None and group.coordinator is not None:
            return group.coordinator
    except Exception:
        pass
    return device


def sonos_didl(title: str, url: str, mime: str) -> str:
    """DIDL-Lite for a live stream from this machine.

    Tagged as a plain music track on purpose. SoCo's play_uri shortcut
    labels whatever it builds as a TuneIn broadcast, complete with a
    third-party service id -- which tells the speaker this is internet
    radio rather than audio from a device on the LAN, and makes Sonos
    withhold its tone controls while it plays.
    """
    from xml.sax.saxutils import escape
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="1" parentID="0" restricted="1">'
        f"<dc:title>{escape(title)}</dc:title>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f'<res protocolInfo="http-get:*:{escape(mime)}:*">{escape(url)}</res>'
        "</item></DIDL-Lite>"
    )


def sonos_play(ip: str, url: str, title: str = "Caster",
               mime: str = "audio/wav") -> None:
    zone = _sonos_coordinator(_sonos_device(ip))
    zone.avTransport.SetAVTransportURI([
        ("InstanceID", 0),
        ("CurrentURI", url),
        ("CurrentURIMetaData", sonos_didl(title, url, mime)),
    ])
    zone.avTransport.Play([("InstanceID", 0), ("Speed", 1)])


def sonos_stop(ip: str) -> None:
    try:
        _sonos_coordinator(_sonos_device(ip)).stop()
    except Exception:
        pass


def sonos_set_volume(ip: str, level: int) -> None:
    try:
        _sonos_device(ip).volume = max(0, min(100, int(level)))
    except Exception:
        pass


def sonos_group(ips: list) -> str:
    """Join several speakers into one group so they play in sync.

    Returns the coordinator's address, which is the only one that should
    then be told to play. Sending the same stream to each speaker
    separately would leave them audibly out of step with each other.
    """
    if not ips:
        return ""
    coordinator_ip, followers = ips[0], ips[1:]
    coordinator = _sonos_device(coordinator_ip)
    for ip in followers:
        try:
            _sonos_device(ip).unjoin()
        except Exception:
            pass
    if followers:
        time.sleep(0.5)
    for ip in followers:
        try:
            _sonos_device(ip).join(coordinator)
        except Exception:
            pass
    if followers:
        time.sleep(0.5)
    return coordinator_ip


def looks_like_sonos(name: str, extra: str = "") -> bool:
    """Whether a discovered AirPlay or UPnP entry is really a Sonos.

    Sonos answers both, and neither answer is usable: the AirPlay one needs
    hardware authentication, and the UPnP one is a ZonePlayer that wants
    Sonos' own transport handling. Hiding those duplicates also stops one
    speaker appearing three times in the device list.
    """
    blob = f"{name} {extra}".lower()
    return "sonos" in blob or "rincon" in blob


# ---------------------------------------------------------------------------
# Roku (External Control Protocol)
# ---------------------------------------------------------------------------
#
# Roku has no general "play this URL" API. What it does have is the Roku
# Media Player channel, which can be launched with a URL as a parameter --
# the same mechanism the "Play on Roku" feature uses.

ROKU_MEDIA_PLAYER = "2213"

#: Roku Media Player plays HLS, MP4 and MKV. It has no MPEG-TS support, so a
#: live capture has to reach it as MP4.
ROKU_VIDEO_FORMATS = {"video/mp4": "mp4", "application/vnd.apple.mpegurl": "hls"}
ROKU_AUDIO_FORMATS = {"audio/wav": "wav", "audio/mpeg": "mp3",
                      "audio/aac": "aac", "audio/flac": "flac"}


def roku_discover(timeout: int = 4) -> list:
    """Rokus as [(name, base_url), ...], via SSDP."""
    responses = _ssdp_search("roku:ecp", timeout)
    found = []
    for location in responses:
        base = location.rstrip("/")
        # The SSDP LOCATION points at the device description; ECP lives at
        # the server root.
        parts = urllib.parse.urlsplit(base)
        base = f"{parts.scheme}://{parts.netloc}"
        name = _roku_name(base)
        if name:
            found.append((name, base))
    return sorted(set(found))


def _roku_name(base: str) -> str:
    try:
        with _urlreq.urlopen(f"{base}/query/device-info", timeout=4) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception:
        return ""
    for tag in ("friendly-device-name", "user-device-name", "default-device-name",
                "model-name"):
        m = re.search(rf"<{tag}>([^<]+)</{tag}>", body)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def roku_play(base: str, url: str, mime: str = "video/mp4",
              title: str = "Caster") -> None:
    audio = mime.startswith("audio/")
    params = {"t": "a" if audio else "v", "u": url}
    if audio:
        params["audioName"] = title
        params["audioFormat"] = ROKU_AUDIO_FORMATS.get(mime, "mp3")
    else:
        params["videoName"] = title
        params["videoFormat"] = ROKU_VIDEO_FORMATS.get(mime, "mp4")
    target = f"{base}/launch/{ROKU_MEDIA_PLAYER}?{urllib.parse.urlencode(params)}"
    _post(target)


def roku_stop(base: str) -> None:
    # There is no "stop" in ECP; going Home leaves the media player.
    try:
        _post(f"{base}/keypress/Home")
    except Exception:
        pass


def roku_key(base: str, key: str) -> None:
    """Send one ECP keypress (Play, VolumeUp, VolumeDown, VolumeMute, ...)."""
    _post(f"{base}/keypress/{key}")


def _post(url: str, timeout: int = 6) -> bytes:
    req = _urlreq.Request(url, data=b"", method="POST")
    with _urlreq.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------------------
# Kodi (JSON-RPC)
# ---------------------------------------------------------------------------
#
# Kodi plays essentially anything ffmpeg can, MPEG-TS included, which makes
# it the most forgiving target here for a live capture.

KODI_SERVICE = "_xbmc-jsonrpc-h._tcp.local."


def kodi_discover(timeout: int = 4) -> list:
    """Kodi instances as [(name, base_url), ...], over mDNS."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return []
    hits: list = []

    class _Listener:
        def add_service(self, zc, type_, name):
            hits.append((type_, name))

        def update_service(self, zc, type_, name):
            pass

        def remove_service(self, zc, type_, name):
            pass

    found = []
    zc = Zeroconf()
    try:
        ServiceBrowser(zc, KODI_SERVICE, _Listener())
        time.sleep(timeout)
        for type_, name in list(hits):
            try:
                info = zc.get_service_info(type_, name, 3000)
            except Exception:
                continue
            if not info or not info.addresses:
                continue
            host = socket.inet_ntoa(info.addresses[0])
            label = name.split(".")[0] or host
            found.append((label, f"http://{host}:{info.port or 8080}"))
    finally:
        try:
            zc.close()
        except Exception:
            pass
    return sorted(set(found))


def _kodi_rpc(base: str, method: str, params: dict | None = None,
              auth: tuple = ("", ""), timeout: int = 8):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params or {}}).encode()
    req = _urlreq.Request(f"{base}/jsonrpc", data=payload, method="POST",
                          headers={"Content-Type": "application/json"})
    user, password = auth
    if user:
        import base64
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with _urlreq.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def kodi_play(base: str, url: str, auth: tuple = ("", "")) -> None:
    _kodi_rpc(base, "Player.Open", {"item": {"file": url}}, auth)


def kodi_stop(base: str, auth: tuple = ("", "")) -> None:
    try:
        active = _kodi_rpc(base, "Player.GetActivePlayers", {}, auth)
        for player in active.get("result", []):
            _kodi_rpc(base, "Player.Stop",
                      {"playerid": player.get("playerid", 1)}, auth)
    except Exception:
        pass


def kodi_pause(base: str, auth: tuple = ("", "")) -> None:
    active = _kodi_rpc(base, "Player.GetActivePlayers", {}, auth)
    for player in active.get("result", []):
        _kodi_rpc(base, "Player.PlayPause",
                  {"playerid": player.get("playerid", 1)}, auth)


def kodi_set_volume(base: str, level: int, auth: tuple = ("", "")) -> None:
    try:
        _kodi_rpc(base, "Application.SetVolume",
                  {"volume": max(0, min(100, int(level)))}, auth)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# SSDP
# ---------------------------------------------------------------------------

def _ssdp_search(search_target: str, timeout: int = 4) -> list:
    """M-SEARCH for one ST; returns the LOCATION header of each responder."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {max(1, timeout - 1)}\r\n"
        f"ST: {search_target}\r\n"
        "\r\n"
    ).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout)
    locations = []
    try:
        sock.sendto(msg, ("239.255.255.250", 1900))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, _ = sock.recvfrom(65536)
            except socket.timeout:
                break
            for line in data.decode("utf-8", "replace").splitlines():
                if line.lower().startswith("location:"):
                    location = line.split(":", 1)[1].strip()
                    if location and location not in locations:
                        locations.append(location)
                    break
    except OSError:
        pass
    finally:
        sock.close()
    return locations
