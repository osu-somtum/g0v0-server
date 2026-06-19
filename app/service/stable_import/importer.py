"""Core stable->lazer score importer (backfill + incremental sync)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.config import settings

from app.database.beatmap import Beatmap
from app.database.beatmap_playcounts import process_beatmap_playcount
from app.database.beatmapset import Beatmapset
from app.database.best_scores import BestScore
from app.database.rank_history import RankHistory
from app.database.score import Score
from app.database.statistics import UserStatistics
from app.database.team import Team, TeamMember
from app.database.total_score_best_scores import TotalScoreBestScore
from app.database.user import User
from app.dependencies.database import engine, get_redis
from app.log import log
from app.models.score import GameMode, HitResult

SOMTUM_SET_ID_FLOOR = 100_000_000

from .bancho_db import (
    fetch_clan_members,
    fetch_clans,
    fetch_custom_maps,
    fetch_map_by_md5,
    fetch_new_scores,
    fetch_rank_history,
    fetch_rx_ap_stats,
    fetch_score_by_id,
    get_bancho_engine,
)
from .mappings import (
    bancho_mode_to_gamemode,
    custom_covers,
    custom_preview_url,
    grade_to_rank,
    int_mods_to_apimods,
    map_status_to_g0v0,
    osu_covers,
    standardised_total_score,
)
from .replay import build_osr, synthesize_relax_replay

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlmodel import col, select
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

    await _create_beatmap(session, m)
    cache[md5] = beatmap_id
    return beatmap_id


async def _create_beatmap(session: AsyncSession, m: Mapping[str, Any]) -> None:
    """Create g0v0 beatmap (+ its set if missing) from a bancho `maps` row. Flushes."""
    beatmap_id = int(m["id"])
    set_id = int(m["set_id"])
    # bancho `maps.owner_id` is the *per-difficulty* guest-mapper credit (NULL on the
    # common case = the set uploader). The set-level uploader lives in
    # `mapsets.uploaded_by`, which `fetch_custom_maps`/`fetch_map_by_md5` JOIN in.
    # Set `user_id` (uploader) = uploaded_by, falling back to owner_id, then 0; this
    # is what makes a custom set show up on its uploader's lazer profile tabs.
    uploaded_by = int(m["uploaded_by"]) if m.get("uploaded_by") is not None else None
    owner = int(m["owner_id"]) if m["owner_id"] is not None else 0
    set_owner = uploaded_by if uploaded_by is not None else owner
    # per-difficulty owner: the guest mapper if credited, else the set uploader.
    diff_owner = owner if m["owner_id"] is not None else set_owner
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
                user_id=set_owner,
                video=False,
                beatmap_status=map_status_to_g0v0(int(m["status"])),
                covers=osu_covers(set_id) if is_osu else custom_covers(set_id, str(settings.server_url)),
                preview_url=f"//b.ppy.sh/preview/{set_id}.mp3"
                if is_osu
                else custom_preview_url(set_id, str(settings.server_url)),
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
            user_id=diff_owner,
            version=m["version"],
            url=f"https://osu.ppy.sh/beatmaps/{beatmap_id}",
            checksum=m["md5"],
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


async def import_custom_beatmaps(session: AsyncSession, conn: AsyncConnection) -> int:
    """Bridge ALL somtum custom (server='private') maps into g0v0 so they're
    browseable/searchable in lazer (osu!'s API has no record of them). Idempotent."""
    created = 0
    rows = await fetch_custom_maps(conn)
    for m in rows:
        if await session.get(Beatmap, int(m["id"])) is None:
            await _create_beatmap(session, m)
            created += 1
    return created


def _statistics_for_row(row: Mapping[str, Any]) -> dict[str, int]:
    """bancho hit-counts -> lazer HitResult statistics (snake_case keys)."""
    return {
        "great": int(row["n300"]),
        "ok": int(row["n100"]),
        "meh": int(row["n50"]),
        "miss": int(row["nmiss"]),
        "perfect": int(row["ngeki"]),
        "good": int(row["nkatu"]),
    }


def _synthesize_relax(raw_replay: bytes, beatmap_id: int, row: Mapping[str, Any]) -> bytes:
    """Inject relax taps into a replay using the cached bancho .osu (if present),
    reproducing the score's 300/100/50/miss distribution so it re-judges to the
    same accuracy as the stable score."""
    osu_path = Path(settings.bancho_osu_dir) / f"{beatmap_id}.osu"
    if not osu_path.is_file():
        return raw_replay
    try:
        osu_text = osu_path.read_text(encoding="utf-8", errors="ignore")
        return synthesize_relax_replay(
            raw_replay,
            osu_text,
            mods_bitmask=int(row["mods"]),
            n300=int(row["n300"]),
            n100=int(row["n100"]),
            n50=int(row["n50"]),
            nmiss=int(row["nmiss"]),
        )
    except (OSError, ValueError) as e:
        logger.warning("Relax replay synth failed for beatmap {b}: {e}", b=beatmap_id, e=e)
        return raw_replay


def _build_osr_for(
    row: Mapping[str, Any],
    score: Score,
    beatmap_id: int,
    standardised: int,
    apimods: list[Any],
    rank: Any,
    raw_replay: bytes,
) -> bytes:
    """Wrap bancho's raw replay payload in a full lazer-recognised `.osr`."""
    total_hits = (
        int(row["n300"]) + int(row["n100"]) + int(row["n50"]) + int(row["nmiss"]) + int(row["ngeki"]) + int(row["nkatu"])
    )
    # osu! relax replays have no recorded taps (relax auto-tapped live and stable
    # never wrote the presses). Synthesize them from the beatmap so lazer replays
    # the score as hits instead of all-misses. osu!standard relax only.
    if score.gamemode == GameMode.OSURX:
        raw_replay = _synthesize_relax(raw_replay, beatmap_id, row)
    return build_osr(
        # lazer rulesets are 0-3; RX/AP collapse to their base ruleset.
        mode=int(score.gamemode),
        beatmap_md5=row["map_md5"],
        username=str(row.get("username") or ""),
        n300=int(row["n300"]),
        n100=int(row["n100"]),
        n50=int(row["n50"]),
        ngeki=int(row["ngeki"]),
        nkatu=int(row["nkatu"]),
        nmiss=int(row["nmiss"]),
        header_score=standardised,
        max_combo=int(row["max_combo"]),
        perfect=bool(int(row["perfect"])),
        mods_bitmask=int(row["mods"]),
        played_at=row["play_time"],
        raw_replay=raw_replay,
        online_id=score.id,
        user_id=int(row["userid"]),
        rank=rank.value if hasattr(rank, "value") else str(rank),
        api_mods=apimods,
        statistics=_statistics_for_row(row),
        maximum_statistics={"great": total_hits},
        total_score_without_mods=standardised,
    )


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
    gamemode = bancho_mode_to_gamemode(int(row["mode"]))
    if gamemode is None:
        return False
    # Relax/autopilot are gated by the same flags g0v0 uses; skip if disabled.
    if gamemode in (GameMode.OSURX, GameMode.TAIKORX, GameMode.FRUITSRX) and not settings.enable_rx:
        return False
    if gamemode == GameMode.OSUAP and not settings.enable_ap:
        return False
    base_mode = int(gamemode)  # 0-3 ruleset id (RX/AP collapse to their base)
    apimods = int_mods_to_apimods(int(row["mods"]))
    rank = grade_to_rank(row["grade"])
    osr_src = Path(settings.bancho_osr_dir) / f"{int(row['id'])}.osr"
    has_replay = osr_src.is_file()
    total_hits = int(row["n300"]) + int(row["n100"]) + int(row["n50"]) + int(row["nmiss"]) + int(row["ngeki"]) + int(
        row["nkatu"]
    )
    bancho_score = int(row["score"])
    # g0v0/lazer's `total_score` is a *standardised* score (0..MAX_SCORE=1,000,000);
    # the client derives both its standardised and in-game classic numbers from it
    # (a raw stable score here explodes to billions). Convert with osu!'s per-ruleset
    # accuracy/combo split (see standardised_total_score). The real stable score is
    # kept in `classic_total_score`.
    acc_fraction = float(row["acc"]) / 100.0
    bm = await session.get(Beatmap, beatmap_id)
    bm_max_combo = int(bm.max_combo) if bm is not None and bm.max_combo else 0
    standardised = standardised_total_score(base_mode, acc_fraction, int(row["max_combo"]), bm_max_combo)

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
        map_md5=row["map_md5"],
        mods=apimods,
        n300=int(row["n300"]),
        n100=int(row["n100"]),
        n50=int(row["n50"]),
        nmiss=int(row["nmiss"]),
        ngeki=int(row["ngeki"]),
        nkatu=int(row["nkatu"]),
        maximum_statistics={HitResult.GREAT: total_hits},
        total_score=standardised,
        total_score_without_mods=standardised,
        classic_total_score=bancho_score,
        preserve=passed,
        processed=True,
        ranked=True,
        has_replay=has_replay,
    )
    session.add(score)
    await session.flush()  # populate score.id

    # Bridge the replay so the score is watchable from leaderboards. bancho stores
    # ONLY the raw replay payload (no .osr header); lazer needs a full .osr, so wrap
    # it with a proper lazer-version header (+ score-info block carrying online_id /
    # user_id so lazer binds it to the leaderboard score & player).
    if has_replay:
        try:
            raw = osr_src.read_bytes()
            osr = _build_osr_for(row, score, beatmap_id, standardised, apimods, rank, raw)
            dest = Path(settings.stable_replay_dir) / f"{score.id}_{beatmap_id}_{int(row['userid'])}_lazer_replay.osr"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(osr)
        except OSError as e:
            logger.warning("Replay bridge failed for score {sid}: {e}", sid=score.id, e=e)

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
                total_score=standardised,
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

            # Bridge all custom (somtum) maps so they're browseable in lazer,
            # independent of whether anyone has scores on them.
            if not dry_run:
                custom = await import_custom_beatmaps(session, conn)
                await session.commit()
                if custom:
                    logger.info("Bridged {n} custom beatmaps into lazer.", n=custom)

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


