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

# bancho gamemodes the importer understands: vanilla (0-3) + relax (4-6) + AP (8).
# RX/AP scores are gated at import time by enable_rx / enable_ap.
IMPORT_MODES = (0, 1, 2, 3, 4, 5, 6, 8)
_IMPORT_MODES_SQL = ", ".join(str(m) for m in IMPORT_MODES)


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
                  AND s.mode IN (0, 1, 2, 3, 4, 5, 6, 8)
                ORDER BY s.id
                LIMIT :limit
                """,
            ),
            {"since": since_id, "limit": limit},
        )
    ).mappings().all()
    return list(rows)


async def fetch_score_by_id(conn: AsyncConnection, score_id: int) -> Mapping[str, Any] | None:
    """A single bancho score row (for replay rebuilds), joined to its username."""
    return (
        await conn.execute(
            text(
                """
                SELECT s.id, s.map_md5, s.score, s.pp, s.acc, s.max_combo, s.mods,
                       s.grade, s.status, s.mode, s.play_time, s.userid, s.perfect,
                       s.n300, s.n100, s.n50, s.nmiss, s.ngeki, s.nkatu,
                       u.name AS username
                FROM scores s
                JOIN users u ON u.id = s.userid
                WHERE s.id = :id
                """,
            ),
            {"id": score_id},
        )
    ).mappings().first()


async def fetch_map_by_md5(conn: AsyncConnection, md5: str) -> Mapping[str, Any] | None:
    row = (
        await conn.execute(
            text(
                """
                SELECT m.server, m.id, m.set_id, m.status, m.md5, m.artist, m.title,
                       m.version, m.creator, m.last_update, m.total_length, m.max_combo,
                       m.mode, m.bpm, m.cs, m.ar, m.od, m.hp, m.diff, m.owner_id,
                       ms.uploaded_by
                FROM maps m
                LEFT JOIN mapsets ms ON ms.id = m.set_id
                WHERE m.md5 = :md5
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
                SELECT m.server, m.id, m.set_id, m.status, m.md5, m.artist, m.title,
                       m.version, m.creator, m.last_update, m.total_length, m.max_combo,
                       m.mode, m.bpm, m.cs, m.ar, m.od, m.hp, m.diff, m.owner_id,
                       ms.uploaded_by
                FROM maps m
                LEFT JOIN mapsets ms ON ms.id = m.set_id
                WHERE m.server = 'private'
                """,
            ),
        )
    ).mappings().all()
    return list(rows)


async def fetch_rank_history(conn: AsyncConnection) -> list[Mapping[str, Any]]:
    """Daily rank snapshots from bancho `ranking_history`, collapsed to one row
    per (user, mode, day) using that day's latest snapshot. Vanilla modes only.

    bancho records multiple snapshots per day; the lazer rank graph plots one
    point per day, so we keep each day's most-recent `global_rank`.
    """
    rows = (
        await conn.execute(
            text(
                """
                SELECT rh.user_id, rh.mode, DATE(rh.timestamp) AS d,
                       rh.global_rank, rh.country_rank
                FROM ranking_history rh
                JOIN (
                    SELECT user_id, mode, DATE(timestamp) AS d, MAX(timestamp) AS mt
                    FROM ranking_history
                    WHERE mode IN (0, 1, 2, 3) AND user_id <> 1
                    GROUP BY user_id, mode, DATE(timestamp)
                ) last
                  ON last.user_id = rh.user_id
                 AND last.mode = rh.mode
                 AND last.d = DATE(rh.timestamp)
                 AND last.mt = rh.timestamp
                ORDER BY rh.user_id, rh.mode, d
                """,
            ),
        )
    ).mappings().all()
    return list(rows)


async def fetch_clans(conn: AsyncConnection) -> list[Mapping[str, Any]]:
    """All bancho clans (-> g0v0 teams)."""
    rows = (
        await conn.execute(
            text(
                """
                SELECT id, name, tag, owner, created_at, description, default_gamemode
                FROM clans
                """,
            ),
        )
    ).mappings().all()
    return list(rows)


async def fetch_clan_members(conn: AsyncConnection) -> list[Mapping[str, Any]]:
    """(user_id, clan_id) for every user currently in a clan (-> team_members)."""
    rows = (
        await conn.execute(
            text("SELECT id AS user_id, clan_id FROM users WHERE clan_id > 0 AND id <> 1"),
        )
    ).mappings().all()
    return list(rows)


async def count_new_scores(conn: AsyncConnection, since_id: int) -> int:
    return int(
        await conn.scalar(
            text(
                "SELECT COUNT(*) FROM scores WHERE id > :since AND userid <> 1 "
                "AND mode IN (0,1,2,3,4,5,6,8)",
            ),
            {"since": since_id},
        )
        or 0,
    )


async def fetch_rx_ap_stats(conn: AsyncConnection, modes: tuple[int, ...]) -> list[Mapping[str, Any]]:
    """bancho `stats` rows for the given relax/autopilot modes (for users that
    exist), so the importer can mirror RX/AP pp/rank into lazer (the trigger
    bridge is vanilla-only)."""
    if not modes:
        return []
    mode_list = ", ".join(str(int(m)) for m in modes)
    rows = (
        await conn.execute(
            text(
                f"""
                SELECT st.id AS user_id, st.mode, st.pp, st.rscore, st.acc, st.tscore,
                       st.total_hits, st.max_combo, st.plays, st.playtime, st.replay_views,
                       st.x_count, st.xh_count, st.s_count, st.sh_count, st.a_count
                FROM stats st
                JOIN users u ON u.id = st.id
                WHERE st.id <> 1 AND st.mode IN ({mode_list})
                """,
            ),
        )
    ).mappings().all()
    return list(rows)
