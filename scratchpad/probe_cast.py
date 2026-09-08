"""Monitor a real Cast load and relay without opening the GUI."""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ['CASTER_TRACE'] = str(Path(__file__).with_suffix('.trace.log'))
import caster

frame = caster.MainFrame.__new__(caster.MainFrame)
frame._stop_flag = False
frame._cast_live_loads = {}
frame._cast_recovering = set()
frame._ui = lambda fn, *args, **kwargs: fn(*args, **kwargs)
frame.set_status = lambda text: print(text, flush=True)
zc = caster.zeroconf.Zeroconf()
ci = caster.CastInfo(
    uuid=caster.uuidlib.UUID(sys.argv[3]), host=sys.argv[2], port=8009,
    cast_type=caster.CAST_TYPE_CHROMECAST, manufacturer='',
    model_name='SmartTV 4K FFM', friendly_name='RB Room',
    services={caster.HostServiceInfo(sys.argv[2], 8009)})
cast = caster.pychromecast.Chromecast(ci, zconf=zc, tries=3, timeout=15)
relay = caster.HlsRelay(sys.argv[1], codecs=['h264', 'aac'], live=True,
                        hls_time=2, trail_keep=8)
started = time.monotonic()
log = open(Path(__file__).with_suffix('.jsonl'), 'w', encoding='utf-8')
try:
    cast.wait(20)
    print('connected', cast.app_id, flush=True)
    frame._ensure_receiver(cast)
    url = sys.argv[1] if '--native' in sys.argv else relay.start()
    mc = cast.media_controller
    before = mc.status.media_session_id
    mc.play_media(url, 'application/vnd.apple.mpegurl', stream_type='LIVE')
    mc.block_until_active(15)
    print('settled', frame._await_playing(mc, before), flush=True)
    frame._cast_live_loads['RB Room (Cast)'] = (
        cast, url, 'application/vnd.apple.mpegurl', 'LIVE')
    for iteration in range(90):
        if '--faults' in sys.argv and iteration == 20:
            print('FAULT: terminating this test encoder', flush=True)
            relay.proc.terminate()
        if '--faults' in sys.argv and iteration == 45:
            print('FAULT: stopping this test media session', flush=True)
            mc.stop()
        mc.update_status()
        proc = relay.proc
        status = mc.status
        row = dict(seconds=round(time.monotonic()-started, 2),
                   state=status.player_state, reason=status.idle_reason,
                   position=status.current_time, session=status.media_session_id,
                   restarts=relay._restarted,
                   exit=proc.poll() if proc else None,
                   playlist=(relay.trailing_playlist() or b'').decode(),
                   requests=list(caster.HlsFileHandler.relay_requests)[-6:])
        log.write(json.dumps(row) + '\n')
        log.flush()
        print({k:v for k,v in row.items() if k not in ('playlist', 'requests')}, flush=True)
        frame._recover_live_casts()
        time.sleep(2)
finally:
    frame._stop_flag = True
    frame._cast_live_loads.clear()
    try:
        cast.media_controller.stop()
    finally:
        cast.disconnect()
        relay.stop()
        zc.close()
        log.close()