async def rebuild_replays() -> dict[str, int]:
    """Regenerate the `.osr` for every already-imported score that has a replay.

    Non-destructive: only rewrites replay files (no DB writes). Use after changing
    the `.osr` format (e.g. lazer-version header + score-info block) so existing
    imported scores get watchable, leaderboard-bound replays without a full re-import.
    """
    bancho_engine = get_bancho_engine()
    rebuilt = missing = 0
    async with AsyncSession(engine) as session, bancho_engine.connect() as conn:
        await _ensure_state_table(session)
        pairs = (await session.execute(text("SELECT bancho_id, lazer_id FROM stable_score_map"))).all()
        for bancho_id, lazer_id in pairs:
            osr_src = Path(settings.bancho_osr_dir) / f"{int(bancho_id)}.osr"
            if not osr_src.is_file():
                missing += 1
                continue
            row = await fetch_score_by_id(conn, int(bancho_id))
            score = await session.get(Score, int(lazer_id))
            if row is None or score is None:
                continue
            try:
                raw = osr_src.read_bytes()
                apimods = int_mods_to_apimods(int(row["mods"]))
                osr = _build_osr_for(row, score, score.beatmap_id, int(score.total_score), apimods, score.rank, raw)
                dest = (
                    Path(settings.stable_replay_dir)
                    / f"{score.id}_{score.beatmap_id}_{score.user_id}_lazer_replay.osr"
                )
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(osr)
                rebuilt += 1
            except OSError as e:
                logger.warning("Replay rebuild failed for score {sid}: {e}", sid=score.id, e=e)
    logger.info("Replay rebuild done | rebuilt={rebuilt} missing_src={missing}", rebuilt=rebuilt, missing=missing)
    return {"rebuilt": rebuilt, "missing": missing}


