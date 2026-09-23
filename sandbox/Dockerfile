# The sandbox desktop — the discovery driver's "hands and eyes".
#
# A virtual X display with Chromium on it, reachable three ways:
#   - xdotool/scrot      : input and screenshots, driven by the surface agent (Step 3)
#   - Chromium CDP :9222 : READ-ONLY semantic probe, container-internal only
#   - x11vnc -> noVNC    : a human window onto the exact same session (Step 8 handoff)
#
# Built natively for the host architecture (arm64 here) — never pin --platform.
# Build context is ./sandbox, not the repo root: the root .dockerignore excludes sandbox/.

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      # display + viewer
      xvfb \
      x11vnc \
      novnc \
      websockify \
      openbox \
      # the browser under automation
      chromium \
      # the input driver and screenshot tool
      xdotool \
      scrot \
      # xdpyinfo: display readiness gate (used instead of sleeping)
      x11-utils \
      # pgrep for the healthcheck; bookworm-slim ships no procps
      procps \
      # CDP readiness probe, and `docker compose exec sandbox curl` is documented in the plan
      curl \
      # socat: this same image also serves as the host<->internal-network relay
      socat \
      # agent runtime; python3-tk is for Step 16's Tkinter mock (free now, avoids a rebuild)
      python3 \
      python3-venv \
      python3-tk \
      # legible text, and TLS for pip during build
      fonts-dejavu \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Bookworm enforces PEP 668, so the agent gets its own venv rather than --break-system-packages.
# Installed now so Step 3 is code-only: bind-mount surface_agent.py and restart, no rebuild.
RUN python3 -m venv /opt/agent-venv \
 && /opt/agent-venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/agent-venv/bin/pip install --no-cache-dir \
      fastapi \
      "uvicorn[standard]" \
      pillow \
      websockets

RUN useradd --create-home --uid 10001 sandbox

COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh
# The agent and its fixed probe script. Baked in so evidence runs need no bind-mount
# (compose adds a read-only bind mount during development for a faster inner loop).
COPY surface_agent.py probe.js /opt/sandbox/

ENV DISPLAY=:99 \
    SCREEN_GEOMETRY=1280x800x24 \
    PATH="/opt/agent-venv/bin:${PATH}" \
    XDG_RUNTIME_DIR=/tmp/runtime-sandbox \
    PYTHONUNBUFFERED=1

# Non-root is the containment boundary. Chromium's own sandbox is disabled in the
# entrypoint (--no-sandbox) because Docker's default seccomp profile blocks the
# unprivileged user namespaces it needs; see the entrypoint for the full rationale.
USER sandbox
WORKDIR /home/sandbox

EXPOSE 6080 8900

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# The entrypoint brings the desktop up, then runs this as a supervised child — so if
# the agent dies, the container goes down with it rather than looking healthy.
CMD ["uvicorn", "--app-dir", "/opt/sandbox", "surface_agent:app", \
     "--host", "0.0.0.0", "--port", "8900", "--log-level", "info"]
