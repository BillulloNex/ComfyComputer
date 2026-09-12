"""Computer on Demand — Orchestrator API.

FastAPI service that manages on-demand desktop computers for AI models.
Each computer is a Docker container backed by a persistent named volume.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

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
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
    return Endpoints(
        vnc=f"http://{host}:{comp['vnc_port']}",
        computer_server=f"http://{host}:{comp['cs_port']}",
        cdp=f"http://{host}:{comp['cdp_port']}",
    )


def _to_response(comp: dict) -> ComputerResponse:
    return ComputerResponse(
        id=comp["id"],
        name=comp["name"],
        status=ComputerStatus(comp["status"]),
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
    )


@app.post("/computers", response_model=ComputerResponse, status_code=201)
async def create_computer(req: CreateComputerRequest | None = None):
    """Create and start a new computer."""
    if req is None:
        req = CreateComputerRequest()

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

    # Allocate
    computer_id = _short_id()
    name = req.name or f"computer-{computer_id}"
    cpu_limit = req.cpu_limit or Config.DEFAULT_CPU_LIMIT
    memory_limit = req.memory_limit or Config.DEFAULT_MEMORY_LIMIT
    resolution = req.resolution or Config.DEFAULT_RESOLUTION

    vnc_port, cs_port, cdp_port = await docker_manager.allocate_ports()

    # Create DB record
    comp = await db.create_computer(
        computer_id=computer_id,
        name=name,
        container_id=None,
        vnc_port=vnc_port,
        cs_port=cs_port,
        cdp_port=cdp_port,
        cpu_limit=cpu_limit,
        memory_limit=memory_limit,
        shm_size=Config.DEFAULT_SHM_SIZE,
        resolution=resolution,
    )

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
            comp = await db.update_computer(computer_id, status="error")
            raise HTTPException(
                status_code=503,
                detail="Computer started but computer-server did not become healthy in time.",
            )
    except HTTPException:
        raise
    except Exception as e:
        await db.update_computer(computer_id, status="error")
        logger.exception("Failed to create computer %s", computer_id)
        raise HTTPException(status_code=500, detail=str(e))

    return _to_response(comp)


@app.get("/computers", response_model=ComputerListResponse)
async def list_computers():
    """List all computers (running + stopped)."""
    computers = await db.list_computers()
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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=3000, log_level="info")