async def recompute_scores() -> dict[str, int]:
    """Recompute the standardised `total_score` of every already-imported score
    with the current conversion formula, mirroring it into the leaderboard
    best-score rows. DB-only; use after changing `standardised_total_score` so
    existing scores get the new value without a destructive re-import.
    """
    updated = 0
    async with AsyncSession(engine) as session:
        await _ensure_state_table(session)
        ids = (await session.execute(text("SELECT lazer_id FROM stable_score_map"))).scalars().all()
        for sid in ids:
            score = await session.get(Score, int(sid))
            if score is None:
                continue
            bm = await session.get(Beatmap, score.beatmap_id)
            bm_max_combo = int(bm.max_combo) if bm is not None and bm.max_combo else 0
            std = standardised_total_score(
                int(score.gamemode),
                float(score.accuracy),
                int(score.max_combo),
                bm_max_combo,
            )
            if score.total_score != std or score.total_score_without_mods != std:
                score.total_score = std
                score.total_score_without_mods = std
                session.add(score)
                updated += 1
            await session.execute(
                text("UPDATE total_score_best_scores SET total_score = :s WHERE score_id = :id"),
                {"s": std, "id": int(sid)},
            )
        await session.commit()
    logger.info("Recomputed standardised scores | updated={u}", u=updated)
    return {"updated": updated}


