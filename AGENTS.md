# Caster — agent notes

Repo dir YamahaAmp. App name Caster. Windows only. Python 3.14.7. wxPython GUI.

## What app do

Send URL, local file, whole screen, one app window, or PC sound to network box.
Six protocols: Chromecast, AirPlay (RAOP, audio only), UPnP/DLNA, Sonos, Roku, Kodi.
Tick many boxes = multi-room.

## Run and build

```
py caster.py                 # run
py release.py patch          # bump, tag, PyInstaller, zip. needs clean tree
py release.py --no-bump      # rebuild, no commit, no tag
py -m PyInstaller caster.spec --noconfirm
```

release.py refuses dirty tree. Deps in requirements.txt. soco optional — without it every
other protocol still works, Sonos just never appears.

Build output `dist/Caster/`. ffmpeg.exe copied beside exe, 96 MB. Zip ~92 MB, 1588 files.

## Files

- `caster.py` — GUI, MainFrame, discovery, per-protocol play, HlsRelay, LoopThread
- `caster_extras.py` — UPnP SOAP, FileServer, AudioTap, ScreenSource, ffmpeg
- `caster_devices.py` - Sonos, Roku, Kodi, MusicCast. No caster imports. Keep it that way, no cycle
- `caster_ui.py` — NvdaSpeaker, labelled(), HotkeyManager, TrayIcon, SettingsDialog
- `caster_config.py` — Settings JSON at `%APPDATA%\Caster\settings.json`, QUALITY_PRESETS

## Accessibility rules — hard

User blind. NVDA. Break these = app unusable.

- Build wx.StaticText FIRST, then control. `labelled(parent, sizer, text, build_callable)`
  takes a builder, not a ready control, for this reason. Windows names a control from the
  nearest static made before it. Control-first = every control wears previous one's name:
  URL box announced "Devices:" and answered to Alt+D.
- Reordering windows after build does NOT fix it. wxSpinCtrl = buddy edit + up-down, only
  the up-down can move, focused part keeps wrong name. Creation order is the only fix.
- wxSL_LABELS injects its own min/max/value statics between label and slider. Slider needs
  `_NamedAccessible` to pin the name outright. Else volume slider announces "0".
- Dialog buttons parent on the dialog, not the panel.
- `set_status(text)` speaks through NVDA. Pass `speak=False` for chatter. Six progress
  lines in a row talk over the answer that matters.
- Device list = CheckListBox, not LB_MULTIPLE. Arrowing a multi-select list toggles every
  device passed over.
- No ASCII art or box tables in any user-facing string.

## Discovery

All six protocols run parallel in a ThreadPoolExecutor. `DISCOVER_SECONDS = 5` in
caster.py. Serial was 35.1 s. Parallel 5.1 s, same devices found.

Merge order fixed, not completion order: chromecast, airplay, upnp, roku, kodi by
setdefault, then sonos `update()` last. Sonos wins — it answers AirPlay and UPnP too and
both of those paths are broken for it.

One Zeroconf shared by the Cast browser and `kodi_discover(zc=...)`. Two instances = port
5353 bound twice, same queries sent twice.

Never fetch HTTP inside an SSDP recv loop. One stalled renderer eats the whole window and
every reply arriving meanwhile is lost. Collect locations, close socket, fetch in parallel.

Resolve mDNS services as announced, not after the browse window. Serial resolve at 3-4 s
each was most of the old cost.

Measured 2026-09-07 on this LAN: the full six-protocol discovery takes 5.31 s
(Cast 5.00, AirPlay 5.01, Kodi 5.00, Roku 5.07, Sonos 5.10, UPnP 5.31).
That is the five-second listen window plus 0.31 s for parallel UPnP capability
reads. Do not shorten the window or remove repeated SSDP sends to make this
number smaller; the result would be faster but unreliable.

## Protocol traps

- Sonos answers SSDP as ZonePlayer, not MediaRenderer. Needs own discovery via soco.
- Sonos AirPlay 2 = trap. Wants MFi hardware auth, Python cannot do it, speaker refuses
  the audio port. Filter with `looks_like_sonos()`.
