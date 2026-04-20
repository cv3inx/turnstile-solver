#!/bin/bash
# Start Xvfb first, wait for it to answer on the socket, then launch service.
set -e

DISPLAY_NUM="${DISPLAY:-:99}"
Xvfb "$DISPLAY_NUM" -screen 0 1280x900x24 -ac +extension GLX +render -noreset >/dev/null 2>&1 &
XVFB_PID=$!

# Wait up to 10s for the Xvfb socket
for i in $(seq 1 40); do
    if [ -S "/tmp/.X11-unix/X${DISPLAY_NUM#:}" ]; then
        break
    fi
    sleep 0.25
done

cleanup() {
    kill "$XVFB_PID" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

export DISPLAY="$DISPLAY_NUM"
exec python3 service.py
