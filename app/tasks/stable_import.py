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
        from app.service.stable_import import sync_new

        try:
            await sync_new()
        except Exception:
            logger.exception("Stable score sync failed")
