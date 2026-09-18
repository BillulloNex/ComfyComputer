"""Docker lifecycle manager for on-demand computers."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import docker
import httpx
from docker.errors import APIError, NotFound
from docker.types import DeviceRequest

from config import Config
import db

logger = logging.getLogger("orchestrator.docker")

# Lazy-init Docker client (thread-safe singleton)
_client: docker.DockerClient | None = None


def _docker() -> docker.DockerClient:
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


async def docker_ping() -> bool:
    """Best-effort Docker daemon ping for /health. Never raises."""
    def _ping() -> bool:
        try:
            _docker().ping()
            return True
        except Exception:
            return False

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _ping)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


def ensure_network() -> None:
    """Create the shared bridge network if it doesn't exist."""
    client = _docker()
    try:
        client.networks.get(Config.DOCKER_NETWORK)
    except NotFound:
        client.networks.create(Config.DOCKER_NETWORK, driver="bridge")
        logger.info("Created Docker network: %s", Config.DOCKER_NETWORK)


# ---------------------------------------------------------------------------
# Port allocation
#
# NOTE: live allocation happens atomically in db.reserve_computer()
# (triple selection + row insert under one lock). Do NOT reintroduce an
# allocate-then-insert split here — parallel claims will collide.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Guest addressing
#
# The broker runs in its own container, so 127.0.0.1 NEVER reaches a guest's
# host-published ports (loopback is per-network-namespace). Guests are
# reached through an address that routes to the host from inside this
# container: explicit GUEST_HOST env wins, otherwise the container's default
# gateway (host-published ports bind 0.0.0.0, so the gateway always works).
# ---------------------------------------------------------------------------


def guest_host() -> str:
    """Host (from this container's POV) where guest ports are reachable."""
    explicit = os.getenv("GUEST_HOST", "").strip()
    if explicit:
        return explicit
    # Default gateway of this container — host-published ports answer there.
    try:
        with open("/proc/net/route") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    gw_hex = parts[2]
                    gw_int = int(gw_hex, 16)
                    return ".".join(
                        str((gw_int >> (8 * i)) & 0xFF) for i in range(4)
                    )
    except Exception:
        logger.warning("Could not read default gateway, falling back", exc_info=True)
    return Config.HOST_ADDRESS


def guest_api_base(cs_port: int) -> str:
    """Base URL for a guest's computer-server, broker-reachable."""
    return f"http://{guest_host()}:{cs_port}"


# ---------------------------------------------------------------------------
# Container helpers
# ---------------------------------------------------------------------------

CONTAINER_PREFIX = "cua-computer-"
VOLUME_PREFIX = "cua-home-"
SNAPSHOT_PREFIX = "cua-snapshot-"


def _container_name(computer_id: str) -> str:
    return f"{CONTAINER_PREFIX}{computer_id}"


def _volume_name(computer_id: str) -> str:
    return f"{VOLUME_PREFIX}{computer_id}"


def _snapshot_tag(computer_id: str, label: str) -> str:
    return f"{SNAPSHOT_PREFIX}{computer_id}:{label}"


# ---------------------------------------------------------------------------
# Lifecycle operations
# ---------------------------------------------------------------------------


async def create_container(
    computer_id: str,
    vnc_port: int,
    cs_port: int,
    cdp_port: int,
    cpu_limit: str,
    memory_limit: str,
    shm_size: str,
    resolution: str,
    image: str | None = None,
) -> str:
    """Create and start a new computer container. Returns the Docker container ID."""
    client = _docker()
    ensure_network()

    container_name = _container_name(computer_id)
    volume_name = _volume_name(computer_id)
    use_image = image or Config.COMPUTER_IMAGE

    def _run():
        container = client.containers.run(
            image=use_image,
            name=container_name,
            detach=True,
            # Resource limits
            nano_cpus=int(float(cpu_limit) * 1e9),
            mem_limit=memory_limit,
            shm_size=shm_size,
            # Port mapping: host_port -> container_port
            ports={
                "6901/tcp": ("0.0.0.0", vnc_port),
                "8000/tcp": ("0.0.0.0", cs_port),
                "9222/tcp": ("0.0.0.0", cdp_port),
            },
            # Persistent home directory
            volumes={
                volume_name: {"bind": "/home/kasm-user", "mode": "rw"},
            },
            # Environment
            environment={
                "VNC_PW": Config.VNC_PASSWORD,
                "VNC_RESOLUTION": resolution,
            },
            # Network
            network=Config.DOCKER_NETWORK,
            # Restart policy — don't auto-restart, orchestrator manages lifecycle
            restart_policy={"Name": "no"},
        )
        return container.id

    # Run in executor to avoid blocking the event loop
    container_id = await asyncio.get_event_loop().run_in_executor(None, _run)
    logger.info("Created container %s for computer %s", container_id[:12], computer_id)
    return container_id


