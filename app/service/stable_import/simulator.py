"""Call the osu!lazer score simulator binary (osu.ScoreSimulator) to get
exact total_score + maximum_statistics for imported stable scores.

The binary runs StandardisedScoreMigrationTools.UpdateFromLegacy on a
FlatWorkingBeatmap — identical to the osu!lazer client — so the leaderboard
classic-mode display matches what the player actually sees in-game.

Falls back gracefully when:
- the binary is not present at `settings.bancho_sim_path`
- a specific beatmap .osu file is missing
- the binary returns an error for a score
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from app.config import settings
from app.log import log

logger = log("ScoreSimulator")

_RULESET_IDS = {0: 0, 1: 1, 2: 2, 3: 3, 4: 0, 5: 1, 6: 2, 8: 0}  # bancho mode → ruleset id


def _sim_available() -> bool:
    return Path(settings.bancho_sim_path).is_file()


def _beatmap_path(beatmap_id: int) -> Path | None:
    p = Path(settings.bancho_osu_dir) / f"{beatmap_id}.osu"
    return p if p.is_file() else None


async def simulate_batch(
    scores: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Simulate a batch of scores. Returns mapping of score id → sim result.

    Each input dict must have: id, beatmap_id, bancho_mode, mods, n300, n100,
    n50, n_geki, n_katu, n_miss, max_combo, legacy_score.
    Missing .osu files are silently skipped (caller falls back to heuristic).
    """
    if not scores or not _sim_available():
        return {}

    # Build request lines, skip missing beatmaps
    lines: list[str] = []
    for s in scores:
        p = _beatmap_path(int(s["beatmap_id"]))
        if p is None:
            continue
        ruleset_id = _RULESET_IDS.get(int(s["bancho_mode"]), 0)
        lines.append(json.dumps({
            "id": int(s["id"]),
            "beatmap_path": str(p),
            "ruleset_id": ruleset_id,
            "mods": int(s["mods"]),
            "n300": int(s["n300"]),
            "n100": int(s["n100"]),
            "n50": int(s["n50"]),
            "n_geki": int(s["n_geki"]),
            "n_katu": int(s["n_katu"]),
            "n_miss": int(s["n_miss"]),
            "max_combo": int(s["max_combo"]),
            "legacy_score": int(s["legacy_score"]),
        }))

    if not lines:
        return {}

    try:
        proc = await asyncio.create_subprocess_exec(
            settings.bancho_sim_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "DOTNET_EnableDiagnostics": "0", "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1"},
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(input="\n".join(lines).encode()),
            timeout=60.0,
        )
    except Exception as e:
        logger.warning("Score simulator failed: {e}", e=e)
        return {}

    results: dict[int, dict[str, Any]] = {}
    for line in stdout.decode().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            sid = int(obj["id"])
            if "error" in obj:
                logger.warning("Simulator error for score {id}: {e}", id=sid, e=obj["error"])
            else:
                results[sid] = obj
        except Exception:
            pass
    return results
