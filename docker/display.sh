#!/bin/bash
set -euo pipefail
umask 077
export DISPLAY=:99
pids=()
cleanup() {
    trap - EXIT INT TERM
    # Stop the application/browser before Xvfb. Losing its display first makes
    # Chromium crash before it can flush cookies to its persistent profile.
    for ((index=${#pids[@]}-1; index>=0; index--)); do
        kill "${pids[index]}" 2>/dev/null || true
        wait "${pids[index]}" 2>/dev/null || true
    done
}
trap cleanup EXIT INT TERM
Xvfb "$DISPLAY" -screen 0 1280x800x24 -nolisten tcp &
pids+=("$!")
for attempt in {1..50}; do
    [ -S /tmp/.X11-unix/X99 ] && break
    sleep 0.1
done
test -S /tmp/.X11-unix/X99
fluxbox >/tmp/fluxbox.log 2>&1 &
pids+=("$!")
# Only the private Docker network reaches VNC; job-bless authenticates viewers.
x11vnc -display "$DISPLAY" -listen 0.0.0.0 -rfbport 5900 -nopw -forever -shared -xkb -noxdamage >/tmp/vnc.log 2>&1 &
pids+=("$!")
"$@" &
pids+=("$!")
# Any essential process exiting makes the container restart as a unit.
wait -n "${pids[@]}" || true
exit 1
