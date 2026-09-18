"""Computer on Demand — Orchestrator API.

FastAPI service that manages on-demand desktop computers for AI models.
Each computer is a Docker container backed by a persistent named volume.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware

from config import Config
from models import (
    ComputerListResponse,
    ComputerResponse,
    ComputerStatus,
    CreateComputerRequest,
    Endpoints,
    ErrorResponse,
    HealthResponse,
    RestoreRequest,
    SnapshotListResponse,
    SnapshotResponse,
)
import db
import docker_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("orchestrator")


# ---------------------------------------------------------------------------
# Auth: fail-closed bearer key, /health stays open for Coolify healthchecks
# ---------------------------------------------------------------------------

_OPEN_PATHS = {"/health"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in _OPEN_PATHS or Config.ALLOW_ANONYMOUS:
            return await call_next(request)
        if not Config.API_KEY:
            return JSONResponse(
                status_code=503,
                content={"error": "misconfigured",
                         "detail": "ORCHESTRATOR_API_KEY is not set and ALLOW_ANONYMOUS is false."},
            )
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(token, Config.API_KEY):
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "detail": "Valid Bearer token required."},
            )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Background task: auto-stop idle computers
# ---------------------------------------------------------------------------

_idle_task: asyncio.Task | None = None


async def _idle_monitor() -> None:
    """Periodically check for idle computers and stop them."""
    while True:
        try:
            if Config.IDLE_TIMEOUT_MINUTES > 0:
                idle = await db.get_idle_computers(Config.IDLE_TIMEOUT_MINUTES)
                for comp in idle:
                    logger.info(
                        "Auto-stopping idle computer %s (%s) — no activity for %d min",
                        comp["id"], comp["name"], Config.IDLE_TIMEOUT_MINUTES,
                    )
                    await _do_stop(comp["id"])
        except Exception:
            logger.exception("Error in idle monitor")
        await asyncio.sleep(60)  # check every minute


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    global _idle_task

    logger.info("Starting Computer on Demand orchestrator")
    logger.info("  Image:         %s", Config.COMPUTER_IMAGE)
    logger.info("  Max running:   %s", Config.MAX_RUNNING_COMPUTERS)
    logger.info("  Idle timeout:  %s min", Config.IDLE_TIMEOUT_MINUTES)
    logger.info("  Host address:  %s", Config.HOST_ADDRESS)
    logger.info("  Public base:   %s", Config.PUBLIC_BASE_URL)
    logger.info("  Auth:          %s", "anonymous (dev only!)"
                if Config.ALLOW_ANONYMOUS else ("configured" if Config.API_KEY else "MISSING — non-health endpoints will 503"))

    if not Config.VNC_PASSWORD:
        raise RuntimeError(
            "VNC_PASSWORD is not set — refusing to boot desktops with a default "
            "password. Set VNC_PASSWORD in Coolify (runtime var) and redeploy."
        )

    # Reconcile DB with Docker state on startup
    await docker_manager.reconcile()

    # Start idle monitor
    _idle_task = asyncio.create_task(_idle_monitor())

    yield

    # Shutdown
    if _idle_task:
        _idle_task.cancel()
    logger.info("Orchestrator shutting down")


app = FastAPI(
    title="Computer on Demand",
    description="Spin up, stop, resume, and snapshot desktop computers for AI models.",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(AuthMiddleware)

# Default-deny CORS: agent traffic is server-side and needs none; browser
# callers (VNC viewer) are allowlisted via CORS_ORIGINS.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=Config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _make_endpoints(comp: dict) -> Endpoints | None:
    if comp["status"] != "running":
        return None
    host = Config.HOST_ADDRESS
    base = Config.PUBLIC_BASE_URL.rstrip("/")
    cid = comp["id"]
    return Endpoints(
        vnc=f"http://{host}:{comp['vnc_port']}",
        computer_server=f"http://{host}:{comp['cs_port']}",
        cdp=f"http://{host}:{comp['cdp_port']}",
        # Broker-proxied URLs: the only ones a remote agent needs. Starship
        # talks to these — never to the raw host ports above.
        broker_mcp_url=f"{base}/computers/{cid}/mcp",
        broker_api_url=f"{base}/computers/{cid}/api",
    )


def _to_response(comp: dict) -> ComputerResponse:
    return ComputerResponse(
        id=comp["id"],
        name=comp["name"],
        status=ComputerStatus(comp["status"]),
        owner=comp.get("owner"),
        endpoints=_make_endpoints(comp),
        cpu_limit=comp["cpu_limit"],
        memory_limit=comp["memory_limit"],
        resolution=comp["resolution"],
        created_at=datetime.fromisoformat(comp["created_at"]),
        last_activity=datetime.fromisoformat(comp["last_activity"]),
        stopped_at=(
            datetime.fromisoformat(comp["stopped_at"])
            if comp.get("stopped_at")
            else None
        ),
    )


async def _do_stop(computer_id: str) -> dict:
    """Internal stop logic shared by the API and idle monitor."""
    await docker_manager.stop_container(computer_id)
    comp = await db.update_computer(
        computer_id,
        status="stopped",
        stopped_at=datetime.now(timezone.utc).isoformat(),
    )
    return comp


async def _gc_failed_create(computer_id: str) -> None:
    """Remove all traces of a create that never became healthy.

    Without this, failed creates sit in `error` holding their port triple
    forever and the pool slowly drains. Best-effort: never raise.
    """
    try:
        await docker_manager.remove_container(computer_id)
    except Exception:
        logger.warning("GC: container removal failed for %s", computer_id, exc_info=True)
    try:
        await db.delete_computer(computer_id)
    except Exception:
        logger.warning("GC: row delete failed for %s", computer_id, exc_info=True)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
async def health():
    running = await db.count_running()
    return HealthResponse(
        status="ok",
        running_computers=running,
        max_running=Config.MAX_RUNNING_COMPUTERS,
        docker_socket_present=os.path.exists("/var/run/docker.sock"),
        docker_reachable=await docker_manager.docker_ping(),
    )


@app.post("/computers", response_model=ComputerResponse, status_code=201)
async def create_computer(req: CreateComputerRequest | None = None):
    """Create and start a new computer.

    Idempotent per owner: repeat POSTs with the same `owner` (e.g.
    `starship:conv-<id>`) return the existing creating/running computer
    instead of spawning a duplicate. Claim once per agent, reuse the id.
    """
    if req is None:
        req = CreateComputerRequest()

    # Idempotent reclaim — one owner, one live computer.
    if req.owner:
        existing = await db.get_computer_by_owner(req.owner)
        if existing is not None and existing["status"] in ("creating", "running"):
            await db.touch_activity(existing["id"])
            existing = await db.get_computer(existing["id"])
            return _to_response(existing)

    # Check limits
    running = await db.count_running()
    if running >= Config.MAX_RUNNING_COMPUTERS:
        raise HTTPException(
            status_code=429,
            detail=f"Maximum running computers ({Config.MAX_RUNNING_COMPUTERS}) reached. "
            f"Stop or destroy an existing computer first.",
        )

    all_computers = await db.list_computers()
    if len(all_computers) >= Config.MAX_TOTAL_COMPUTERS:
        raise HTTPException(
            status_code=429,
            detail=f"Maximum total computers ({Config.MAX_TOTAL_COMPUTERS}) reached. "
            f"Destroy unused computers first.",
        )

    # Allocate — port-triple selection and row insert happen atomically
    # inside reserve_computer (no check-then-act race under parallel claims).
    computer_id = _short_id()
    name = req.name or f"computer-{computer_id}"
    cpu_limit = req.cpu_limit or Config.DEFAULT_CPU_LIMIT
    memory_limit = req.memory_limit or Config.DEFAULT_MEMORY_LIMIT
    resolution = req.resolution or Config.DEFAULT_RESOLUTION

    try:
        comp = await db.reserve_computer(
            computer_id=computer_id,
            name=name,
            owner=req.owner,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
            shm_size=Config.DEFAULT_SHM_SIZE,
            resolution=resolution,
            vnc_start=Config.PORT_RANGE_VNC_START,
            cs_start=Config.PORT_RANGE_COMPUTER_START,
            cdp_start=Config.PORT_RANGE_CDP_START,
            range_size=Config.PORT_RANGE_SIZE,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=429, detail=str(e))

    vnc_port, cs_port, cdp_port = comp["vnc_port"], comp["cs_port"], comp["cdp_port"]

    # Create and start Docker container
    try:
        container_id = await docker_manager.create_container(
            computer_id=computer_id,
            vnc_port=vnc_port,
            cs_port=cs_port,
            cdp_port=cdp_port,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
            shm_size=Config.DEFAULT_SHM_SIZE,
            resolution=resolution,
        )
        await db.update_computer(computer_id, container_id=container_id)

        # Wait for computer-server to be healthy
        healthy = await docker_manager.wait_for_healthy(cs_port)
        if healthy:
            comp = await db.update_computer(computer_id, status="running")
        else:
            await _gc_failed_create(computer_id)
            raise HTTPException(
                status_code=503,
                detail="Computer started but computer-server did not become healthy in time. "
                "Claim cleaned up — retry the request.",
            )
    except HTTPException:
        raise
    except Exception as e:
        await _gc_failed_create(computer_id)
        logger.exception("Failed to create computer %s", computer_id)
        raise HTTPException(status_code=500, detail=str(e))

    return _to_response(comp)


@app.get("/computers", response_model=ComputerListResponse)
async def list_computers(owner: str | None = None):
    """List all computers (running + stopped). Filter by claim owner."""
    computers = await db.list_computers(owner=owner)
    running = sum(1 for c in computers if c["status"] == "running")
    return ComputerListResponse(
        computers=[_to_response(c) for c in computers],
        running_count=running,
        max_running=Config.MAX_RUNNING_COMPUTERS,
    )


@app.get("/computers/{computer_id}", response_model=ComputerResponse)
async def get_computer(computer_id: str):
    """Get a single computer's details."""
    comp = await db.get_computer(computer_id)
    if comp is None:
        raise HTTPException(status_code=404, detail="Computer not found")
    await db.touch_activity(computer_id)
    return _to_response(comp)


