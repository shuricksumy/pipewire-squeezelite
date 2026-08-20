#!/usr/bin/env python3
"""Stand-in for squeezelite: prints its arguments, then behaves as told.

FAKE_SQUEEZELITE_MODE:
  run    stay alive until terminated (default)
  crash  exit non-zero immediately, to exercise the restart backoff
"""
import os
import signal
import sys
import time

print("squeezelite args: %s" % " ".join(sys.argv[1:]), flush=True)
print("PIPEWIRE_NODE=%s" % os.environ.get("PIPEWIRE_NODE", ""), flush=True)
print("PIPEWIRE_LATENCY=%s" % os.environ.get("PIPEWIRE_LATENCY", ""), flush=True)

if os.environ.get("FAKE_SQUEEZELITE_MODE") == "crash":
    print("simulated failure", flush=True)
    sys.exit(3)

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(0.2)
