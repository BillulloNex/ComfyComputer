"""SQLite state store for computers and snapshots."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import aiosqlite

from config import Config

_DB_LOCK = asyncio.Lock()

SCHEMA = """\
CREATE TABLE IF NOT EXISTS computers (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'creating',
    container_id    TEXT,
    vnc_port        INTEGER NOT NULL,
    cs_port         INTEGER NOT NULL,
    cdp_port        INTEGER NOT NULL,
    cpu_limit       TEXT NOT NULL,
    memory_limit    TEXT NOT NULL,
    shm_size        TEXT NOT NULL,
    resolution      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    last_activity   TEXT NOT NULL,
    stopped_at      TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id              TEXT PRIMARY KEY,
    computer_id     TEXT NOT NULL,
    image_tag       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (computer_id) REFERENCES computers(id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _get_db() -> aiosqlite.Connection:
    os.makedirs(os.path.dirname(Config.DB_PATH), exist_ok=True)
    db = await aiosqlite.connect(Config.DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()
    return db


async def create_computer(
    *,
    computer_id: str,
    name: str,
    container_id: str | None,
    vnc_port: int,
    cs_port: int,
    cdp_port: int,
    cpu_limit: str,
    memory_limit: str,
    shm_size: str,
    resolution: str,
) -> dict:
    now = _now()
    async with _DB_LOCK:
        db = await _get_db()
        try:
            await db.execute(
                """INSERT INTO computers
                   (id, name, status, container_id, vnc_port, cs_port, cdp_port,
                    cpu_limit, memory_limit, shm_size, resolution, created_at, last_activity)
                   VALUES (?, ?, 'creating', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    computer_id, name, container_id,
                    vnc_port, cs_port, cdp_port,
                    cpu_limit, memory_limit, shm_size, resolution,
                    now, now,
                ),
            )
            await db.commit()
            return await _get_computer(db, computer_id)
        finally:
            await db.close()


async def update_computer(computer_id: str, **fields) -> dict | None:
    if not fields:
        return await get_computer(computer_id)
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [computer_id]
    async with _DB_LOCK:
        db = await _get_db()
        try:
            await db.execute(
                f"UPDATE computers SET {set_clause} WHERE id = ?", values,
            )
            await db.commit()
            return await _get_computer(db, computer_id)
        finally:
            await db.close()


async def get_computer(computer_id: str) -> dict | None:
    async with _DB_LOCK:
        db = await _get_db()
        try:
            return await _get_computer(db, computer_id)
        finally:
            await db.close()


async def _get_computer(db: aiosqlite.Connection, computer_id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM computers WHERE id = ?", (computer_id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return dict(row)


async def list_computers() -> list[dict]:
    async with _DB_LOCK:
        db = await _get_db()
        try:
            cursor = await db.execute("SELECT * FROM computers ORDER BY created_at DESC")
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            await db.close()


async def delete_computer(computer_id: str) -> bool:
    async with _DB_LOCK:
        db = await _get_db()
        try:
            await db.execute("DELETE FROM snapshots WHERE computer_id = ?", (computer_id,))
            cursor = await db.execute("DELETE FROM computers WHERE id = ?", (computer_id,))
            await db.commit()
            return cursor.rowcount > 0
        finally:
            await db.close()


async def get_allocated_ports() -> set[int]:
    """Return all ports currently allocated (running or stopped, not destroyed)."""
    async with _DB_LOCK:
        db = await _get_db()
        try:
            cursor = await db.execute("SELECT vnc_port, cs_port, cdp_port FROM computers")
            rows = await cursor.fetchall()
            ports = set()
            for r in rows:
                ports.update([r["vnc_port"], r["cs_port"], r["cdp_port"]])
            return ports
        finally:
            await db.close()


async def count_running() -> int:
    async with _DB_LOCK:
        db = await _get_db()
        try:
            cursor = await db.execute(
                "SELECT COUNT(*) as cnt FROM computers WHERE status = 'running'"
            )
            row = await cursor.fetchone()
            return row["cnt"]
        finally:
            await db.close()


async def get_idle_computers(timeout_minutes: int) -> list[dict]:
    """Return running computers whose last_activity is older than timeout_minutes."""
    from datetime import timedelta

    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
    ).isoformat()
    async with _DB_LOCK:
        db = await _get_db()
        try:
            cursor = await db.execute(
                "SELECT * FROM computers WHERE status = 'running' AND last_activity < ?",
                (cutoff,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            await db.close()


async def touch_activity(computer_id: str) -> None:
    """Update last_activity to now."""
    await update_computer(computer_id, last_activity=_now())


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


async def create_snapshot(
    *, snapshot_id: str, computer_id: str, image_tag: str,
) -> dict:
    now = _now()
    async with _DB_LOCK:
        db = await _get_db()
        try:
            await db.execute(
                "INSERT INTO snapshots (id, computer_id, image_tag, created_at) VALUES (?, ?, ?, ?)",
                (snapshot_id, computer_id, image_tag, now),
            )
            await db.commit()
            cursor = await db.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,))
            row = await cursor.fetchone()
            return dict(row)
        finally:
            await db.close()


async def list_snapshots(computer_id: str) -> list[dict]:
    async with _DB_LOCK:
        db = await _get_db()
        try:
            cursor = await db.execute(
                "SELECT * FROM snapshots WHERE computer_id = ? ORDER BY created_at DESC",
                (computer_id,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            await db.close()


async def get_snapshot(snapshot_id: str) -> dict | None:
    async with _DB_LOCK:
        db = await _get_db()
        try:
            cursor = await db.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None
        finally:
            await db.close()