- Caster uses RAOP and has no pairing flow. Discovery must show only a
  `Protocol.RAOP` service whose pairing requirement is `NotNeeded`; an
  AirPlay-only service or one requiring pairing is guaranteed to fail.
- Multi-room AirPlay owns one runner, RAOP connection and (if needed) ffmpeg
  process per device label. Never use a shared cleanup handle: when one room
  ends it must not close another room or kill its audio pipe; Stop cancels all
  runners and close waits for all of them.
- Sonos transport commands only to the group coordinator. Members reject them.
- SoCo default REQUEST_TIMEOUT 20 s. Set to 4.0. One asleep speaker stalls everything.
- Don't use SoCo `play_uri` — tags the stream as TuneIn radio, Sonos then hides its tone
  controls. Build own DIDL, class `object.item.audioItem.musicTrack`.
- Roku has no play-URL API. Launch channel `2213` (Roku Media Player) with `u=` param.
  No MPEG-TS support — must be MP4 or HLS. No stop in ECP, send `keypress/Home`.
- Kodi plays anything ffmpeg does, MPEG-TS included. Most forgiving target here.
- Cast rejects raw MPEG-TS. Remux to HLS through HlsRelay.
- Cast needs the receiver app launched first: `start_app("CC1AD845", force_launch=True)`
  then sleep 2.5. Else play_media silently no-ops, LOADING -> IDLE.
- Cast LIVE vs BUFFERED wrong = load rejected. Code retries the other one once.
- HLS needs >= 3x TARGETDURATION buffered or Cast refuses to start. TRAIL_KEEP = 6.
- MusicCast (YXC) ignores a pushed URL unless input is already on the network source, and
  gives no error. Call `yxc_set_input(host)` before push.
- Windows mimetypes maps `.ts` to TypeScript and `.m3u8` to junk. Force the right types in
  `HlsFileHandler._TYPES`.
- FFM smart TVs refuse HLS without CORS headers even though the fetch is native.
- pychromecast's own browser misses some Cast devices (the FFM TVs here). Browse
  `_googlecast._tcp` directly and read TXT records.

## Connect latency - where it went, and what must not come back

Was 20-30 s to first picture. Six fixed waits in series, not one slow thing.
Measured on this box, not guessed.

- `pick_screen_grabber()` probed ddagrab for a hard 10 s EVERY launch, because
  ddagrab hangs here and the cache was a module global. Now cached to
  `settings.json` under `screen_grabber` + `screen_grabber_key`
  (COMPUTERNAME|SESSIONNAME - RDP is a different answer), probe cut to 2.5 s,
  and `prewarm_screen_grabber()` pays it in the background at startup.
  caster.py wires the load/store hooks in `_install_grabber_cache()`; without
  them caster_extras still works and simply re-probes. Do not import the
  settings into caster_extras to "simplify" this - the hooks are what keep
  that module standalone.
- `ScreenSource._verify()` opened the app's OWN url and read 32 KB. ffmpeg is
  spawned per connection, so that ran a whole encoder, killed it, and left the
  receiver to wait through a second cold start. Gone. `start(verify=True)` now
  only settles the gdigrab window spec (one frame, `-frames:v 1`), and
  `wait_for_media()` reports failure from the receiver's own connection AFTER
  the url is dispatched. Never reintroduce a self-connect check.
- Cast `start_app(force_launch=True)` + `time.sleep(2.5)` relaunched a receiver
  that was usually already up. `_ensure_receiver()` launches only when
  `cast.app_id` is something else, then polls for it. It also runs on a warm
  thread started before probing, so a TV's launch overlaps the probe and the
  relay prime instead of following them.
- The settle loop slept 1.5 s BEFORE its first look, up to 8 times. Even an
  instant cast could not be announced for 1.5 s, which a blind user hears as
  the app hanging. `_await_playing()` polls at 0.15 s. Keep it short.
- `_probe_codecs` had NO timeout: `p.stderr.read(65536)` on a server that goes
  quiet blocked forever. Now `subprocess.run(timeout=8)` with
  `-analyzeduration 2000000 -probesize 2000000` (the defaults spend 5 s on a TS
  before saying anything). `HlsRelay(codecs=...)` takes the answer when the
  caller already has it, so the source is not opened a third time.
