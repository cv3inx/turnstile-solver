#!/bin/bash
# Camoufox manages its own Xvfb when headless='virtual', so this
# entrypoint just hands off to python. Any stale X locks inside the
# container filesystem are cleared so Camoufox can pick a fresh display.
set -e

rm -f /tmp/.X*-lock /tmp/.X11-unix/X* 2>/dev/null || true

exec python3 service.py