@app.post("/computers/{computer_id}/heartbeat", response_model=ComputerResponse)
async def heartbeat_computer(computer_id: str):
    """Agent keep-alive: marks activity so the idle reaper spares this computer.

    Agents with long-running tasks should hit this (or any proxied guest
    route, which also touches activity) more often than IDLE_TIMEOUT_MINUTES.
    """
    comp = await db.get_computer(computer_id)
    if comp is None:
        raise HTTPException(status_code=404, detail="Computer not found")
    if comp["status"] != "running":
        raise HTTPException(status_code=409, detail=f"Computer is {comp['status']}, not running")
    await db.touch_activity(computer_id)
    comp = await db.get_computer(computer_id)
    return _to_response(comp)


# ---------------------------------------------------------------------------
# Broker-proxied guest routes — remote agents talk ONLY to these.
#
# Raw host ports (6901+/8001+/9223+) are not exposed through Coolify, so the
# direct URLs in `endpoints` only work on the host/LAN. These proxied routes
# ride the broker's own :3000 listener (i.e. the public FQDN) instead.
# Every proxied call counts as activity for the idle reaper.
# ---------------------------------------------------------------------------

# Hop-by-hop headers that must never cross the proxy boundary.
_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
# Allowlisted request/response headers (MCP session affinity lives here).
_PASS_HEADERS = {
    "content-type", "accept", "mcp-session-id", "mcp-protocol-version",
    "last-event-id", "user-agent",
}


