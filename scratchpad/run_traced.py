"""Launch the fixed Caster build with the trace timeline on.

Writes a timestamped timeline to scratchpad/trace_<stamp>.log and prints its
path. Launches the freshly built dist/Caster/Caster.exe (the one with the
no-reconnect fix) rather than whatever else is on the machine.
"""
import os, subprocess, sys, time

here = os.path.dirname(os.path.abspath(__file__))
root = os.path.dirname(here)
exe = os.path.join(root, "dist", "Caster", "Caster.exe")
if not os.path.exists(exe):
    sys.exit(f"build not found: {exe}")

stamp = time.strftime("%Y%m%d_%H%M%S")
trace = os.path.join(here, f"trace_{stamp}.log")

env = dict(os.environ, CASTER_TRACE=trace)
# The trace env var must reach the child; run_traced exists because a detached
# Start-Process does not reliably inherit the environment.
proc = subprocess.Popen([exe], env=env, cwd=os.path.dirname(exe),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(f"PID {proc.pid}  trace: {trace}")
