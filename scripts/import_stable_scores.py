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

from app.service.stable_import import backfill


async def _main(dry_run: bool) -> None:
    result = await backfill(dry_run=dry_run)
    tag = "[dry-run] " if dry_run else ""
    print(
        f"{tag}stable import done: created={result['created']} "
        f"skipped_no_map={result['skipped_no_map']}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill bancho.py scores into lazer.")
    parser.add_argument("--dry-run", action="store_true", help="Report counts without writing.")
    args = parser.parse_args()
    asyncio.run(_main(args.dry_run))
