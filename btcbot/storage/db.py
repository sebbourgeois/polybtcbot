"""SQLite connection helpers — mirrors the parent project pattern."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from ..config import CONFIG

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()


async def init_db(path: Path | None = None) -> None:
    p = path or CONFIG.db_path
    async with aiosqlite.connect(p) as conn:
        await conn.executescript(_SCHEMA_SQL)
        await conn.commit()


@asynccontextmanager
async def connect(path: Path | None = None) -> AsyncIterator[aiosqlite.Connection]:
    p = path or CONFIG.db_path
    async with aiosqlite.connect(p) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        yield conn