async def refresh_custom_covers() -> dict[str, int]:
    """Point every somtum custom beatmapset's covers at this server's `/somtum/bg`
    route, so existing custom sets (created before covers were bridged) show their
    background/thumbnail in lazer. Idempotent."""
    base_url = str(settings.server_url)
    updated = 0
    async with AsyncSession(engine) as session:
        sets = (
            await session.exec(select(Beatmapset).where(Beatmapset.id >= SOMTUM_SET_ID_FLOOR))
        ).all()
        for bs in sets:
            covers = custom_covers(int(bs.id), base_url)
            if bs.covers != covers:
                bs.covers = covers
                session.add(bs)
                updated += 1
        await session.commit()
    logger.info("Refreshed custom covers | updated={u}", u=updated)
    return {"updated": updated}


async def refresh_custom_previews() -> dict[str, int]:
    """Point every somtum custom beatmapset's `preview_url` at this server's
    `/somtum/preview` route. Early custom sets were bridged with `preview_url=''`
    (osu!'s b.ppy.sh preview CDN has nothing for id >= 1e8), so lazer's in-client
    song preview did nothing. bancho stores the preview audio locally; serve it.
    Idempotent."""
    base_url = str(settings.server_url)
    updated = 0
    async with AsyncSession(engine) as session:
        sets = (
            await session.exec(select(Beatmapset).where(Beatmapset.id >= SOMTUM_SET_ID_FLOOR))
        ).all()
        for bs in sets:
            preview = custom_preview_url(int(bs.id), base_url)
            if bs.preview_url != preview:
                bs.preview_url = preview
                session.add(bs)
                updated += 1
        await session.commit()
    logger.info("Refreshed custom previews | updated={u}", u=updated)
    return {"updated": updated}


