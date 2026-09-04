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
- `caster_devices.py` — Sonos, Roku, Kodi. No caster imports. Keep it that way, no cycle
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

## Protocol traps

- Sonos answers SSDP as ZonePlayer, not MediaRenderer. Needs own discovery via soco.
- Sonos AirPlay 2 = trap. Wants MFi hardware auth, Python cannot do it, speaker refuses
  the audio port. Filter with `looks_like_sonos()`.
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

## Capture

`CONTAINERS`: mp4 -> Chromecast and Roku, mpegts -> DLNA/TV/Kodi, wav -> audio boxes.
Sonos and AirPlay always get wav — RAOP carries no video, Sonos is speakers.

wav + `pcm_is_directly_usable()` = no ffmpeg in the path at all. Lowest latency route.
AudioTap = one WASAPI loopback, many subscribers, bounded queue, drop oldest chunk. Never
let a queue build a backlog — backlog is heard as lag.

Mixed selection (Cast + DLNA) = one ScreenSource per container, two encoders.

`_no_window_kwargs()` on every subprocess or a console flashes on screen.
`_find_ffmpeg()` order: beside frozen exe, PATH, winget glob.

## Known bugs — open as of v1.1.2

Confirmed by running the code:

1. `HlsRelay.stop()` never calls `httpd.shutdown()` / `server_close()`. Port stays open and
   the thread stays alive for the life of the app, once per relay. `FileServer.stop()` and
   `ScreenSource.stop()` do it right — copy those.
2. `HlsRelay.start()` raises "ffmpeg exited early" without calling `self.stop()`. Leaks the
   server, its thread, and the temp dir `%TEMP%\caster_hls_*`.
3. `LoopThread.submit()` returns None, declared `-> None`. So `self._runner_fut` is always
   None. `stop_silent()`'s `rf.cancel()` is dead and `_on_close()`'s `fut.result(timeout=8)`
   never waits — app closes without letting AirPlay tear down. Return the future.
4. `HlsFileHandler.relay_requests` unbounded. Appended on every GET, cleared only when a new
   Cast relay starts. Long IPTV cast grows it forever.

By inspection, not reproduced:

5. `zeroconf.Zeroconf()` made fresh per Chromecast connect (caster.py ~1497), never closed.
   `stop_silent()` drops `self.cast` without `disconnect()`. Leaks per play.
6. `_play_upnp` worker sets `self._relay = relay`. Two UPnP renderers + one TS URL = second
   overwrites first, first ffmpeg and its server orphaned.
7. `_cast_capture` worker sets `self._sources = started` AFTER the last `_stop_flag` check.
   Stop pressed in that gap = capture keeps running with no handle to stop it.
8. `_air_shutdown` event is created and never set anywhere. Stop during AirPlay falls into
   the "stream ended naturally" branch and announces "Finished." after `stop()` already
   said "Stopped." Wrong word spoken.
9. `HotkeyManager.register_all()` returns the actions that failed. caster.py ignores the
   return, so a hotkey another app already owns is lost silently.
10. `AudioTap.start()` leaves `self._thread` set after a failed start, so a retry returns
    immediately and the caller believes capture is live. Note: an unknown device name does
    NOT fail — `_pick_loopback_device` falls back to the default on purpose.

## Editing traps

- Working tree is LF, git autocrlf is on. Patch scripts must pass `newline=""` on read AND
  write or the whole file shows as changed.
- ffmpeg on this box: ddagrab hangs, qsv and amf are dead. gdigrab needs the `hwnd=` form
  for a window. Binary can be `ffmpeg.EXE` — match case-insensitively.
- Bash heredocs eat backslashes: `\n` inside one becomes a real newline. Use the Write tool
  or `chr(92)` for anything with backslashes.
Update this with new important information like what is in this file if you notice changes, or something new that should be added here.
