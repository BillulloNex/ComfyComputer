#!/bin/bash
set -e

LOGFILE="/home/kasm-user/startup.log"
exec > >(tee -a "$LOGFILE") 2>&1

echo "[$(date)] Starting Agent Computer background services..."

# Wait for X11 display to be ready
echo "[$(date)] Waiting for X11 display :1 to be ready..."
for i in {1..40}; do
    if xset q >/dev/null 2>&1 || xdpyinfo -display :1 >/dev/null 2>&1; then
        echo "[$(date)] X11 display :1 is ready!"
        break
    fi
    sleep 1
done

export DISPLAY=:1
export HOME=/home/kasm-user

# Launch Google Chrome with remote debugging (CDP) for browser-use
echo "[$(date)] Starting Google Chrome with CDP on port 9222..."
google-chrome-stable \
    --remote-debugging-port=9222 \
    --remote-debugging-address=0.0.0.0 \
    --no-first-run \
    --no-default-browser-check \
    --disable-dev-shm-usage \
    --start-maximized \
    https://cua.ai &

# Launch Cua Computer Server (MCP / REST / WebSockets) on port 8000
echo "[$(date)] Starting Cua Computer Server on port 8000..."
/usr/bin/python3 -m computer_server --host 0.0.0.0 --port 8000 &

echo "[$(date)] All Agent Computer services started successfully."