- Relay primed 4 segments at `hls_time 2` = a guaranteed 8 s wait on a live
  source, then sat 12 s behind live at `TRAIL_KEEP = 6`. Both now come from
  QUALITY_PRESETS (`hls_time`, `hls_prime`, `hls_trail`) via `_make_relay()`.
  The `hls_prime` floor of 3 is not a preference: it is what clears the cast
  receiver's 3x TARGETDURATION rule. `hls_trail` is the jitter cushion AND the
  delay, second for second - deep for stuttering IPTV, shallow for clean
  sources.

Roughly 15 s off both paths. What is left is the receiver's own startup.

## Capture

`CONTAINERS`: mp4 -> Chromecast and Roku, mpegts -> DLNA/TV/Kodi, wav -> audio boxes.
Sonos and AirPlay always get wav — RAOP carries no video, Sonos is speakers.

wav + `pcm_is_directly_usable()` = no ffmpeg in the path at all. Lowest latency route.
AudioTap = one WASAPI loopback, many subscribers, bounded queue, drop oldest chunk. Never
let a queue build a backlog — backlog is heard as lag.

`cast_file()` must route each receiver explicitly. Chromecast and UPnP probe
the FileServer URL; Sonos and Roku need the served MIME and title; Kodi gets
its own play call; only an AirPlay device uses `_play_airplay`. Do not let all
other kinds fall through to AirPlay. A standalone MusicCast zone is a
control-only follower, so explain that a transport device must be selected.

Mixed selection (Cast + DLNA) = one ScreenSource per container, two encoders.

`_no_window_kwargs()` on every subprocess or a console flashes on screen.
`_find_ffmpeg()` order: beside frozen exe, PATH, winget glob.

## Known bugs

Fixed before the 0.5.0 release, listed so they are not reintroduced:

- `HlsRelay.stop()` never closed the server. Port and thread stayed alive for
  the life of the app, once per relay. Now shuts down and closes like
  `FileServer` does.
- `HlsRelay.start()` raised without calling `self.stop()`, leaking the server,
  its thread and `%TEMP%\caster_hls_*`. The body is now wrapped in
  `except BaseException: self.stop(); raise`.
- `HlsRelay._ffmpeg_cmd` passed `-reconnect`/`-rw_timeout` for EVERY input.
  Those are HTTP-protocol options: handed a local path, ffmpeg refused the
  whole command with "Option reconnect not found" and opened no input at all,
  so relaying a local file - which `_play_upnp` does for anything not http(s)
  - could never have worked. Now applied only to http(s) urls. `-fflags
  +genpts` also moved BEFORE `-i`; after `-i` it landed on the muxer, where it
  does nothing.
- `HlsFileHandler.relay_requests` unbounded. Now `deque(maxlen=200)`.
- A fresh `zeroconf.Zeroconf()` per Chromecast connect, never closed, and
  `stop_silent()` dropped `self.cast` without `disconnect()`. Now one
  `_cast_zeroconf()` for the session, closed in `_on_close`, and `stop_silent`
  disconnects.
- `_cast_capture` set `self._sources` AFTER the last `_stop_flag` check, so
  Stop pressed in that gap left a capture running with no handle to it. The
  list is published before the worker starts and appended to as sources come
  up.
- `_play_chromecast` and `_play_upnp` set `self._relay` only after
  `relay.start()` returned, so Stop during the prime could not find it.
  Assigned before starting.

Also fixed, and worth not reintroducing:

- `LoopThread.submit()` returned None, so `self._runner_fut` was always None and both the
  cancel on stop and the eight-second wait on close were dead code -- the app exited
  without letting AirPlay tear the session down. It returns the future.
- One `self._relay` for any number of receivers: a TS url sent to two UPnP renderers built
  two relays and the second replaced the first, orphaning an ffmpeg and an HTTP server.
  `_keep_relay()` tracks them all and `_stop_relay()` stops them all.
- Stop during AirPlay fell into the "stream ended naturally" branch and announced
  "Finished." after stop() had already said "Stopped." -- the wrong word, spoken last.
