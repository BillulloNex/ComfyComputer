#!/bin/bash

echo "[$(date)] Starting Agent Computer background services..."

# Wait for X11 display :1 to be ready
echo "[$(date)] Waiting for X11 display :1 to be ready..."
for i in {1..30}; do
    if xset q >/dev/null 2>&1 || xdpyinfo -display :1 >/dev/null 2>&1; then
        echo "[$(date)] X11 display :1 is ready!"
        break
    fi
    sleep 1
done

export DISPLAY=:1
export HOME=/home/kasm-user

# 1. Launch Google Chrome with remote debugging (CDP) for browser-use
echo "[$(date)] Starting Google Chrome with CDP on port 9222..."
google-chrome-stable \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-gpu \
    --remote-debugging-port=9222 \
    --remote-debugging-address=0.0.0.0 \
    --no-first-run \
    --no-default-browser-check \
    --start-maximized \
    https://cua.ai >/tmp/chrome.log 2>&1 &
CHROME_PID=$!

# 2. Launch Cua Computer Server (MCP / REST / WebSockets) on port 8000
echo "[$(date)] Starting Cua Computer Server on port 8000..."
/usr/bin/python3 -m computer_server --host 0.0.0.0 --port 8000 >/tmp/computer_server.log 2>&1 &
CUA_PID=$!

echo "[$(date)] Background services started: Chrome (PID $CHROME_PID), Cua Server (PID $CUA_PID)"

# 3. Supervisory loop: Kasm expects custom_startup.sh to remain alive indefinitely
while true; do
    if ! kill -0 "$CHROME_PID" 2>/dev/null; then
        echo "[$(date)] Chrome stopped (PID $CHROME_PID). Tail log:"
        tail -n 10 /tmp/chrome.log 2>/dev/null || true
        echo "[$(date)] Restarting Chrome..."
        google-chrome-stable \
            --no-sandbox \
            --disable-dev-shm-usage \
            --disable-gpu \
            --remote-debugging-port=9222 \
            --remote-debugging-address=0.0.0.0 \
            --no-first-run \
            --no-default-browser-check \
            --start-maximized \
            https://cua.ai >/tmp/chrome.log 2>&1 &
        CHROME_PID=$!
    fi
    if ! kill -0 "$CUA_PID" 2>/dev/null; then
        echo "[$(date)] Cua Computer Server stopped (PID $CUA_PID). Tail log:"
        tail -n 10 /tmp/computer_server.log 2>/dev/null || true
        echo "[$(date)] Restarting Cua Server..."
        /usr/bin/python3 -m computer_server --host 0.0.0.0 --port 8000 >/tmp/computer_server.log 2>&1 &
        CUA_PID=$!
    fi
    sleep 5
done

