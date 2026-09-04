# Caster

A vibe-coded, screen-reader-friendly desktop caster for Windows. Send a file, a URL, your screen, one app window, or just this PC's sound to whatever is on your network — Chromecast, AirPlay, UPnP/DLNA, Sonos, Roku, or Kodi.

[![Join SerrebiProjects on Telegram](https://img.shields.io/badge/Telegram-SerrebiProjects-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/SerrebiProjects)

**Have a question, hit a bug, or want early word on new releases?** Join the [SerrebiProjects Telegram group](https://t.me/SerrebiProjects) — the community hub for Caster and my other projects, and the fastest place to get help.

## Features

- Casts local media files, http(s) URLs, live IPTV channels, and YouTube links.
- Casts your whole screen, a single app window, or this PC's sound on its own.
- Speaks six protocols: Chromecast, AirPlay (RAOP), UPnP/DLNA, Sonos, Roku, and Kodi.
- Multi-room: tick several devices and they all play at once.
- Groups Sonos speakers and MusicCast zones so the receiver keeps them in step, rather than sending each its own copy and letting them drift.
- Finds devices on all six protocols at once — a full scan takes about five seconds, not half a minute.
- Reads what a UPnP renderer says it accepts, so a stereo amplifier is never sent a picture it will refuse.
- Yamaha MusicCast support: turns the receiver on to cast, selects the right input, puts it back on the input it was showing when you stop, and controls volume on the receiver's own scale in real dB rather than a guessed percentage.
- Remuxes MPEG-TS to HLS for receivers that will not take it raw, and transcodes only when the video codec leaves no choice.
- Sends system audio as uncompressed PCM with no encoder in the path at all, which is as close to realtime as this gets.
- Quality presets that trade delay against tolerance for a stuttering source, from lowest delay to best picture.
- Microphone mixing, so you can narrate over whatever you are casting.
- Audio/video offset control for receivers that run sound ahead of picture.
- Six system-wide hotkeys, so casting never requires finding the window.
- Sleep timer, tray controls, favourites, and a recent-URLs list.
- Speaks status through NVDA directly, rather than leaving it in a status bar nobody is reading.

## Accessibility

Caster is built to be used without seeing it. Every control is created behind a real label so Windows gives it the right accessible name, the device list is a checklist rather than a multi-select, and progress chatter is deliberately kept out of the speech stream so the line that matters is not talked over.

If something announces wrongly or reads awkwardly, that is a bug — please report it.

## Download and install

Grab the latest build from the [Releases page](https://github.com/serrebidev/Caster/releases).

**Windows portable**

1. Download `Caster-portable.zip`.
2. Extract it anywhere and run `Caster.exe` — no installation required.

`ffmpeg.exe` ships beside the executable, so there is nothing else to install.

Settings live in `%APPDATA%\Caster\settings.json`. Deleting the folder you extracted leaves them untouched.

## Run from source

1. Install Python 3.14.
2. Install dependencies: `pip install -r requirements.txt`
3. Make sure `ffmpeg` is on your PATH.
4. Launch it: `python caster.py`

`soco` is optional — without it every other protocol still works and Sonos speakers simply do not appear.

## Hotkeys

| Keys | Action |
| --- | --- |
| Ctrl+Alt+S | Cast the screen |
| Ctrl+Alt+A | Cast this PC's sound |
| Ctrl+Alt+X | Stop casting |
| Ctrl+Alt+M | Mute or unmute |
| Ctrl+Alt+Up / Down | Volume up or down |

These are system-wide. If another program already owns one, Caster says so in the status line rather than losing it silently.

## Diagnostic trace

Set `CASTER_TRACE` to a file path before launching and Caster writes a timeline there — every probe, every encoder, every state the receiver reported, with timestamps. Buffering is a timing problem and timing problems are invisible from a status bar. With the variable unset, nothing is written and nothing is measured.

## A note on IPTV sources

Live IPTV servers vary enormously. Some hand over a clean stream; some close the connection every ten seconds and re-send what they already sent. Caster now declares those sources unseekable so a reconnect cannot ask to resume at a byte offset a live stream does not have — which is what used to make playback jump backwards. A channel whose keyframes are eight seconds apart will still take longer to start than one with two-second keyframes, because a Chromecast will not begin until it holds three segments, and a segment cannot be shorter than the gap between keyframes.

## Contributing

Pull requests are welcome. If Caster has been useful to you, open a PR with a fix or feature and I'll review it.

## License

Caster is under the [MIT license](LICENSE) — use it, change it, redistribute it, or package it for a distro repository, no permission needed. Every source file carries an `SPDX-License-Identifier: MIT` header so packaging tools can pick the license up automatically.

## Community and support

Report bugs and request features in [Issues](https://github.com/serrebidev/Caster/issues). For questions, feedback, and release news, join the [SerrebiProjects Telegram group](https://t.me/SerrebiProjects).