- `AudioTap.start()` left `self._thread` set after a failed start, so a retry returned at
  once and the caller believed capture was live. Note: an unknown device name does NOT
  fail - `_pick_loopback_device` falls back to the default on purpose.
- `HotkeyManager.register_all()`'s return was ignored, so a hotkey another app owned was
  lost silently; worse, wx logged the failure to a MODAL DIALOG behind the main window,
  which blocked the UI thread and took the screen reader down with it. `wx.LogNull` around
  registration, and the lost keys are reported in the status line.
- `Settings.save()` wrote its temp file into `config_dir()` rather than beside the target,
  so `path=` only half worked and a cross-volume replace would have failed silently.
- `load()` accepted a JSON `true` for an int setting, because in Python a bool IS an int:
  `"volume": true` loaded as 1, i.e. near-silence. `_same_type()` keeps them apart.
- `probe_media` treated any file starting with 0x47 as MPEG-TS, but that byte is ASCII "G"
  -- a text file, subtitle or GIF went down the live-remux path. Both branches confirm the
  packet stride now, and too few bytes to check it means "no", not "probably".

## MusicCast (Yamaha)

YXC is a control channel, not a transport. The receiver still takes audio over
AirPlay or DLNA; YXC on port 80 adds power, input, real volume, zones and
grouping. So it is ATTACHED to whichever device entry won discovery
(`_attach_musiccast`), never a device kind that plays anything. Kind
`musiccast` exists only for extra zones, which are followers: they are woken
and aimed, and `_cast_capture` skips them so no second encoder is started.

- The unit publishes what it can do. Everything optional is gated on
  `yxc_can(host, func, zone)` reading `func_list` from getFeatures, never on a
  model name. On the RX-V4A here, main has link_control and sound_program and
  zone2 has neither, and that is the unit talking, not a guess.
- Volume is NOT 0-100. `range_step` says 0-161 step 1, and `actual_volume` is
  real dB. `yxc_set_volume` maps the slider onto whatever the zone reports;
  treating it as a percentage lands between steps and throws away resolution.
- `prepareInputChange` before `setInput` is required whenever the unit lists
  `prepare_input_change`, and MusicCast's own controller does it. `yxc_set_input`
  now does both.
- getFeatures is big and static. Cached per host in `_yxc_features_cache`;
  measured 0.03 s cold, 0 s after.
- Link Audio Delay is documented as ignored while Link Control is on Stability
  Boost, so it is not attempted there. Whether Link Control affects a direct
  DLNA/AirPlay push at all is NOT documented either way -- it is a MusicCast
  link setting, so treat any improvement as unproven until measured.
- `musiccast_group()` mirrors `sonos_group()` and no-ops below two units. There
  is only one MusicCast unit on this LAN, so the multi-unit path is written
  from the spec and has never been run.

## SSDP is lossy and one search is not enough

A single M-SEARCH missed the Yamaha in roughly one scan out of two -- it is on
Wi-Fi, and multicast replies that collide are simply gone. Both
`upnp_discover` and `_ssdp_search` now repeat the search three times across the
window (`SSDP_SEARCHES`) and deduplicate by LOCATION. Measured after: 4 scans
out of 4. Do not "simplify" this back to one send.

Consequence to remember: the socket timeout is now short (0.5 s) and the loop
`continue`s on timeout instead of breaking, because breaking on the first quiet
half-second would end the scan before the later searches ever go out.

## Renderers publish what they accept

`upnp_discover` returns 4-tuples now: `(name, control_url, maker, sink_mimes)`,
where sink_mimes comes from ConnectionManager `GetProtocolInfo`.
`Device.supports_video` believes that list when it is non-empty and falls back
to `VIDEO_KINDS` when the renderer said nothing -- plenty answer badly, and
refusing to send them anything would be worse than guessing.

This matters: "UPnP renderer" spans televisions and stereo amplifiers. The
RX-V4A's sink list is 49 content types and every one is `audio/*`. Before this,
a screen cast to it built an H.264 MPEG-TS and pushed `video/mpeg` to a device
that accepts no video at all -- a whole encoder's work for something it must
refuse.

## Live sources: never let ffmpeg resume at a byte offset

