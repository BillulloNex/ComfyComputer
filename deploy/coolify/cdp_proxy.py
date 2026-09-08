"""CDP Reverse Proxy & WebSocket Tunnel for Remote Browser Automation."""

import asyncio
import logging
import os
import re
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
import httpx
import uvicorn
import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cdp_proxy")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

CHROME_HTTP = "http://127.0.0.1:9223"
CHROME_WS = "ws://127.0.0.1:9223"
DEFAULT_PUBLIC_HOST = os.environ.get("CDP_PUBLIC_HOST", "cdp-computer.beenex.cloud")


@app.websocket("/devtools/{tail:path}")
async def proxy_websocket(websocket: WebSocket, tail: str):
    await websocket.accept()
    target_url = f"{CHROME_WS}/devtools/{tail}"
    try:
        async with websockets.connect(target_url, max_size=200 * 1024 * 1024) as target_ws:
            async def client_to_target():
                try:
                    while True:
                        msg = await websocket.receive()
                        if "text" in msg:
                            await target_ws.send(msg["text"])
                        elif "bytes" in msg:
                            await target_ws.send(msg["bytes"])
                        elif msg.get("type") == "websocket.disconnect":
                            break
                except (WebSocketDisconnect, asyncio.CancelledError):
                    pass

            async def target_to_client():
                try:
                    async for msg in target_ws:
                        if isinstance(msg, str):
                            await websocket.send_text(msg)
                        else:
                            await websocket.send_bytes(msg)
                except (WebSocketDisconnect, asyncio.CancelledError):
                    pass

            c2t = asyncio.create_task(client_to_target())
            t2c = asyncio.create_task(target_to_client())
            done, pending = await asyncio.wait([c2t, t2c], return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except Exception as e:
        logger.warning(f"CDP WebSocket error: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
async def proxy_http(request: Request, path: str):
    url = f"{CHROME_HTTP}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    proto = request.headers.get("x-forwarded-proto", "https")
    ws_scheme = "wss" if proto == "https" else "ws"

    # Resolve public host
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or DEFAULT_PUBLIC_HOST
    )
    # If host is an internal IP or localhost, prefer DEFAULT_PUBLIC_HOST
    if (
        host.startswith("172.")
        or host.startswith("10.")
        or host.startswith("127.")
        or host.startswith("localhost")
    ):
        host = DEFAULT_PUBLIC_HOST

    async with httpx.AsyncClient() as client:
        resp = await client.request(
            method=request.method,
            url=url,
            headers={
                "Host": "localhost:9223",
                "User-Agent": request.headers.get("user-agent", "Mozilla/5.0"),
            },
            timeout=15.0,
        )
        content = resp.content
        if "application/json" in resp.headers.get("content-type", "") or path.startswith("json"):
            text = resp.text
            # Rewrite any ws://<internal-ip-or-host>/devtools/ to wss://<public-host>/devtools/
            text = re.sub(r"ws://[0-9a-zA-Z.:]+/(devtools/)", f"{ws_scheme}://{host}/\\1", text)
            content = text.encode("utf-8")

        response_headers = dict(resp.headers)
        response_headers.pop("content-length", None)
        response_headers.pop("content-encoding", None)

        return Response(
            content=content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type"),
            headers=response_headers,
        )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9222, log_level="info")
