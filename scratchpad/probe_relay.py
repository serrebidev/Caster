import sys, time, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from caster import HlsRelay
r = HlsRelay(sys.argv[1], codecs=['h264', 'aac'], live=True, trail_keep=8)
try:
    t = time.monotonic()
    r.start()
    print('primed', round(time.monotonic()-t, 2), flush=True)
    for i in range(45):
        data = r.trailing_playlist().decode()
        ds = [float(x.split(':')[1].split(',')[0]) for x in data.splitlines() if x.startswith('#EXTINF:')]
        proc = r.proc
        print(round(time.monotonic()-t, 1), 'exit', proc.poll() if proc else 'restarting', 'restarts', r._restarted, 'newest', r._newest_seg_number(), 'durations', ds, flush=True)
        time.sleep(2)
finally:
    r.stop()