async def _proxy_to_guest(request: Request, comp: dict, guest_path: str, timeout: float | None):
    """Stream request → guest computer-server → response. Never buffers SSE."""
    url = f"http://127.0.0.1:{comp['cs_port']}/{guest_path.lstrip('/')}"
    fwd = {k: v for k, v in request.headers.items() if k.lower() in _PASS_HEADERS}
    body = await request.body()
    client = httpx.AsyncClient(timeout=timeout)
    try:
        upstream = await client.send(
            client.build_request(request.method, url, headers=fwd, content=body),
            stream=True,
        )
    except httpx.ConnectError:
        await client.aclose()
        raise HTTPException(status_code=502, detail="Guest computer-server unreachable.")

    async def _aiter():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() in _PASS_HEADERS}
    await db.touch_activity(comp["id"])
    return StreamingResponse(_aiter(), status_code=upstream.status_code, headers=out_headers)


async def _require_running_guest(computer_id: str) -> dict:
    comp = await db.get_computer(computer_id)
    if comp is None:
        raise HTTPException(status_code=404, detail="Computer not found")
    if comp["status"] != "running":
        raise HTTPException(status_code=409, detail=f"Computer is {comp['status']}, not running")
    return comp


@app.api_route(
    "/computers/{computer_id}/mcp",
    methods=["GET", "POST", "DELETE"],
    include_in_schema=True,
)
async def proxy_mcp(computer_id: str, request: Request):
    """computer-server MCP (streamable HTTP) via the broker.

    This is the primary agent contract: point an MCP client at the returned
    `broker_mcp_url` and drive screenshot/click/type/shell/file tools.
    GET opens the long-lived SSE stream — proxied without buffering.
    """
    comp = await _require_running_guest(computer_id)
    return await _proxy_to_guest(request, comp, "mcp", timeout=None)


