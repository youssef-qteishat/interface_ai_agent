#!/usr/bin/env bash
#
# Brings up the sandbox desktop in dependency order, then supervises it.
#
# Two rules this file exists to enforce:
#   1. Readiness is polled, never slept for. A fixed sleep either wastes time or
#      races, and both failures look like "the driver is broken" later.
#   2. Death is loud. The first supervised process to exit takes the container with
#      it, so `docker compose ps` shows a problem instead of a healthy-looking
#      container with a dead browser inside.

set -euo pipefail

DISPLAY_NUM="${DISPLAY_NUM:-99}"
export DISPLAY=":${DISPLAY_NUM}"
SCREEN_GEOMETRY="${SCREEN_GEOMETRY:-1280x800x24}"
TARGET_URL="${TARGET_URL:?TARGET_URL must be set (e.g. http://bank-sim:8001/)}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
CDP_PORT="${CDP_PORT:-9222}"

log() { printf '[entrypoint] %s\n' "$*"; }

# Poll a command until it succeeds. wait_for <seconds> <label> <cmd...>
wait_for() {
  local deadline_s="$1" label="$2"; shift 2
  local attempts=$(( deadline_s * 5 ))
  for _ in $(seq 1 "$attempts"); do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  log "FATAL: ${label} did not become ready within ${deadline_s}s"
  return 1
}

mkdir -p "${XDG_RUNTIME_DIR:-/tmp/runtime-sandbox}"
chmod 700 "${XDG_RUNTIME_DIR:-/tmp/runtime-sandbox}"

# 0. Clear a stale X lock. If the container was killed hard (daemon crash, OOM,
# `docker kill`), /tmp/.X99-lock survives in the container's filesystem and Xvfb
# refuses to start forever after with "Server is already active for display 99".
# Only remove it when no X server actually answers on that display.
if [ -e "/tmp/.X${DISPLAY_NUM}-lock" ] && ! xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
  log "removing stale X lock for ${DISPLAY} (no server is answering)"
  rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}" || true
fi

# 1. Virtual display. -nolisten tcp: X is reachable only via the local socket.
log "starting Xvfb on ${DISPLAY} at ${SCREEN_GEOMETRY}"
Xvfb "${DISPLAY}" -screen 0 "${SCREEN_GEOMETRY}" -nolisten tcp &
wait_for 10 "Xvfb ${DISPLAY}" xdpyinfo -display "${DISPLAY}"
log "display ready: $(xdotool getdisplaygeometry | tr '\n' 'x' | sed 's/x$//')"

# 2. Window manager. Without one, X input focus falls back to PointerRoot and typed
# keys go wherever the pointer happens to be — fine until it isn't. openbox also
# honours Chromium's kiosk/fullscreen request, which is what pins the client area to
# the full display and keeps coordinates stable across runs.
log "starting openbox"
openbox --sm-disable &

# 3. VNC on loopback only. Port 5900 is never published; the only way in is noVNC.
log "starting x11vnc on 127.0.0.1:${VNC_PORT}"
x11vnc -display "${DISPLAY}" -forever -shared -nopw -localhost \
       -rfbport "${VNC_PORT}" -noxdamage -quiet &
wait_for 10 "x11vnc" bash -c "exec 3<>/dev/tcp/127.0.0.1/${VNC_PORT}"

# 4. noVNC: serves the viewer and bridges websocket -> VNC.
log "starting noVNC on :${NOVNC_PORT}"
websockify --web=/usr/share/novnc "${NOVNC_PORT}" "127.0.0.1:${VNC_PORT}" &

# 5. The browser.
#
# --no-sandbox is required, not a shortcut: Docker's default seccomp profile blocks
# the unprivileged user-namespace creation Chromium's zygote sandbox needs, so it
# cannot initialize regardless of which user runs it. The alternatives that would let
# it work (seccomp=unconfined, or cap_add SYS_ADMIN) weaken the *container* boundary
# much more than disabling Chromium's inner one. Containment here is: non-root uid
# 10001, an internal network with no egress, and the policy engine.
#
# Not using --disable-dev-shm-usage on purpose: compose grants a real 1 GB /dev/shm,
# which is the better fix for the same crash.
#
# The --disable-background-networking family matters more than usual on a no-egress
# network, where telemetry and update checks become hanging connections.
#
# --log-level=3 (FATAL only) exists to keep `docker compose logs sandbox` readable:
# with no system D-Bus in the container, Chromium emits a continuous stream of
# dbus/upower connection ERRORs that bury this script's own lines. A session bus via
# dbus-run-session would silence only some of them, and a system bus needs root. When
# debugging an actual Chromium problem, drop this flag temporarily to see its errors.
log "starting chromium against ${TARGET_URL}"
chromium \
  --kiosk --app="${TARGET_URL}" \
  --window-size=1280,800 \
  --window-position=0,0 \
  --user-data-dir="${HOME}/chrome-profile" \
  --remote-debugging-port="${CDP_PORT}" \
  --remote-debugging-address=127.0.0.1 \
  --remote-allow-origins='*' \
  --no-sandbox \
  --disable-gpu \
  --no-first-run \
  --no-default-browser-check \
  --disable-infobars \
  --disable-features=Translate,DefaultBrowserSettingEnabled,AutofillServerCommunication \
  --password-store=basic \
  --use-mock-keychain \
  --disable-background-networking \
  --disable-component-update \
  --disable-sync \
  --metrics-recording-only \
  --log-level=3 &

wait_for 20 "chromium CDP on ${CDP_PORT}" curl -sf "http://127.0.0.1:${CDP_PORT}/json/version"
log "chromium ready: $(curl -s "http://127.0.0.1:${CDP_PORT}/json/version" | head -c 200)"

# 6. Hand off to CMD (Step 3: the surface agent; until then, sleep infinity).
if [ "$#" -gt 0 ]; then
  log "starting supervised command: $*"
  "$@" &
fi

log "sandbox up — noVNC on ${NOVNC_PORT}, CDP on ${CDP_PORT} (container-internal)"

# First death wins: report it and take the container down.
wait -n || true
log "a supervised process exited — shutting the sandbox down so the failure is visible"
exit 1
