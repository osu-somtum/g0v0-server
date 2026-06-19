#!/usr/bin/env python3
"""One-time backfill of osu!stable (bancho.py) scores into lazer (g0v0).

Imports existing bancho `scores`/`maps` into g0v0's `scores`/`beatmaps`/
`beatmapsets` (+ `best_scores`/`beatmap_playcounts`) so shared accounts see real
Recent / Ranks / Most Played on their lazer profile. Idempotent — safe to re-run;
the periodic task (`app/tasks/stable_import.py`) keeps new plays flowing after.

Run from the g0v0-server root (or anywhere — it fixes sys.path):

    python scripts/import_stable_scores.py            # backfill
    python scripts/import_stable_scores.py --dry-run  # report only, no writes
"""

import argparse
import asyncio
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.service.stable_import import (
    backfill,
    rebuild_replays,
    recompute_scores,
    refresh_custom_covers,
    sync_rank_history,
    sync_rx_stats,
    sync_teams,
)


async def _main(
    dry_run: bool,
    rebuild: bool,
    recompute: bool,
    rank_history: bool,
    refresh_covers: bool,
    rx_stats: bool,
    teams: bool,
) -> None:
    if teams:
        result = await sync_teams()
        print(
            f"teams sync done: teams={result['teams']} "
            f"members_added={result['members_added']} members_removed={result['members_removed']}"
        )
        return
    if rx_stats:
        result = await sync_rx_stats()
        print(
            f"RX/AP stats sync done: updated={result['updated']} "
            f"inserted={result['inserted']} skipped_no_user={result['skipped_no_user']}"
        )
        return
    if refresh_covers:
        result = await refresh_custom_covers()
        print(f"custom-cover refresh done: updated={result['updated']}")
        return
    if rank_history:
        result = await sync_rank_history()
        print(
            f"rank-history bridge done: inserted={result['inserted']} "
            f"updated={result['updated']} skipped_no_user={result['skipped_no_user']}"
        )
        return
    if recompute:
        result = await recompute_scores()
        print(f"score recompute done: updated={result['updated']}")
        return
    if rebuild:
        result = await rebuild_replays()
        print(f"replay rebuild done: rebuilt={result['rebuilt']} missing_src={result['missing']}")
        return
    result = await backfill(dry_run=dry_run)
    tag = "[dry-run] " if dry_run else ""
    print(
        f"{tag}stable import done: created={result['created']} "
        f"skipped_no_map={result['skipped_no_map']}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill bancho.py scores into lazer.")
    parser.add_argument("--dry-run", action="store_true", help="Report counts without writing.")
    parser.add_argument(
        "--rebuild-replays",
        action="store_true",
        help="Regenerate .osr files for already-imported scores (after a format change).",
    )
    parser.add_argument(
        "--recompute-scores",
        action="store_true",
        help="Recompute standardised total_score for already-imported scores (after a formula change).",
    )
    parser.add_argument(
        "--sync-rank-history",
        action="store_true",
        help="Bridge bancho ranking_history into g0v0 rank_history (profile rank graph).",
    )
    parser.add_argument(
        "--refresh-covers",
        action="store_true",
        help="Point existing somtum custom beatmapset covers at the local /somtum/bg route.",
    )
    parser.add_argument(
        "--sync-rx-stats",
        action="store_true",
        help="Mirror bancho relax/autopilot stats (pp/rank) into lazer_user_statistics.",
    )
    parser.add_argument(
        "--sync-teams",
        action="store_true",
        help="Bridge bancho clans into g0v0 teams (so clans show as teams in lazer).",
    )
    args = parser.parse_args()
    asyncio.run(
        _main(
            args.dry_run,
            args.rebuild_replays,
            args.recompute_scores,
            args.sync_rank_history,
            args.refresh_covers,
            args.sync_rx_stats,
            args.sync_teams,
        )
    )