async def refresh_user_banners() -> dict[str, int]:
    """Bridge each user's website banner into their lazer profile `cover`.

    The bancho-side user-sync trigger fills `avatar_url` but leaves `cover` as
    `{"url": ""}`, so lazer profiles render no banner. Point every user's
    `cover.url` at this server's `/somtum/user/{id}/banner` route — which serves
    the player's own website banner if they uploaded one, else the shared
    default.jpeg — so every profile renders a banner. Doesn't clobber a custom,
    non-empty cover the player set in lazer (some other, non-/somtum url).
    Idempotent.
    """
    base = str(settings.server_url).rstrip("/")
    updated = 0
    async with AsyncSession(engine) as session:
        users = (await session.exec(select(User))).all()
        for u in users:
            want = f"{base}/somtum/user/{int(u.id)}/banner"
            current = (u.cover or {}).get("url", "") if isinstance(u.cover, dict) else ""
            if current == want:
                continue
            # Only fill an empty/own-route cover; don't clobber a custom one the
            # user set in lazer (some other non-empty, non-/somtum url).
            if current and "/somtum/user/" not in current:
                continue
            u.cover = {"url": want}
            session.add(u)
            updated += 1
        await session.commit()
    logger.info("Refreshed user banners | updated={u}", u=updated)
    return {"updated": updated}


async def refresh_custom_owners() -> dict[str, int]:
    """Re-attribute every somtum custom beatmapset to its real uploader.

    Early custom sets were bridged with `user_id = maps.owner_id or 0`, but
    `owner_id` is the *per-difficulty* guest credit (NULL = uploader) — so those
    sets landed with `user_id = 0` and never appeared on their uploader's lazer
    profile tabs. The true uploader is bancho `mapsets.uploaded_by`. This reads
    that and fixes `beatmapsets.user_id` (and the set's beatmaps, where they still
    point at the old set owner so genuine guest-diff credits aren't clobbered).
    Idempotent.
    """
    bancho_engine = get_bancho_engine()
    updated_sets = updated_maps = 0
    async with AsyncSession(engine) as session, bancho_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, uploaded_by FROM mapsets "
                    "WHERE server = 'private' AND uploaded_by IS NOT NULL"
                ),
            )
        ).mappings().all()
        for row in rows:
            set_id = int(row["id"])
            uploader = int(row["uploaded_by"])
            bs = await session.get(Beatmapset, set_id)
            if bs is None or int(bs.user_id) == uploader:
                continue
            old_owner = int(bs.user_id)
            bs.user_id = uploader
            session.add(bs)
            updated_sets += 1
            # Re-attribute the set's beatmaps that still credit the old set owner
            # (leave any distinct per-difficulty guest mapper untouched).
            beatmaps = (
                await session.exec(
                    select(Beatmap).where(
                        Beatmap.beatmapset_id == set_id,
                        col(Beatmap.user_id) == old_owner,
                    )
                )
            ).all()
            for bm in beatmaps:
                bm.user_id = uploader
                session.add(bm)
                updated_maps += 1
        await session.commit()
    logger.info("Refreshed custom owners | sets={s} maps={m}", s=updated_sets, m=updated_maps)
    return {"updated_sets": updated_sets, "updated_maps": updated_maps}


async def sync_rank_history() -> dict[str, int]:
    """Bridge bancho `ranking_history` -> g0v0 `rank_history` (the profile rank
    graph). Copies each user's daily global_rank for vanilla modes; idempotent
    (upsert per user/mode/day). g0v0 also self-records a row for "today" when a
    profile is viewed, so this mainly backfills the historical curve.
    """
    bancho_engine = get_bancho_engine()
    inserted = updated = skipped_no_user = 0
    user_exists: dict[int, bool] = {}
    async with AsyncSession(engine) as session, bancho_engine.connect() as conn:
        rows = await fetch_rank_history(conn)
        for row in rows:
            uid = int(row["user_id"])
            rank = int(row["global_rank"])
            day = row["d"]
            mode = GameMode.from_int(int(row["mode"]))

            if uid not in user_exists:
                user_exists[uid] = (await session.get(User, uid)) is not None
            if not user_exists[uid]:
                skipped_no_user += 1
                continue

            existing = (
                await session.exec(
                    select(RankHistory).where(
                        RankHistory.user_id == uid,
                        RankHistory.mode == mode,
                        RankHistory.date == day,
                    )
                )
            ).first()
            if existing is None:
                session.add(RankHistory(user_id=uid, mode=mode, rank=rank, date=day))
                inserted += 1
            elif existing.rank != rank:
                existing.rank = rank
                session.add(existing)
                updated += 1
        await session.commit()
    logger.info(
        "Rank history bridge | inserted={i} updated={u} skipped_no_user={s}",
        i=inserted,
        u=updated,
        s=skipped_no_user,
    )
    return {"inserted": inserted, "updated": updated, "skipped_no_user": skipped_no_user}


