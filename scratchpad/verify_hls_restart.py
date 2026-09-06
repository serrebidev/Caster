"""Exercise real ffmpeg append_list and Caster's served timeline offline."""
import os
import sys
import tempfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from caster import HlsRelay, _find_ffmpeg, _no_window_kwargs


def entries(data):
    seq = 0
    timeline = 0
    result = {}
    for line in data.decode().splitlines():
        if line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
            seq = int(line.split(':')[1])
        elif line.startswith('#EXT-X-DISCONTINUITY-SEQUENCE:'):
            timeline = int(line.split(':')[1])
        elif line == '#EXT-X-DISCONTINUITY':
            timeline += 1
        elif line and not line.startswith('#'):
            result[line] = (seq, timeline)
            seq += 1
    return result


with tempfile.TemporaryDirectory(prefix='caster_restart_check_') as root:
    source = os.path.join(root, 'source.ts')
    subprocess.run([_find_ffmpeg(), '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=160x90:rate=10', '-t', '5', '-c:v',
                    'libx264', '-g', '10', '-f', 'mpegts', source],
                   check=True, **_no_window_kwargs())
    relay = HlsRelay(source, hls_time=1, codecs=['h264'])
    relay.root = root
    playlist = os.path.join(root, 'live.m3u8')
    previous = {}
    for restart in range(5):
        subprocess.run(relay._ffmpeg_cmd(playlist, relay._next_segment_number()),
                       check=True, **_no_window_kwargs())
        current = entries(relay.trailing_playlist())
        common = previous.keys() & current.keys()
        if previous:
            assert common, 'No overlapping segments to verify'
        for uri in common:
            assert previous[uri] == current[uri], (uri, previous[uri], current[uri])
        previous = current
    print('PASS: five real ffmpeg runs preserve segment and discontinuity IDs')