@app.api_route(
    "/computers/{computer_id}/api/{guest_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=True,
)
async def proxy_guest_api(computer_id: str, guest_path: str, request: Request):
    """computer-server REST API via the broker (`/cmd`, `/status`, ...)."""
    comp = await _require_running_guest(computer_id)
    return await _proxy_to_guest(request, comp, guest_path or "/", timeout=120.0)


@app.get("/computers/{computer_id}/api", include_in_schema=False)
async def proxy_guest_api_root(computer_id: str, request: Request):
    comp = await _require_running_guest(computer_id)
    return await _proxy_to_guest(request, comp, "/", timeout=120.0)


@app.post("/computers/{computer_id}/stop", response_model=ComputerResponse)
async def stop_computer(computer_id: str):
    """Stop a running computer. Volume persists — all logins and files are kept."""
    comp = await db.get_computer(computer_id)
    if comp is None:
        raise HTTPException(status_code=404, detail="Computer not found")
    if comp["status"] != "running":
        raise HTTPException(status_code=409, detail=f"Computer is {comp['status']}, not running")

    comp = await _do_stop(computer_id)
    return _to_response(comp)


@app.post("/computers/{computer_id}/start", response_model=ComputerResponse)
async def start_computer(computer_id: str):
    """Start a stopped computer. Mounts the same volume — logins and files are back."""
    comp = await db.get_computer(computer_id)
    if comp is None:
        raise HTTPException(status_code=404, detail="Computer not found")
    if comp["status"] == "running":
        raise HTTPException(status_code=409, detail="Computer is already running")
    if comp["status"] not in ("stopped", "error"):
        raise HTTPException(status_code=409, detail=f"Computer is {comp['status']}, cannot start")

    # Check concurrency limit
    running = await db.count_running()
    if running >= Config.MAX_RUNNING_COMPUTERS:
        raise HTTPException(
            status_code=429,
            detail=f"Maximum running computers ({Config.MAX_RUNNING_COMPUTERS}) reached.",
        )

    try:
        # Check if the container still exists in Docker
        docker_status = await docker_manager.get_container_status(computer_id)
        if docker_status is not None:
            # Container exists, just start it
            await docker_manager.start_container(computer_id)
        else:
            # Container was removed — recreate with same ports and volume
            container_id = await docker_manager.create_container(
                computer_id=computer_id,
                vnc_port=comp["vnc_port"],
                cs_port=comp["cs_port"],
                cdp_port=comp["cdp_port"],
                cpu_limit=comp["cpu_limit"],
                memory_limit=comp["memory_limit"],
                shm_size=comp["shm_size"],
                resolution=comp["resolution"],
            )
            await db.update_computer(computer_id, container_id=container_id)

        # Wait for healthy
        healthy = await docker_manager.wait_for_healthy(comp["cs_port"])
        if healthy:
            comp = await db.update_computer(
                computer_id, status="running", stopped_at=None,
            )
        else:
            comp = await db.update_computer(computer_id, status="error")
            raise HTTPException(status_code=503, detail="Computer started but failed health check.")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to start computer %s", computer_id)
        raise HTTPException(status_code=500, detail=str(e))

    await db.touch_activity(computer_id)
    return _to_response(comp)


