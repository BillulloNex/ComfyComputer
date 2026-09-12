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

# 1. Launch Google Chrome on localhost:9223 (with --remote-allow-origins=*)
echo "[$(date)] Starting Google Chrome on port 9223..."
mkdir -p /home/kasm-user/.config/google-chrome-cdp
google-chrome-stable \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-gpu \
    --remote-debugging-port=9223 \
    --remote-allow-origins="*" \
    --user-data-dir=/home/kasm-user/.config/google-chrome-cdp \
    --no-first-run \
    --no-default-browser-check \
    --restore-last-session \
    --start-maximized \
    https://cua.ai &
CHROME_PID=$!

# 2. Launch cdp_proxy on 0.0.0.0:9222 -> 127.0.0.1:9223 (translates host headers & WebSocket URLs)
echo "[$(date)] Starting CDP Proxy on 0.0.0.0:9222 -> 127.0.0.1:9223..."
/usr/bin/python3 /dockerstartup/cdp_proxy.py &
CDP_PID=$!

# 3. Launch Cua Computer Server on 0.0.0.0:8000 (direct logging to console)
echo "[$(date)] Starting Cua Computer Server on 0.0.0.0:8000..."
/usr/bin/python3 -m computer_server --host 0.0.0.0 --port 8000 &
CUA_PID=$!

echo "[$(date)] Background services launched: Chrome (PID $CHROME_PID), CDP Proxy (PID $CDP_PID), Cua Server (PID $CUA_PID)"

# Wait for services to bind and run initial local self-tests
sleep 10
echo "[$(date)] Testing local Chrome CDP (port 9222)..."
curl -s http://127.0.0.1:9222/json/version || echo "Local CDP curl failed"
echo "[$(date)] Testing local Cua Server (port 8000)..."
curl -s http://127.0.0.1:8000/status || echo "Local Cua Server curl failed"

# 4. Supervisory loop: keep custom_startup.sh running indefinitely and auto-restart failed services
while true; do
    if ! kill -0 "$CHROME_PID" 2>/dev/null; then
        echo "[$(date)] Chrome stopped, restarting on port 9223..."
        google-chrome-stable \
            --no-sandbox \
            --disable-dev-shm-usage \
            --disable-gpu \
            --remote-debugging-port=9223 \
            --remote-allow-origins="*" \
            --user-data-dir=/home/kasm-user/.config/google-chrome-cdp \
            --no-first-run \
            --no-default-browser-check \
            --start-maximized \
            https://cua.ai &
        CHROME_PID=$!
    fi
    if ! kill -0 "$CDP_PID" 2>/dev/null; then
        echo "[$(date)] CDP Proxy stopped, restarting on port 9222..."
        /usr/bin/python3 /dockerstartup/cdp_proxy.py &
        CDP_PID=$!
    fi
    if ! kill -0 "$CUA_PID" 2>/dev/null; then
        echo "[$(date)] Cua Computer Server stopped, restarting on port 8000..."
        /usr/bin/python3 -m computer_server --host 0.0.0.0 --port 8000 &
        CUA_PID=$!
    fi
    sleep 5
done


