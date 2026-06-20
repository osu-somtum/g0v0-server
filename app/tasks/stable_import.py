"""Scheduled incremental import of osu!stable (bancho.py) scores into lazer.

Gated by `enable_stable_score_import`. Runs `sync_new()` every
`stable_import_interval_seconds` so new stable plays appear on lazer profiles
(Recent / Ranks / Most Played). Also subscribes to the `somtum:new_score`
Redis channel so bancho.py can trigger an immediate import on score submission.
One-time catch-up is the `backfill()` CLI (`scripts/import_stable_scores.py`).
See app/service/stable_import.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from app.config import settings
from app.dependencies.scheduler import get_scheduler
from app.log import log

logger = log("StableImport")

if settings.enable_stable_score_import:

    @get_scheduler().scheduled_job(
        "interval",
        id="stable_score_import",
        seconds=settings.stable_import_interval_seconds,
        next_run_time=datetime.now() + timedelta(seconds=30),
    )
    async def stable_score_import_job() -> None:
        from app.service.stable_import import (
            refresh_custom_covers,
            refresh_custom_owners,
            refresh_custom_previews,
            refresh_user_banners,
            sync_new,
            sync_rank_history,
            sync_rx_stats,
            sync_teams,
        )

        try:
            await sync_new()
        except Exception:
            logger.exception("Stable score sync failed")
        try:
            await sync_rank_history()
        except Exception:
            logger.exception("Stable rank-history sync failed")
        try:
            await sync_rx_stats()
        except Exception:
            logger.exception("Stable RX/AP stats sync failed")
        try:
            await refresh_custom_covers()
        except Exception:
            logger.exception("Stable custom-cover refresh failed")
        try:
            await refresh_custom_previews()
        except Exception:
            logger.exception("Stable custom-preview refresh failed")
        try:
            await refresh_user_banners()
        except Exception:
            logger.exception("Stable user-banner refresh failed")
        try:
            await refresh_custom_owners()
        except Exception:
            logger.exception("Stable custom-owner refresh failed")
        try:
            await sync_teams()
        except Exception:
            logger.exception("Stable teams sync failed")

    async def _score_notify_listener() -> None:
        """Subscribe to `somtum:new_score` and trigger sync_new() immediately
        when bancho.py publishes a new score, so the leaderboard updates within
        seconds instead of waiting for the next scheduled poll."""
        from app.dependencies.database import get_redis_pubsub
        from app.service.stable_import import sync_new

        pubsub = get_redis_pubsub()
        await pubsub.subscribe("somtum:new_score")
        logger.info("Subscribed to somtum:new_score for real-time score sync")
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                await sync_new()
            except Exception:
                logger.exception("Real-time score sync failed")

    @get_scheduler().scheduled_job(
        "date",
        id="start_score_notify_listener",
        run_date=datetime.now() + timedelta(seconds=10),
    )
    async def _boot_score_notify_listener() -> None:
        asyncio.create_task(_score_notify_listener())
