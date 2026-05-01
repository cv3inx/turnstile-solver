#!/bin/bash
# Clean any stale X lock/socket from a prior container lifetime (same
# volumes), start Xvfb, wait for the socket, then hand off to python.
set -e

DISPLAY_NUM="${DISPLAY:-:99}"
N="${DISPLAY_NUM#:}"
rm -f "/tmp/.X${N}-lock" "/tmp/.X11-unix/X${N}"

Xvfb "$DISPLAY_NUM" -screen 0 1280x900x24 -ac +extension GLX +render -noreset >/dev/null 2>&1 &
XVFB_PID=$!

for i in $(seq 1 40); do
    if [ -S "/tmp/.X11-unix/X${N}" ] && kill -0 "$XVFB_PID" 2>/dev/null; then
        break
    fi
    sleep 0.25
done

if ! kill -0 "$XVFB_PID" 2>/dev/null; then
    echo "entrypoint: Xvfb failed to start on $DISPLAY_NUM" >&2
    exit 1
fi

cleanup() {
    kill "$XVFB_PID" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

export DISPLAY="$DISPLAY_NUM"
exec python3 service.py