async def sync_rx_stats() -> dict[str, int]:
    """Mirror bancho relax/autopilot `stats` (pp/score/acc/grades) into g0v0
    `lazer_user_statistics` for the RX/AP gamemodes, so those profiles show real
    Akatsuki pp + rank. The trigger bridge is vanilla-only; this is the RX/AP
    companion (g0v0 derives RX rank at read time from the per-mode pp). Idempotent.
    """
    modes: list[int] = []
    if settings.enable_rx:
        modes += [4, 5, 6]  # rx!std / rx!taiko / rx!catch
    if settings.enable_ap:
        modes += [8]  # ap!std
    if not modes:
        return {"updated": 0, "inserted": 0, "skipped_no_user": 0}

    bancho_engine = get_bancho_engine()
    updated = inserted = skipped_no_user = 0
    user_exists: dict[int, bool] = {}
    async with AsyncSession(engine) as session, bancho_engine.connect() as conn:
        rows = await fetch_rx_ap_stats(conn, tuple(modes))
        for row in rows:
            gamemode = bancho_mode_to_gamemode(int(row["mode"]))
            if gamemode is None:
                continue
            uid = int(row["user_id"])
            if uid not in user_exists:
                user_exists[uid] = (await session.get(User, uid)) is not None
            if not user_exists[uid]:
                skipped_no_user += 1
                continue

            stat = (
                await session.exec(
                    select(UserStatistics).where(
                        UserStatistics.user_id == uid,
                        UserStatistics.mode == gamemode,
                    )
                )
            ).first()
            # Only bancho-owned columns are written (g0v0 owns level_current/is_ranked),
            # matching the vanilla `stats` trigger.
            fields = {
                "pp": float(row["pp"]),
                "ranked_score": int(row["rscore"]),
                "hit_accuracy": float(row["acc"]),
                "total_score": int(row["tscore"]),
                "total_hits": int(row["total_hits"]),
                "maximum_combo": int(row["max_combo"]),
                "play_count": int(row["plays"]),
                "play_time": int(row["playtime"]),
                "replays_watched_by_others": int(row["replay_views"]),
                "grade_ss": int(row["x_count"]),
                "grade_ssh": int(row["xh_count"]),
                "grade_s": int(row["s_count"]),
                "grade_sh": int(row["sh_count"]),
                "grade_a": int(row["a_count"]),
            }
            if stat is None:
                session.add(UserStatistics(user_id=uid, mode=gamemode, is_ranked=True, **fields))
                inserted += 1
            else:
                changed = any(getattr(stat, k) != v for k, v in fields.items())
                if changed:
                    for k, v in fields.items():
                        setattr(stat, k, v)
                    session.add(stat)
                    updated += 1
        await session.commit()
    logger.info(
        "RX/AP stats bridge | updated={u} inserted={i} skipped_no_user={s}",
        u=updated,
        i=inserted,
        s=skipped_no_user,
    )
    return {"updated": updated, "inserted": inserted, "skipped_no_user": skipped_no_user}


_CLAN_IMG_EXTS = ("png", "jpg", "jpeg", "gif", "webp")


def _clan_has_file(subdirs: tuple[str, ...], clan_id: int) -> bool:
    base = Path(settings.bancho_clan_assets_dir)
    return any((base / sub / f"{clan_id}.{ext}").is_file() for sub in subdirs for ext in _CLAN_IMG_EXTS)


