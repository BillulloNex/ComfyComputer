"""Pydantic models for the Computer on Demand API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ComputerStatus(str, Enum):
    """Lifecycle states of a computer."""

    CREATING = "creating"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class CreateComputerRequest(BaseModel):
    """Request body for POST /computers."""

    name: str | None = Field(
        default=None,
        description="Human-friendly label. Auto-generated if omitted.",
    )
    cpu_limit: str | None = Field(
        default=None,
        description="Max CPUs, e.g. '1.0'. Uses server default if omitted.",
    )
    memory_limit: str | None = Field(
        default=None,
        description="Max RAM, e.g. '3g'. Uses server default if omitted.",
    )
    resolution: str | None = Field(
        default=None,
        description="VNC resolution, e.g. '1920x1080'. Uses server default if omitted.",
    )


class RestoreRequest(BaseModel):
    """Request body for POST /computers/{id}/restore."""

    snapshot_id: str = Field(description="Snapshot image tag to restore from.")


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class Endpoints(BaseModel):
    """Connection endpoints for a running computer."""

    vnc: str = Field(description="KasmVNC web desktop URL")
    computer_server: str = Field(description="Cua computer-server REST/MCP URL")
    cdp: str = Field(description="Chrome DevTools Protocol URL")


class ComputerResponse(BaseModel):
    """Full representation of a computer."""

    id: str
    name: str
    status: ComputerStatus
    endpoints: Endpoints | None = Field(
        default=None,
        description="Connection endpoints. Only present when status is 'running'.",
    )
    cpu_limit: str
    memory_limit: str
    resolution: str
    created_at: datetime
    last_activity: datetime
    stopped_at: datetime | None = None


class ComputerListResponse(BaseModel):
    """Response for GET /computers."""

    computers: list[ComputerResponse]
    running_count: int
    max_running: int


class SnapshotResponse(BaseModel):
    """Response for POST /computers/{id}/snapshot."""

    snapshot_id: str
    computer_id: str
    image_tag: str
    created_at: datetime


class SnapshotListResponse(BaseModel):
    """Response for GET /computers/{id}/snapshots."""

    snapshots: list[SnapshotResponse]


class HealthResponse(BaseModel):
    """Response for GET /health."""

    status: str = "ok"
    running_computers: int
    max_running: int


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str
    detail: str | None = None
