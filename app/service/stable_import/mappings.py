"""Pure bancho.py -> g0v0 value mappings for the stable score importer."""

from __future__ import annotations

import math

from app.const import MAX_SCORE

# osu!lazer ScoreProcessor.COMBO_EXPONENT — used in the stable→standardised conversion.
_COMBO_EXPONENT = 0.5


def _osu_std_combo_proportion(
    max_combo: int, beatmap_max_combo: int, nmiss: int, acc: float
) -> float:
    """Dual-estimate newComboScoreProportion for osu!std.
    Mirrors StandardisedScoreMigrationTools.convertFromLegacyTotalScore (case 0,
    lines 214-292), using comboProportion ≈ max_combo/beatmap_max_combo because
    we don't run the ScoreV1 simulator server-side.
    """
    if beatmap_max_combo == 0:
        return 1.0
    cp = min(max_combo / beatmap_max_combo, 1.0)
    max_v1 = float(beatmap_max_combo ** 2)
    max_std = float(beatmap_max_combo ** (1 + _COMBO_EXPONENT))
    longest_v1 = float(max_combo ** 2)
    longest_std = float(max_combo ** (1 + _COMBO_EXPONENT))

    combo_v1 = max(max_v1 * cp / max(acc, 1e-10), longest_v1)

    # Score-based estimate
    n_repeat = math.floor(combo_v1 / longest_v1) if longest_v1 > 0 else 0
    remaining_std = math.sqrt(combo_v1 - n_repeat * longest_v1) ** (1 + _COMBO_EXPONENT)
    score_est = n_repeat * longest_std + remaining_std

    # Object-count-based estimate
    remaining_objects = beatmap_max_combo - max_combo - nmiss
    combo_len = (combo_v1 - longest_v1) / remaining_objects if remaining_objects > 0 else 0.0
    obj_est = longest_std + remaining_objects * (combo_len ** _COMBO_EXPONENT)

    score_est = min(max(score_est, 0.0), max_std)
    obj_est = min(max(obj_est, 0.0), max_std)
    lower, upper = min(score_est, obj_est), max(score_est, obj_est)
    estimated = min(0.3 * lower + 0.7 * upper, 1.2 * (lower + upper) / 2)
    return estimated / max_std if max_std > 0 else 0.0


def _catch_combo_proportion(
    beatmap_max_combo: int, score_max_combo: int, nmiss: int
) -> float:
    """estimateComboProportionForCatch — log-scaled ScoreV1 catch combo model.
    Mirrors StandardisedScoreMigrationTools.estimateComboProportionForCatch.
    """
    if beatmap_max_combo == 0:
        return 1.0
    if score_max_combo == 0:
        return 0.0
    if beatmap_max_combo == score_max_combo:
        return 1.0

    def _best_case(mc: int) -> float:
        if mc == 0:
            return 1.0
        t = 0.5 * min(mc, 2)
        if mc <= 2:
            return t
        t += (min(mc, 200) * (math.log(min(mc, 200)) - 1) + 2 - math.log(4)) / math.log(4)
        if mc <= 200:
            return t
        t += (mc - 200) * math.log(200) / math.log(4)
        return t

    def _dropped(length: int) -> float:
        length = min(length, 200)
        return length * (1 + math.log(200) - math.log(max(length, 1))) / math.log(4)

    best = _best_case(beatmap_max_combo)
    remaining = beatmap_max_combo - (score_max_combo + nmiss)
    dropped = 0.0
    assumed_len = int(remaining / nmiss) if nmiss > 0 else 0
    if assumed_len > 0:
        assumed_count = int(remaining / assumed_len)
        dropped += assumed_count * _dropped(assumed_len)
        leftover = remaining - assumed_count * assumed_len
        if leftover > 0:
            dropped += _dropped(leftover)
    else:
        dropped = best - _best_case(score_max_combo)
    return 1.0 - min(max(dropped / best, 0.0), 1.0) if best > 0 else 1.0
from app.models.beatmap import BeatmapRankStatus
from app.models.mods.definition import APIMod
from app.models.score import GameMode, Rank

# bancho.py gamemode int -> g0v0 GameMode. bancho numbers relax/autopilot as
# 4=rx!std, 5=rx!taiko, 6=rx!catch, 8=ap!std (g0v0's own from_int_extra uses a
# DIFFERENT numbering, so it can't be reused for bancho modes). Returns None for
# ids bancho never produces (e.g. 7).
_BANCHO_MODE_TO_GAMEMODE: dict[int, GameMode] = {
    0: GameMode.OSU,
    1: GameMode.TAIKO,
    2: GameMode.FRUITS,
    3: GameMode.MANIA,
    4: GameMode.OSURX,
    5: GameMode.TAIKORX,
    6: GameMode.FRUITSRX,
    8: GameMode.OSUAP,
}