def _clan_flag_url(clan_id: int) -> str | None:
    """Team flag URL: a clan-specific avatar, or the shared default.jpg fallback so
    every team renders something. None only if there's no default either."""
    base = Path(settings.bancho_clan_assets_dir)
    has_default = (base / "avatar" / "default.jpg").is_file() or (base / "avatars" / "default.jpg").is_file()
    if _clan_has_file(("avatars", "avatar"), clan_id) or has_default:
        return f"{str(settings.server_url).rstrip('/')}/somtum/team/{clan_id}/flag"
    return None


def _clan_cover_url(clan_id: int) -> str | None:
    if _clan_has_file(("banners",), clan_id):
        return f"{str(settings.server_url).rstrip('/')}/somtum/team/{clan_id}/banner"
    return None


async def sync_teams() -> dict[str, int]:
    """Bridge bancho clans -> g0v0 teams so a player's clan shows as their team in
    lazer. The g0v0 team id IS the bancho clan id (they match). Mirrors
    name/tag/owner/avatar/banner into `teams` and clan membership into
    `team_members`; removes memberships for users who left their clan. Idempotent.
    """
    bancho_engine = get_bancho_engine()
    teams_upserted = members_added = members_removed = 0
    async with AsyncSession(engine) as session, bancho_engine.connect() as conn:
        clans = await fetch_clans(conn)
        clan_ids: set[int] = set()
        for clan in clans:
            team_id = int(clan["id"])  # team id == clan id
            clan_ids.add(team_id)
            leader_id = int(clan["owner"])
            if (await session.get(User, leader_id)) is None:
                continue  # leader must exist (FK)
            gm = int(clan["default_gamemode"] or 0)
            playmode = GameMode.from_int(gm) if gm in (0, 1, 2, 3) else GameMode.OSU
            flag_url = _clan_flag_url(team_id)
            cover_url = _clan_cover_url(team_id)
            team = await session.get(Team, team_id)
            if team is None:
                session.add(
                    Team(
                        id=team_id,
                        name=str(clan["name"])[:100],
                        short_name=str(clan["tag"])[:10],
                        leader_id=leader_id,
                        created_at=clan["created_at"],
                        description=clan["description"],
                        playmode=playmode,
                        flag_url=flag_url,
                        cover_url=cover_url,
                    )
                )
                teams_upserted += 1
            else:
                team.name = str(clan["name"])[:100]
                team.short_name = str(clan["tag"])[:10]
                team.leader_id = leader_id
                team.description = clan["description"]
                team.playmode = playmode
                team.flag_url = flag_url
                team.cover_url = cover_url
                session.add(team)
        await session.commit()

        # Membership: add/move current clan members, drop those who left.
        rows = await fetch_clan_members(conn)
        wanted: dict[int, int] = {}  # user_id -> team_id (= clan id)
        for r in rows:
            tid = int(r["clan_id"])
            if tid in clan_ids:
                wanted[int(r["user_id"])] = tid

        for uid, tid in wanted.items():
            if (await session.get(User, uid)) is None:
                continue
            existing = await session.get(TeamMember, uid)
            if existing is None:
                session.add(TeamMember(user_id=uid, team_id=tid))
                members_added += 1
            elif existing.team_id != tid:
                existing.team_id = tid
                session.add(existing)

        # Remove memberships of clan-derived teams whose user left that clan.
        # Scope strictly to clan ids so native lazer teams are never touched.
        if clan_ids:
            bridged = (
                await session.exec(select(TeamMember).where(col(TeamMember.team_id).in_(clan_ids)))
            ).all()
            for tm in bridged:
                if wanted.get(int(tm.user_id)) != int(tm.team_id):
                    await session.delete(tm)
                    members_removed += 1
        await session.commit()

    logger.info(
        "Teams bridge | teams={t} members_added={a} members_removed={r}",
        t=teams_upserted,
        a=members_added,
        r=members_removed,
    )
    return {"teams": teams_upserted, "members_added": members_added, "members_removed": members_removed}