@app.post("/computers/{computer_id}/snapshot", response_model=SnapshotResponse)
async def snapshot_computer(computer_id: str):
    """Create a snapshot (docker commit) of the computer."""
    comp = await db.get_computer(computer_id)
    if comp is None:
        raise HTTPException(status_code=404, detail="Computer not found")
    if comp["status"] != "running":
        raise HTTPException(status_code=409, detail="Computer must be running to snapshot")

    snapshot_id = _short_id()
    try:
        image_tag = await docker_manager.snapshot_container(computer_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    snap = await db.create_snapshot(
        snapshot_id=snapshot_id,
        computer_id=computer_id,
        image_tag=image_tag,
    )

    await db.touch_activity(computer_id)
    return SnapshotResponse(
        snapshot_id=snap["id"],
        computer_id=snap["computer_id"],
        image_tag=snap["image_tag"],
        created_at=datetime.fromisoformat(snap["created_at"]),
    )


@app.get("/computers/{computer_id}/snapshots", response_model=SnapshotListResponse)
async def list_snapshots(computer_id: str):
    """List all snapshots for a computer."""
    comp = await db.get_computer(computer_id)
    if comp is None:
        raise HTTPException(status_code=404, detail="Computer not found")

    snaps = await db.list_snapshots(computer_id)
    return SnapshotListResponse(
        snapshots=[
            SnapshotResponse(
                snapshot_id=s["id"],
                computer_id=s["computer_id"],
                image_tag=s["image_tag"],
                created_at=datetime.fromisoformat(s["created_at"]),
            )
            for s in snaps
        ]
    )


@app.post("/computers/{computer_id}/restore", response_model=ComputerResponse)
async def restore_computer(computer_id: str, req: RestoreRequest):
    """Restore a computer from a snapshot. Stops the current container, starts from snapshot image."""
    comp = await db.get_computer(computer_id)
    if comp is None:
        raise HTTPException(status_code=404, detail="Computer not found")

    snap = await db.get_snapshot(req.snapshot_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    if snap["computer_id"] != computer_id:
        raise HTTPException(status_code=400, detail="Snapshot does not belong to this computer")

    try:
        # Stop and remove current container
        await docker_manager.remove_container(computer_id)

        # Start a new container from the snapshot image (same ports, same volume)
        container_id = await docker_manager.create_container(
            computer_id=computer_id,
            vnc_port=comp["vnc_port"],
            cs_port=comp["cs_port"],
            cdp_port=comp["cdp_port"],
            cpu_limit=comp["cpu_limit"],
            memory_limit=comp["memory_limit"],
            shm_size=comp["shm_size"],
            resolution=comp["resolution"],
            image=snap["image_tag"],
        )
        await db.update_computer(computer_id, container_id=container_id)

        healthy = await docker_manager.wait_for_healthy(comp["cs_port"])
        if healthy:
            comp = await db.update_computer(computer_id, status="running", stopped_at=None)
        else:
            comp = await db.update_computer(computer_id, status="error")
            raise HTTPException(status_code=503, detail="Restored but failed health check.")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to restore computer %s", computer_id)
        raise HTTPException(status_code=500, detail=str(e))

    await db.touch_activity(computer_id)
    return _to_response(comp)


@app.delete("/computers/{computer_id}", status_code=200)
async def delete_computer(computer_id: str):
    """Permanently destroy a computer — stops container, deletes volume, removes all data."""
    comp = await db.get_computer(computer_id)
    if comp is None:
        raise HTTPException(status_code=404, detail="Computer not found")

    await docker_manager.destroy_computer(computer_id)
    await db.delete_computer(computer_id)

    return {"status": "destroyed", "id": computer_id}


# ---------------------------------------------------------------------------
# Admin (auth-gated): host introspection for ops
# ---------------------------------------------------------------------------


@app.get("/admin/images")
async def admin_images():
    """List desktop-candidate images present on the host daemon."""
    return {
        "default_image": Config.COMPUTER_IMAGE,
        "images": await docker_manager.list_images(),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=3000, log_level="info")