async def stop_container(computer_id: str) -> None:
    """Stop a running computer container (preserves volume)."""
    client = _docker()
    name = _container_name(computer_id)

    def _stop():
        try:
            container = client.containers.get(name)
            container.stop(timeout=15)
            logger.info("Stopped container %s", name)
        except NotFound:
            logger.warning("Container %s not found during stop", name)

    await asyncio.get_event_loop().run_in_executor(None, _stop)


async def start_container(computer_id: str) -> None:
    """Start a stopped computer container."""
    client = _docker()
    name = _container_name(computer_id)

    def _start():
        try:
            container = client.containers.get(name)
            container.start()
            logger.info("Started container %s", name)
        except NotFound:
            raise RuntimeError(f"Container {name} not found. Computer may need to be recreated.")

    await asyncio.get_event_loop().run_in_executor(None, _start)


async def remove_container(computer_id: str) -> None:
    """Force-remove a container (does NOT remove the volume)."""
    client = _docker()
    name = _container_name(computer_id)

    def _remove():
        try:
            container = client.containers.get(name)
            container.remove(force=True)
            logger.info("Removed container %s", name)
        except NotFound:
            logger.warning("Container %s already gone", name)

    await asyncio.get_event_loop().run_in_executor(None, _remove)


async def remove_volume(computer_id: str) -> None:
    """Remove the named volume for a computer."""
    client = _docker()
    vol_name = _volume_name(computer_id)

    def _remove():
        try:
            vol = client.volumes.get(vol_name)
            vol.remove(force=True)
            logger.info("Removed volume %s", vol_name)
        except NotFound:
            logger.warning("Volume %s already gone", vol_name)

    await asyncio.get_event_loop().run_in_executor(None, _remove)


async def destroy_computer(computer_id: str) -> None:
    """Remove the container AND the volume. Permanent."""
    await remove_container(computer_id)
    await remove_volume(computer_id)


async def snapshot_container(computer_id: str, label: str | None = None) -> str:
    """docker commit the container into a tagged snapshot image. Returns image tag."""
    client = _docker()
    name = _container_name(computer_id)
    if label is None:
        label = datetime.now(timezone.utc).strftime("snap-%Y%m%dT%H%M%S")
    tag = _snapshot_tag(computer_id, label)
    repo, _, tag_part = tag.rpartition(":")

    def _commit():
        try:
            container = client.containers.get(name)
            container.commit(repository=repo, tag=tag_part)
            logger.info("Snapshot created: %s", tag)
            return tag
        except NotFound:
            raise RuntimeError(f"Container {name} not found for snapshot.")

    return await asyncio.get_event_loop().run_in_executor(None, _commit)


async def list_images() -> list[dict]:
    """Images present on the host daemon (tag + size). Never raises."""

    def _list() -> list[dict]:
        try:
            out = []
            for img in _docker().images.list():
                out.append({
                    "tags": img.tags or [],
                    "id": (img.id or "")[:19],
                    "size_mb": round((img.attrs.get("Size", 0) or 0) / 1e6, 1),
                })
            return out
        except Exception as e:
            return [{"error": str(e)}]

    return await asyncio.get_event_loop().run_in_executor(None, _list)


async def get_container_status(computer_id: str) -> str | None:
    """Return the Docker container status string, or None if not found."""
    client = _docker()
    name = _container_name(computer_id)

    def _status():
        try:
            container = client.containers.get(name)
            return container.status  # 'running', 'exited', 'created', etc.
        except NotFound:
            return None

    return await asyncio.get_event_loop().run_in_executor(None, _status)


# ---------------------------------------------------------------------------
# Health checking
# ---------------------------------------------------------------------------


async def wait_for_healthy(host: str, cs_port: int) -> bool:
    """Poll guest computer-server /status until 200 or timeout.

    `host` must be broker-reachable (see guest_host) — never 127.0.0.1.
    """
    url = f"http://{host}:{cs_port}/status"
    deadline = asyncio.get_event_loop().time() + Config.HEALTH_CHECK_TIMEOUT

    async with httpx.AsyncClient() as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                resp = await client.get(url, timeout=3)
                if resp.status_code == 200:
                    logger.info("Computer healthy at %s", url)
                    return True
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                pass
            await asyncio.sleep(Config.HEALTH_CHECK_INTERVAL)

    logger.error("Health check timed out for %s", url)
    return False


# ---------------------------------------------------------------------------
# Reconciliation (on orchestrator startup)
# ---------------------------------------------------------------------------


async def reconcile() -> None:
    """Sync DB state with actual Docker container states."""
    computers = await db.list_computers()
    for comp in computers:
        docker_status = await get_container_status(comp["id"])
        db_status = comp["status"]

        if docker_status is None and db_status in ("running", "creating"):
            # Container vanished — mark as stopped
            logger.warning("Computer %s container missing, marking stopped", comp["id"])
            await db.update_computer(comp["id"], status="stopped", stopped_at=db._now())
        elif docker_status == "running" and db_status != "running":
            await db.update_computer(comp["id"], status="running")
        elif docker_status == "exited" and db_status == "running":
            await db.update_computer(comp["id"], status="stopped", stopped_at=db._now())

    logger.info("Reconciliation complete")