def bancho_mode_to_gamemode(mode: int) -> GameMode | None:
    return _BANCHO_MODE_TO_GAMEMODE.get(mode)

# bancho `Mods` IntFlag bit -> osu! mod acronym (see bancho.py app/constants/mods.py).
# We cannot import bancho's enum from g0v0, so the bits are mirrored here. Stable
# scores always carry the lazer `CL` (classic) mod, appended in `int_mods_to_apimods`.
_MODS_BITS: list[tuple[int, str]] = [
    (1 << 0, "NF"),
    (1 << 1, "EZ"),
    (1 << 2, "TD"),  # touchscreen
    (1 << 3, "HD"),
    (1 << 4, "HR"),
    (1 << 5, "SD"),
    (1 << 6, "DT"),
    (1 << 7, "RX"),  # relax (vanilla-only import skips these scores, but map anyway)
    (1 << 8, "HT"),
    (1 << 9, "NC"),
    (1 << 10, "FL"),
    (1 << 11, "AT"),  # autoplay
    (1 << 12, "SO"),
    (1 << 13, "AP"),  # autopilot
    (1 << 14, "PF"),
    (1 << 15, "4K"),
    (1 << 16, "5K"),
    (1 << 17, "6K"),
    (1 << 18, "7K"),
    (1 << 19, "8K"),
    (1 << 20, "FI"),  # fade in
    (1 << 21, "RD"),  # random
    (1 << 22, "CN"),  # cinema
    (1 << 23, "TP"),  # target practice
    (1 << 24, "9K"),
    (1 << 26, "1K"),
    (1 << 27, "3K"),
    (1 << 28, "2K"),
    (1 << 30, "MR"),  # mirror
]
# bits intentionally dropped: KEYCOOP (1<<25, no lazer equivalent), SCOREV2 (1<<29,
# lazer scores ScoreV2 by default).


def int_mods_to_apimods(mods: int) -> list[APIMod]:
    """Convert a bancho int mod bitmask to a lazer `APIMod` list (+ `CL`).

    NC implies DT in the bancho bitmask but lazer treats NC as standalone, so DT is
    dropped when NC is present (likewise SD dropped when PF is present).
    """
    acronyms = [acr for bit, acr in _MODS_BITS if mods & bit]
    if "NC" in acronyms and "DT" in acronyms:
        acronyms.remove("DT")
    if "PF" in acronyms and "SD" in acronyms:
        acronyms.remove("SD")
    # Stable plays are always "classic" in lazer terms.
    acronyms.append("CL")
    return [APIMod(acronym=acr) for acr in acronyms]


_GRADE_TO_RANK: dict[str, Rank] = {
    "XH": Rank.XH,
    "SSH": Rank.XH,
    "X": Rank.X,
    "SS": Rank.X,
    "SH": Rank.SH,
    "S": Rank.S,
    "A": Rank.A,
    "B": Rank.B,
    "C": Rank.C,
    "D": Rank.D,
    "F": Rank.F,
    "N": Rank.F,  # bancho default 'N' (no grade) for fails
}


def grade_to_rank(grade: str) -> Rank:
    return _GRADE_TO_RANK.get((grade or "").upper(), Rank.D)


