#!/usr/bin/env python3
"""Docker healthcheck.

ROLE=player has no HTTP surface, so it is healthy by definition here -- the
container is a squeezelite supervisor and its own reconnect loop is the story.

For ROLE=panel, any status code counts as healthy, including 401 (ADMIN_PASSWORD
is set and we have no credentials). Only a refused or timed-out connection means
the process is actually broken.
"""
import os
import sys
import urllib.error
import urllib.request

if os.environ.get("ROLE", "player") != "panel":
    sys.exit(0)

url = "http://127.0.0.1:%s/api/players" % os.environ.get("PORT", "8080")

try:
    urllib.request.urlopen(url, timeout=5)
except urllib.error.HTTPError:
    pass
except Exception as exc:  # URLError, socket.timeout, ...
    print("unhealthy: %s" % exc, file=sys.stderr)
    sys.exit(1)
