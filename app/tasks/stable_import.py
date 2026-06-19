"""Scheduled incremental import of osu!stable (bancho.py) scores into lazer.

Gated by `enable_stable_score_import`. Runs `sync_new()` every
`stable_import_interval_seconds` so new stable plays appear on lazer profiles
(Recent / Ranks / Most Played). One-time catch-up is the `backfill()` CLI
(`scripts/import_stable_scores.py`). See app/service/stable_import.
"""

from __future__ import annotations

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
