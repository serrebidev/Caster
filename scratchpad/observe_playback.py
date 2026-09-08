"""Read-only receiver/relay observation; never logs source URLs."""
import argparse
import collections
import json
from pathlib import Path
import subprocess
import time
import uuid

import psutil
import pychromecast
from pychromecast.models import CastInfo, HostServiceInfo
from zeroconf import Zeroconf
from urllib.request import urlopen
from urllib.error import URLError

p = argparse.ArgumentParser()
p.add_argument('--root', required=True)
p.add_argument('--port', required=True, type=int)
p.add_argument('--host', required=True)
p.add_argument('--seconds', type=int, default=90)
a = p.parse_args()
root = Path(a.root)
seen = set()
hashes = {}
zc = Zeroconf()
info = CastInfo(services={HostServiceInfo(a.host, 8009)}, uuid=uuid.uuid4(),
                model_name='', friendly_name='Observer', host=a.host,
                port=8009, cast_type='cast', manufacturer='')
cast = pychromecast.Chromecast(info, zconf=zc)
try:
    cast.wait(10)
    start = time.monotonic()
    while time.monotonic() - start < a.seconds:
        cast.media_controller.update_status()
        try:
            body = urlopen(f'http://127.0.0.1:{a.port}/live.m3u8', timeout=3).read().decode()
        except (URLError, OSError):
            print('Relay closed or changed; observation ended.', flush=True)
            break
        durations = [float(l.split(':')[1].rstrip(',')) for l in body.splitlines() if l.startswith('#EXTINF:')]
        seq = next(l.split(':')[1] for l in body.splitlines() if l.startswith('#EXT-X-MEDIA-SEQUENCE:'))
        target = next(l.split(':')[1] for l in body.splitlines() if l.startswith('#EXT-X-TARGETDURATION:'))
        overlap = []
        for name in (l for l in body.splitlines() if l.startswith('seg')):
            if name in seen:
                continue
            seen.add(name)
            probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                '-show_packets', '-show_entries', 'packet=data_hash', '-show_data_hash', 'sha256',
                '-of', 'json', str(root / name)], capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW, timeout=4)
            matches = collections.Counter()
            for packet in json.loads(probe.stdout or b'{}').get('packets', []):
                value = packet.get('data_hash')
                if value in hashes:
                    matches[hashes[value]] += 1
                elif value:
                    hashes[value] = name
            if sum(matches.values()) > 10:
                overlap.append([name, matches.most_common(2)])
        status = cast.media_controller.status
        print(json.dumps(dict(t=round(time.monotonic()-start, 1), state=status.player_state,
            position=status.current_time, session=status.media_session_id,
            seq=seq, target=target, seconds=round(sum(durations), 2),
            encoder=[x.pid for x in psutil.process_iter(['name']) if x.info['name'].lower()=='ffmpeg.exe'],
            repeated_packets=overlap)), flush=True)
        time.sleep(2)
finally:
    cast.disconnect()
    zc.close()
