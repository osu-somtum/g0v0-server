"""Core stable->lazer score importer (backfill + incremental sync)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.database.beatmap import Beatmap
from app.database.beatmap_playcounts import process_beatmap_playcount
from app.database.beatmapset import Beatmapset
from app.database.best_scores import BestScore
from app.database.score import Score
from app.database.total_score_best_scores import TotalScoreBestScore
from app.dependencies.database import engine, get_redis
from app.log import log
from app.models.score import GameMode, HitResult

from .bancho_db import fetch_map_by_md5, fetch_new_scores, get_bancho_engine
from .mappings import grade_to_rank, int_mods_to_apimods, map_status_to_g0v0, osu_covers

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlmodel.ext.asyncio.session import AsyncSession

logger = log("StableImport")

_STATE_DDL = """
CREATE TABLE IF NOT EXISTS stable_score_map (
    bancho_id BIGINT NOT NULL PRIMARY KEY,
    lazer_id  BIGINT NOT NULL,
    INDEX idx_stable_score_map_lazer (lazer_id)
)
"""


async def _ensure_state_table(session: AsyncSession) -> None:
    await session.execute(text(_STATE_DDL))


async def _last_imported_id(session: AsyncSession) -> int:
    return int(await session.scalar(text("SELECT COALESCE(MAX(bancho_id), 0) FROM stable_score_map")) or 0)


async def _ensure_beatmap(
    session: AsyncSession,
    conn: AsyncConnection,
    md5: str,
    cache: dict[str, int | None],
) -> int | None:
    """Ensure the g0v0 beatmap (+ its set) for a bancho map md5 exists; return its id.

    Returns None when bancho has no `maps` row for the md5 (the player played a map
    bancho never cached) — the score is then skipped (can't satisfy the FK).
    """
    if md5 in cache:
        return cache[md5]

    m = await fetch_map_by_md5(conn, md5)
    if m is None:
        cache[md5] = None
        return None

    beatmap_id = int(m["id"])
    existing = await session.get(Beatmap, beatmap_id)
    if existing is not None:
        cache[md5] = beatmap_id
        return beatmap_id

    set_id = int(m["set_id"])
    owner = int(m["owner_id"]) if m["owner_id"] is not None else 0
    is_osu = m["server"] == "osu!"

    if await session.get(Beatmapset, set_id) is None:
        session.add(
            Beatmapset(
                id=set_id,
                artist=m["artist"],
                artist_unicode=m["artist"],
                title=m["title"],
                title_unicode=m["title"],
                creator=m["creator"],
                user_id=owner,
                video=False,
                beatmap_status=map_status_to_g0v0(int(m["status"])),
                covers=osu_covers(set_id) if is_osu else None,
                preview_url=f"//b.ppy.sh/preview/{set_id}.mp3" if is_osu else "",
                last_updated=m["last_update"],
                submitted_date=m["last_update"],
            ),
        )
        await session.flush()

    session.add(
        Beatmap(
            id=beatmap_id,
            beatmapset_id=set_id,
            mode=GameMode.from_int(int(m["mode"])),
            total_length=int(m["total_length"]),
            user_id=owner,
            version=m["version"],
            url=f"https://osu.ppy.sh/beatmaps/{beatmap_id}",
            checksum=md5,
            last_updated=m["last_update"],
            beatmap_status=map_status_to_g0v0(int(m["status"])),
            difficulty_rating=float(m["diff"]),
            max_combo=int(m["max_combo"]),
            ar=float(m["ar"]),
            cs=float(m["cs"]),
            drain=float(m["hp"]),
            accuracy=float(m["od"]),
            bpm=float(m["bpm"]),
        ),
    )
    await session.flush()
    cache[md5] = beatmap_id
    return beatmap_id


async def _import_one(
    session: AsyncSession,
    conn: AsyncConnection,
    row: Mapping[str, Any],
    cache: dict[str, int | None],
) -> bool:
    """Import a single bancho score row. Returns True if a score was created."""
    beatmap_id = await _ensure_beatmap(session, conn, row["map_md5"], cache)
    if beatmap_id is None:
        return False

    passed = int(row["status"]) > 0
    gamemode = GameMode.from_int(int(row["mode"]))
    apimods = int_mods_to_apimods(int(row["mods"]))
    rank = grade_to_rank(row["grade"])
    total_hits = int(row["n300"]) + int(row["n100"]) + int(row["n50"]) + int(row["nmiss"]) + int(row["ngeki"]) + int(
        row["nkatu"]
    )
    bancho_score = int(row["score"])

    score = Score(
        beatmap_id=beatmap_id,
        user_id=int(row["userid"]),
        gamemode=gamemode,
        type="solo",
        rank=rank,
        accuracy=float(row["acc"]) / 100.0,
        max_combo=int(row["max_combo"]),
        passed=passed,
        pp=float(row["pp"]),  # Akatsuki pp, copied verbatim
        started_at=row["play_time"],
        ended_at=row["play_time"],
        has_replay=False,
        map_md5=row["map_md5"],
        mods=apimods,
        n300=int(row["n300"]),
        n100=int(row["n100"]),
        n50=int(row["n50"]),
        nmiss=int(row["nmiss"]),
        ngeki=int(row["ngeki"]),
        nkatu=int(row["nkatu"]),
        maximum_statistics={HitResult.GREAT: total_hits},
        total_score=bancho_score,
        total_score_without_mods=bancho_score,
        classic_total_score=bancho_score,
        preserve=passed,
        processed=True,
        ranked=True,
    )
    session.add(score)
    await session.flush()  # populate score.id

    await session.execute(
        text("INSERT INTO stable_score_map (bancho_id, lazer_id) VALUES (:b, :l)"),
        {"b": int(row["id"]), "l": score.id},
    )

    # bancho status 2 = the user's best on this map. Populate both best-score tables:
    #  - best_scores (pp-based) → Ranks tab / weighted-pp list. Raw-insert WITHOUT
    #    BestScore.update() (which would recompute & overwrite lazer_user_statistics.pp).
    #  - total_score_best_scores (total-score) → beatmap leaderboards + #1-rank counts.
    if int(row["status"]) == 2:
        session.add(
            BestScore(
                user_id=int(row["userid"]),
                score_id=score.id,
                beatmap_id=beatmap_id,
                gamemode=gamemode,
                pp=float(row["pp"]),
                acc=float(row["acc"]) / 100.0,
            ),
        )
        session.add(
            TotalScoreBestScore(
                user_id=int(row["userid"]),
                score_id=score.id,
                beatmap_id=beatmap_id,
                gamemode=gamemode,
                total_score=bancho_score,
                mods=[m["acronym"] for m in apimods],
                rank=rank,
            ),
        )

    # Most Played
    if passed:
        await process_beatmap_playcount(session, int(row["userid"]), beatmap_id)

    return True


_LOCK_KEY = "stable_import:lock"


async def _run(dry_run: bool, from_zero: bool) -> dict[str, int]:
    bancho_engine = get_bancho_engine()
    created = skipped_no_map = 0

    # Single-runner lock so the periodic sync and a manual backfill can't race
    # (concurrent inserts would collide on beatmap/beatmapset PKs).
    redis = get_redis()
    if not await redis.set(_LOCK_KEY, "1", nx=True, ex=900):
        logger.info("Stable import already running (lock held); skipping this run.")
        return {"created": 0, "skipped_no_map": 0, "locked": 1}
    try:
        async with AsyncSession(engine) as session, bancho_engine.connect() as conn:
            await _ensure_state_table(session)
            await session.commit()
            since = 0 if from_zero else await _last_imported_id(session)
            start_since = since

            cache: dict[str, int | None] = {}
            while True:
                rows = await fetch_new_scores(conn, since)
                if not rows:
                    break
                for row in rows:
                    # On a from-zero backfill, skip ids already mapped (idempotent re-run).
                    if from_zero:
                        exists = await session.scalar(
                            text("SELECT 1 FROM stable_score_map WHERE bancho_id = :b"),
                            {"b": int(row["id"])},
                        )
                        if exists:
                            since = int(row["id"])
                            continue
                    if dry_run:
                        m = await fetch_map_by_md5(conn, row["map_md5"])
                        if m is None:
                            skipped_no_map += 1
                        else:
                            created += 1
                    else:
                        if await _import_one(session, conn, row, cache):
                            created += 1
                        else:
                            skipped_no_map += 1
                    since = int(row["id"])
                # dry-run pages via the advanced cursor without persisting.
                if not dry_run:
                    await session.commit()

            logger.info(
                "Stable import {mode} | from_id={start} created={created} skipped_no_map={skipped}",
                mode="DRY-RUN" if dry_run else ("backfill" if from_zero else "sync"),
                start=start_since,
                created=created,
                skipped=skipped_no_map,
            )
    finally:
        await redis.delete(_LOCK_KEY)

    return {"created": created, "skipped_no_map": skipped_no_map}


async def backfill(dry_run: bool = False) -> dict[str, int]:
    """Import all not-yet-imported bancho scores (from id 0). Idempotent."""
    return await _run(dry_run=dry_run, from_zero=True)


async def sync_new() -> dict[str, int]:
    """Incrementally import bancho scores newer than the last imported id."""
    return await _run(dry_run=False, from_zero=False)