This is the one that took a whole session to find, so it is written down.

IPTV servers close the connection every ten to twenty seconds. With
`-reconnect_streamed 1`, ffmpeg reconnects -- and it reconnects by asking for
`Range: bytes=<offset>`:

    Stream ends prematurely at 918900, should be 18446744073709551615
    Will reconnect at 918900 in 0 second(s), error=I/O error.
    Packet corrupt (stream = 0, dts = 3864180449).

A live stream has no such position. The server hands back its current live
edge, ffmpeg splices it in as though it followed on, and the overlap is media
the listener has already heard. It is heard as the stream jumping backwards a
few seconds, over and over. `-seekable 0` before `-i` stops the Range request.
Set on BOTH the relay and the RAOP pipe, and only for http(s).

Measured on a live IPTV source over 90s:

- as shipped: 1.88x of real time produced, 8 byte-offset resumes, 7 corrupt
  packets
- `-seekable 0`: 0.89x, 0 resumes, 0 corrupt

THE METRIC THAT MATTERS: media-seconds produced per wall-second. A live source
cannot produce more than 1.0x. Anything above is material arriving twice. Read
that ratio first; it identifies duplication instantly. Reading it as "the
source is fast" wasted hours, and so did chasing rw_timeout (raising it to 30s
made reconnects WORSE: 9 vs 1), the playlist sequence (never went backwards,
`restarts 0`), and segment filenames.

Things that look clean while this is happening, so prove nothing: the
supervisor's restart count, EXT-X-MEDIA-SEQUENCE monotonicity, segment names,
and hashing PCM blocks for repeats (a repeat offset by one sample hashes
differently -- that test is useless).

## HLS segment length is the source GOP, not hls_time

With `-c copy` ffmpeg can only cut at a keyframe, so `-hls_time 1` on a source
with a 7.5s GOP produces 7.5s segments. `hls_prime` is counted in SEGMENTS, so
asking for 6 of them there is a 45-second wait, not six seconds. A Cast
receiver refuses to start below 3x TARGETDURATION, which three segments
satisfy whatever their length -- so `hls_prime` is 3, and raising it only makes
a long-GOP channel look broken.

The 22-second start once measured on a long-GOP channel was NOT the GOP: it
was the byte-offset reconnects tearing the stream up so badly that segments
crawled out. With `-seekable 0` the same channel primes in 0.7s and another source
in 2.8s. Do not reintroduce a "long GOP means slow start" rule of thumb; it
was a symptom of the reconnect bug, not a property of the source.

Fragmented MP4 was evaluated as a way around the 3x rule and is NOT needed now
that priming is fast. Note if it is ever revisited: `-c copy` from TS into MP4
fails with "Malformed AAC bitstream detected" and needs `-bsf:a aac_adtstoasc`.

## Cast status is stale until the new session arrives

`mc.status` keeps reporting the PREVIOUS media session until the receiver
sends one for the new load, and `IDLE`/`INTERRUPTED` is what it says when a
session is replaced -- which is exactly what loading something else does. So a
load issued moments after a stop reads the old session's dying status and
looks rejected while it is in fact starting. `_await_playing` takes the
media_session_id from before `play_media` and ignores anything still carrying
it. Do not treat INTERRUPTED or CANCELLED as a rejection.

The Cast recovery watchdog runs from a wx timer. `media_controller.status` can
be absent or raise during a transient socket loss; treat that tick as
inconclusive and let the next one retry. Never let a status read escape the
timer callback, or recovery stops after the very disconnect it is meant to
handle.

## HLS restart continuity (2026-09-05)

With ffmpeg `append_list`, `-start_number` must be the existing playlist's
MEDIA-SEQUENCE, not the next segment filename: ffmpeg adds the retained
entry count itself. Passing the next filename renumbers retained segments
and can disconnect a Cast receiver. Verified with five consecutive real
ffmpeg runs and overlapping segment identity checks.

Use ffmpeg's actual DISCONTINUITY markers rather than guessing the restart
filename from disk (an unfinished segment may exist). Deduplicate markers
and retain EXT-X-DISCONTINUITY-SEQUENCE as seams leave the served window;
otherwise still-buffered segments change timeline IDs. Serialize playlist
rewrites because HTTP requests can overlap.

