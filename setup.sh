#!/usr/bin/env bash
# Provision the droplet for the UAA. Non-destructive: if you already have an XFCE
# desktop / VNC session, it leaves them alone and just installs what's missing.
# Run from the repo root:  chmod +x setup.sh && ./setup.sh
set -euo pipefail

GEOMETRY="${UAA_DISPLAY_WIDTH:-1280}x${UAA_DISPLAY_HEIGHT:-800}"
DISP="${UAA_DISPLAY:-:1}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

echo "==> Installing computer-use essentials (xdotool, scrot, imagemagick, venv)"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update
sudo apt-get install -y xdotool scrot imagemagick python3 python3-venv python3-pip x11-utils

echo "==> Ensuring a desktop is available (best-effort; skipped if you already have one)"
sudo apt-get install -y xfce4 xfce4-goodies tightvncserver firefox-esr xterm dbus-x11 || \
  echo "  -> desktop packages not all installed (you may already have a desktop) — continuing"

echo "==> Installing Node.js + npm (most MCP servers are npx-based; best-effort)"
sudo apt-get install -y nodejs npm || \
  echo "  -> node not installed via apt; the agent can install it itself if it needs a Node MCP"

echo "==> Creating Python virtualenv"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "==> Preparing runtime dirs + .env"
mkdir -p state logs mcp
[ -f .env ] || { cp .env.example .env; echo "  -> created .env (FILL IN OPENROUTER_API_KEY)"; }

echo "==> Checking display $DISP"
if DISPLAY="$DISP" xdpyinfo >/dev/null 2>&1; then
  GEO=$(DISPLAY="$DISP" xdotool getdisplaygeometry 2>/dev/null | tr ' ' x || echo '?')
  echo "  -> $DISP is already running (${GEO}). Leaving your session untouched."
  echo "     (the agent auto-detects this geometry — nothing to configure)"
else
  echo "  -> no display on $DISP; starting a TightVNC session at $GEOMETRY"
  mkdir -p "$HOME/.vnc"
  [ -f "$HOME/.vnc/passwd" ] || { echo "  set a VNC password (to connect from your laptop):"; vncpasswd; }
  cat > "$HOME/.vnc/xstartup" <<'XSTARTUP'
#!/bin/sh
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
exec startxfce4
XSTARTUP
  chmod +x "$HOME/.vnc/xstartup"
  vncserver "$DISP" -geometry "$GEOMETRY" -depth 24
fi

cat <<EOF

==> Done.

Next:
  1. Put your key in .env:   OPENROUTER_API_KEY=sk-or-...
     (confirm your Claude model slug at openrouter.ai/models, e.g. anthropic/claude-opus-4.8)
  2. Start the organism:
        ./venv/bin/python daemon.py          # foreground, to watch it
     or install systemd/uaa.service (edit paths first).
  3. Observe / control:  ./venv/bin/python -m uaa.ctl status   (stop | resume | audit | logs)

  Stop it any time:   touch state/STOP      (rm state/STOP to resume)
EOF
