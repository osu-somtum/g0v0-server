"""Read-only access to bancho.py's `freedomdive_db` for the stable importer.

g0v0's own engine points at the `lazer` database; this opens a second async engine
to bancho's database on the SAME MySQL server (shared host/user/password), so the
importer can read `scores` and `maps` directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.config import settings

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

_engine: AsyncEngine | None = None

# Vanilla modes only (osu/taiko/fruits/mania). RX (4-7) / AP (8) are out of scope
# this phase, matching the stats bridge.
VANILLA_MODES = (0, 1, 2, 3)


def get_bancho_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(settings.bancho_database_url, pool_pre_ping=True, pool_recycle=3600)
    return _engine


async def fetch_new_scores(conn: AsyncConnection, since_id: int, limit: int = 5000) -> list[Mapping[str, Any]]:
    """Vanilla, non-bot bancho scores with id > since_id, oldest first."""
    rows = (
        await conn.execute(
            text(
                """
                SELECT s.id, s.map_md5, s.score, s.pp, s.acc, s.max_combo, s.mods,
                       s.grade, s.status, s.mode, s.play_time, s.userid, s.perfect,
                       s.n300, s.n100, s.n50, s.nmiss, s.ngeki, s.nkatu,
                       u.name AS username
                FROM scores s
                JOIN users u ON u.id = s.userid
                WHERE s.id > :since
                  AND s.userid <> 1
                  AND s.mode IN (0, 1, 2, 3)
                ORDER BY s.id
                LIMIT :limit
                """,
            ),
            {"since": since_id, "limit": limit},
        )
    ).mappings().all()
    return list(rows)


async def fetch_map_by_md5(conn: AsyncConnection, md5: str) -> Mapping[str, Any] | None:
    row = (
        await conn.execute(
            text(
                """
                SELECT server, id, set_id, status, md5, artist, title, version, creator,
                       last_update, total_length, max_combo, mode, bpm, cs, ar, od, hp,
                       diff, owner_id
                FROM maps
                WHERE md5 = :md5
                """,
            ),
            {"md5": md5},
        )
    ).mappings().first()
    return row


async def fetch_custom_maps(conn: AsyncConnection) -> list[Mapping[str, Any]]:
    """All somtum custom (private) maps — these don't exist on osu! so must be
    bridged locally for lazer browse/search."""
    rows = (
        await conn.execute(
            text(
                """
                SELECT server, id, set_id, status, md5, artist, title, version, creator,
                       last_update, total_length, max_combo, mode, bpm, cs, ar, od, hp,
                       diff, owner_id
                FROM maps
                WHERE server = 'private'
                """,
            ),
        )
    ).mappings().all()
    return list(rows)


async def count_new_scores(conn: AsyncConnection, since_id: int) -> int:
    return int(
        await conn.scalar(
            text(
                "SELECT COUNT(*) FROM scores WHERE id > :since AND userid <> 1 AND mode IN (0,1,2,3)",
            ),
            {"since": since_id},
        )
        or 0,
    )
