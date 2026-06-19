"""Somtum dual-bancho: import osu!stable (bancho.py) scores into g0v0.

Bridges bancho.py's `scores`/`maps` (in the shared `freedomdive_db`) into g0v0's
lazer `scores`/`beatmaps`/`beatmapsets` + `best_scores`/`beatmap_playcounts`, so a
shared account's osu!lazer profile shows real Recent plays, Ranks (top), and Most
Played. Vanilla modes (0-3) only.

Design notes:
- This is a Python importer (NOT a MySQL trigger): the conversion is too complex
  for SQL (int mod bitmask -> lazer APIMod list + CL, hit-counts -> statistics,
  rank, lazer scoring, dependent best/playcount rows).
- **pp identity stays Akatsuki**: each score's pp is copied verbatim from bancho
  `scores.pp`, and `best_scores` rows are raw-inserted WITHOUT calling
  `BestScore.update()` (which would recompute/overwrite `lazer_user_statistics.pp`).
  The user's total pp keeps coming from the `somtum_stats_*` bridge triggers.
- Idempotent: a `stable_score_map(bancho_id -> lazer_id)` table in the lazer DB
  records what's been imported; backfill starts from 0, incremental sync from the
  last imported bancho score id.

See DUAL_BANCHO_PLAN.md and the `dual-bancho-lazer` skill.
"""

from __future__ import annotations

from .importer import (
    backfill,
    rebuild_replays,
    recompute_scores,
    refresh_custom_covers,
    refresh_custom_owners,
    refresh_custom_previews,
    refresh_user_banners,
    sync_new,
    sync_rank_history,
    sync_rx_stats,
    sync_teams,
)

__all__ = [
    "backfill",
    "rebuild_replays",
    "recompute_scores",
    "refresh_custom_covers",
    "refresh_custom_owners",
    "refresh_custom_previews",
    "refresh_user_banners",
    "sync_new",
    "sync_rank_history",
    "sync_rx_stats",
    "sync_teams",
]