Chromecast may issue a `HEAD` playlist probe on the same HTTP/1.1 connection
it subsequently uses for `GET`. HEAD must return the same headers (including
Content-Length) but absolutely no body; leaking playlist bytes into that
connection corrupts the next response parser and can look like a random TV
stall.

## Diagnostics

Never put real stream URLs or their credentials in source code, tests, or
agent notes. Use example.invalid fixtures and runtime arguments for live
checks. Diagnostic traces must not record stream URLs.

For numeric IPTV channel URLs, a verified sibling `.m3u8` can avoid the raw
TS server replaying its buffer after reconnection. `_native_hls_url` checks
the actual live playlist body, not its suffix or Content-Type. Chromecast
prefers this feed and falls back to the TS relay if loading it fails. Tested
on RB Room (Cast), now at 192.168.1.76, on 2026-09-05: one provider's native
HLS played for three minutes without a session restart after the user heard
a skip-back on the TS relay. A steadily advancing Cast playback clock does
NOT prove the content did not repeat. Another provider's tested `.m3u8` endpoints
returned binary media, not playlists; do not assume the same HLS path works
there just because changing the extension returns HTTP 200.

Live Chromecast URL recovery must not depend on `_sources`: that list is
populated for capture, not ordinary URL playback. `_cast_live_loads` retains
successful live loads per receiver so the watchdog can reload the existing
relay after IDLE, without starting another source connection. Clear those
loads on Stop, and check their identity after receiver launch before reloading.
The 2026-09-05 channel probe observed five encoder restarts in about 98 seconds;
relay survival alone does not establish uninterrupted receiver playback.

`trace(event, detail)` in caster.py writes a timestamped timeline to whatever
`CASTER_TRACE` names, and is a no-op otherwise. It is what found the two bugs
above; a status bar cannot show timing. `scratchpad/run_traced.py` launches the
app with it on, because a detached `Start-Process` does not reliably inherit
the environment and `nohup ... &` inside a tool call dies with its shell.

## What is actually on this LAN

Scanned 2026-09-04 with the app's own discovery, so no protocol guessing is needed:

- `192.168.1.65` **Yamaha RX-V4A**, network name "R&B Room", firmware 1.8, YXC
  api 2.17. Answers RAOP and AirPlay on 7000 with pairing NotNeeded and no
  password (so pyatv drives it directly), a DLNA renderer on 49154, MusicCast
  on 80, and Spotify Connect. Zones main and zone2. On WIRELESS (5 GHz ch 161),
  which is the real buffering variable for a live PCM stream.
  Its AirPlay and UPnP entries share the name "R&B Room", and the merge is
  chromecast, airplay, upnp by setdefault -- so the AirPlay entry wins and the
  UPnP one is dropped. That is the better path and it happens by luck of a name
  collision, not by design. If the names ever diverge, both appear.
- `192.168.1.73` Hisense TV. Chromecast built-in on 8008/8009 AND a DLNA
  renderer at `http://192.168.1.73:38400/upnp/control/mingusavtr`, named
  "RB Room". NOT a Roku TV: nothing on 8060, no answer to `ST: roku:ecp`, and
  VIDAA MQTT 36669 closed. 21 video sink types.
- `192.168.1.101` Samsung 6 Series TV, DLNA renderer on 9197.
- `192.168.1.67` a second Chromecast-built-in device.
- `192.168.1.70` AirPlay device with pairing **Mandatory**. Caster has no pyatv
  pairing flow, so it cannot be cast to and that is not a bug to chase.

## Editing traps

- Working tree is LF, git autocrlf is on. Patch scripts must pass `newline=""` on read AND
  write or the whole file shows as changed.
- ffmpeg on this box: ddagrab hangs, qsv and amf are dead. gdigrab needs the `hwnd=` form
  for a window. Binary can be `ffmpeg.EXE` — match case-insensitively.
- Bash heredocs eat backslashes: `\n` inside one becomes a real newline. Use the Write tool
  or `chr(92)` for anything with backslashes.
Update this with new important information like what is in this file if you notice changes, or something new that should be added here.
