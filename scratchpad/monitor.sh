#!/bin/bash
# caster relay live watch — appended timeline for debugging
LOG="C:/Users/admin/git/Caster/scratchpad/trace.log"
mkdir -p "$(dirname "$LOG")"
: >> "$LOG"
while true; do
  ts=$(date +%H:%M:%S)
  d=$(ls -dt "$LOCALAPPDATA/Temp"/caster_hls_* 2>/dev/null | head -1)
  if [ -n "$d" ]; then
    n=$(ls "$d" 2>/dev/null | grep -c '\.ts$' || true)
    m="$d/live.m3u8"
    if [ -f "$m" ]; then
      age=$(( $(date +%s) - $(stat -c %Y "$m") ))
      last=$(grep '\.ts$' "$m" | tail -1 || true)
      echo "$ts dir=$(basename "$d") segs=$n last=$last playlist_age=${age}s" >> "$LOG"
    else
      echo "$ts dir=$(basename "$d") segs=$n no_playlist_yet" >> "$LOG"
    fi
  else
    echo "$ts no_relay_dir" >> "$LOG"
  fi
  pids=$(tasklist /FI "IMAGENAME eq ffmpeg.exe" /FO CSV 2>/dev/null | tail -n +2 | cut -d'"' -f4 | tr '\n' ',' || true)
  echo "$ts ffmpeg_pids=$pids" >> "$LOG"
  sleep 12
done
