#!/bin/sh
# Launch TubeView using the bundled venv (creates it on first run).
# Needs mpv, ffmpeg and node from the system (sudo pacman -S mpv ffmpeg nodejs); the app window also uses pyside6.
#   ./run.sh            open in your web browser
#   ./run.sh --window   open in TubeView's own window (what the app-menu entry runs)
cd "$(dirname "$0")"
# a venv breaks when its folder moves (its scripts hard-code the old path): rebuild it then
if ! .venv/bin/yt-dlp --version >/dev/null 2>&1; then
    rm -rf .venv
    python3 -m venv .venv && .venv/bin/pip install -q "yt-dlp[default]"
fi
exec .venv/bin/python tubeview.py "$@"