def standardised_total_score(
    mode: int,
    accuracy: float,
    max_combo: int,
    beatmap_max_combo: int,
    nmiss: int = 0,
    legacy_score: int = 0,
    primary_objects: int = 0,
) -> int:
    """Convert a stable (ScoreV1) play to osu!lazer's standardised score (0..1,000,000).

    Implements StandardisedScoreMigrationTools.convertFromLegacyTotalScore per-ruleset:
    - osu!std: dual-estimate combo proportion (COMBO_EXPONENT=0.5, max_combo, nmiss),
               plus a slider-density correction: stable's combo count includes slider
               ticks which contribute less in lazer's ScoreProcessor. We scale by
               (primary_objects / beatmap_max_combo)^0.02 to compensate.
    - catch:   log-scaled estimateComboProportionForCatch
    - mania:   comboProportion = legacy_score / 1M (ScoreV1 mania has no acc portion)
    - taiko:   max_combo / beatmap_max_combo (formula already matches)
    Bonus (spinner/drumroll) score is omitted as it needs the beatmap simulator.
    """
    acc = min(max(accuracy, 0.0), 1.0)

    if mode == 1:  # taiko
        cp = min(max_combo / beatmap_max_combo, 1.0) if beatmap_max_combo > 0 else acc
        score = 250000 * cp + 750000 * acc**3.6

    elif mode == 2:  # catch — log-scaled combo model
        cp = _catch_combo_proportion(beatmap_max_combo, max_combo, nmiss)
        score = MAX_SCORE * cp

    elif mode == 3:  # mania — ScoreV1 is all combo; no separate accuracy portion
        cp = min(legacy_score / MAX_SCORE, 1.0) if legacy_score > 0 else (
            min(max_combo / beatmap_max_combo, 1.0) if beatmap_max_combo > 0 else acc
        )
        score = 850000 * cp + 150000 * acc ** (2 + 2 * acc)

    else:  # osu! std (0) + relax/autopilot variants
        if max_combo == 0 or acc == 0:
            score = 500000 * acc**5
        else:
            new_cp = _osu_std_combo_proportion(max_combo, beatmap_max_combo, nmiss, acc)
            score = 500000 * new_cp * acc + 500000 * acc**5
            # Slider-density correction: stable's max_combo includes slider ticks which
            # have lower weight in lazer's ScoreProcessor than circles. Scale the combo
            # portion down proportionally. Exponent 0.02 is empirically calibrated.
            if primary_objects > 0 and beatmap_max_combo > primary_objects:
                correction = (primary_objects / beatmap_max_combo) ** 0.02
                score = 500000 * new_cp * acc * correction + 500000 * acc**5

    return round(min(score, MAX_SCORE))


# bancho RankedStatus int (app/objects/beatmap.py) -> g0v0 BeatmapRankStatus.
# bancho: NotSubmitted=-1, Pending=0, UpdateAvailable=1, Ranked=2, Approved=3,
# Qualified=4, Loved=5.  g0v0: GRAVEYARD=-2, WIP=-1, PENDING=0, RANKED=1,
# APPROVED=2, QUALIFIED=3, LOVED=4.
_STATUS_MAP: dict[int, BeatmapRankStatus] = {
    -1: BeatmapRankStatus.GRAVEYARD,
    0: BeatmapRankStatus.PENDING,
    1: BeatmapRankStatus.WIP,
    2: BeatmapRankStatus.RANKED,
    3: BeatmapRankStatus.APPROVED,
    4: BeatmapRankStatus.QUALIFIED,
    5: BeatmapRankStatus.LOVED,
}


def map_status_to_g0v0(status: int) -> BeatmapRankStatus:
    return _STATUS_MAP.get(status, BeatmapRankStatus.PENDING)


_COVER_KEYS = ("cover", "cover@2x", "card", "card@2x", "list", "list@2x", "slimcover", "slimcover@2x")


def empty_covers() -> dict[str, str]:
    """Non-null covers for custom maps (no osu! art). osu!lazer's Covers struct is
    NOT nullable — a null `covers` breaks deserialization of the whole beatmap
    listing, so custom sets must carry an (empty) covers object, not None."""
    return {k: "" for k in _COVER_KEYS}


def custom_covers(set_id: int, base_url: str) -> dict[str, str]:
    """Covers for a somtum custom set, served from this server's `/somtum/bg`
    route (bancho stored the background locally; osu!'s CDN has nothing). Full
    image for big covers, thumbnail for card/list."""
    base = base_url.rstrip("/")
    full = f"{base}/somtum/bg/{set_id}"
    thumb = f"{full}/thumb"
    return {
        "cover": full,
        "cover@2x": full,
        "card": thumb,
        "card@2x": thumb,
        "list": thumb,
        "list@2x": thumb,
        "slimcover": full,
        "slimcover@2x": full,
    }


def custom_preview_url(set_id: int, base_url: str) -> str:
    """Audio-preview URL for a somtum custom set, served from this server's
    `/somtum/preview` route (osu!'s b.ppy.sh preview CDN has nothing for it)."""
    return f"{base_url.rstrip('/')}/somtum/preview/{set_id}"


def osu_covers(set_id: int) -> dict[str, str]:
    """Standard osu! CDN cover set for an osu!-origin beatmapset."""
    base = f"https://assets.ppy.sh/beatmaps/{set_id}/covers"
    return {
        "cover": f"{base}/cover.jpg",
        "cover@2x": f"{base}/cover@2x.jpg",
        "card": f"{base}/card.jpg",
        "card@2x": f"{base}/card@2x.jpg",
        "list": f"{base}/list.jpg",
        "list@2x": f"{base}/list@2x.jpg",
        "slimcover": f"{base}/slimcover.jpg",
        "slimcover@2x": f"{base}/slimcover@2x.jpg",
    }
