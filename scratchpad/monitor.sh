#!/bin/bash
# Watch the newest caster_hls_* relay dir: segment count, playlist age,
# ffmpeg pid. One line every 15s, forever. Kill with: pkill -f monitor.sh
while true; do
  ts=$(date +%H:%M:%S)
  d=$(ls -dt "$LOCALAPPDATA/Temp"/caster_hls_* 2>/dev/null | head -1)
  if [ -n "$d" ]; then
    n=$(ls "$d" 2>/dev/null | grep -c '\.ts$')
    m="$d/live.m3u8"
    if [ -f "$m" ]; then
      age=$(( $(date +%s) - $(stat -c %Y "$m") ))
      last=$(grep '\.ts$' "$m" | tail -1)
      echo "$ts dir=$(basename "$d") segs=$n last=$last playlist_age=${age}s"
    else
      echo "$ts dir=$(basename "$d") segs=$n no_playlist_yet"
    fi
  else
    echo "$ts no_relay_dir"
  fi
  ffpid=$(tasklist /FI "IMAGENAME eq ffmpeg.exe" /FO CSV 2>/dev/null | tail -n +2 | cut -d'"' -f4 | tr '\n' ',')
  echo "$ts ffmpeg_pids=$ffpid"
  sleep 15
done
