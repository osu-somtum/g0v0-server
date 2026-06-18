"""Pure bancho.py -> g0v0 value mappings for the stable score importer."""

from __future__ import annotations

from app.models.beatmap import BeatmapRankStatus
from app.models.mods.definition import APIMod
from app.models.score import Rank

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
