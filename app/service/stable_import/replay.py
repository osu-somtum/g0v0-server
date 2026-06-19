"""Build a complete osu! `.osr` replay from bancho's raw replay data.

bancho.py stores only the raw replay payload the client uploaded (the LZMA frame
data) at `.data/osr/{id}.osr` and lets the osu!stable client reconstruct the `.osr`
header from leaderboard metadata. osu!lazer's replay download expects a *full* `.osr`
file, so we prepend the standard header (built from the score row) and append the
online-score-id footer.

Critically, for osu!lazer to recognise a downloaded replay as the *same score* on a
leaderboard (and the *same player*), the `.osr` must be a **lazer-version** replay:
the modern `OnlineID` and the player's `RealmUser.OnlineID` are read ONLY from an
LZMA-compressed `LegacyReplaySoloScoreInfo` JSON block that lazer parses only when the
replay's version field is >= 30000001 (FIRST_LAZER_VERSION). A stable-version replay
(e.g. 20210520) leaves `OnlineID`/user id unset, so lazer treats the replay as a
different, anonymous score -> "not the same person" + can't bind to the leaderboard.

Format refs:
- https://osu.ppy.sh/wiki/en/Client/File_formats/osr_%28file_format%29
- ppy/osu osu.Game/Scoring/Legacy/LegacyScoreEncoder.cs / LegacyScoreDecoder.cs
- ppy/osu osu.Game/Scoring/Legacy/LegacyReplaySoloScoreInfo.cs
"""

from __future__ import annotations

import json
import lzma
import struct
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.models.mods.definition import APIMod

# .NET epoch offset: ticks = (unix_seconds + 62135596800) * 10_000_000
_TICKS_EPOCH_OFFSET = 62135596800

# Any value >= FIRST_LAZER_VERSION (30000000) makes lazer (a) read the appended
# LegacyReplaySoloScoreInfo block and (b) treat the header total score as a lazer
# standardised score (not auto-converting it). We use LATEST_VERSION so lazer
# considers the score fully up to date and doesn't try to reprocess it.
_LAZER_VERSION = 30000017


def _uleb128(n: int) -> bytes:
    if n == 0:
        return b"\x00"
    out = bytearray()
    while n > 0:
        b = n & 0x7F
        n >>= 7
        if n > 0:
            b |= 0x80
        out.append(b)
    return bytes(out)


def _osu_string(s: str) -> bytes:
    """osu! string: 0x00 = absent; else 0x0b + ULEB128(len) + UTF-8 bytes."""
    if not s:
        return b"\x00"
    enc = s.encode("utf-8")
    return b"\x0b" + _uleb128(len(enc)) + enc


def _osu_byte_array(b: bytes) -> bytes:
    """osu! SerializationWriter.WriteByteArray: int32 length prefix + raw bytes."""
    return struct.pack("<i", len(b)) + b


def _u16(x: Any) -> int:
    return min(max(int(x), 0), 65535)


def _i32(x: Any) -> int:
    return min(max(int(x), 0), 2147483647)


def _lzma_alone(data: bytes) -> bytes:
    """LZMA1 'alone' frame (5 props + 8-byte size + stream) — the exact format osu!
    uses for both replay frames and the appended score-info block.

    Python's FORMAT_ALONE writes -1 (unknown size) in the 8-byte size field and
    relies on an end-of-stream marker, but osu!'s decoder reads that field as the
    real output length and decodes exactly that many bytes. So patch in the true
    uncompressed size (the EOS marker is then simply left unread — harmless)."""
    frame = bytearray(lzma.compress(data, format=lzma.FORMAT_ALONE))
    frame[5:13] = struct.pack("<q", len(data))  # real uncompressed size
    return bytes(frame)


# osu! replay key bitmask: M1=1, M2=2, K1=4, K2=8. A keyboard tap sets both the
# keyboard bit and its mouse-button companion, so alternate K1(=M1|K1) / K2(=M2|K2).
_TAP_K1 = 1 | 4
_TAP_K2 = 2 | 8
_TAP_LEAD_MS = 6  # press a hair before the object so the key-down lands in-window
_TAP_HOLD_MS = 40  # circle hold length
_SLIDER_TAIL_MS = 20
_SPINNER_TAIL_MS = 20

_MOD_EZ = 1 << 1
_MOD_HR = 1 << 4


def _parse_beatmap(osu_text: str) -> tuple[float, list[tuple[float, float, int]]]:
    """Parse a .osu into (overall_difficulty, objects).

    objects = [(tap_time, hold_end_time, type_bits)] in map-time, ordered.
    Used to reconstruct relax taps: stable relax replays store cursor movement but
    no key presses, so lazer replays them as all-misses. We only need object
    *times* (the cursor is already on the object) + OD (to place off-hits in the
    right judgement window). Map-time is the replay's own time base (verified), so
    DT/HT need no scaling.
    """
    slider_mult = 1.4
    od = 5.0
    section = ""
    raw_objs: list[str] = []
    raw_timing: list[tuple[float, float]] = []

    for line in osu_text.splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("["):
            section = line
            continue
        if section == "[Difficulty]":
            low = line.lower()
            if low.startswith("slidermultiplier"):
                try:
                    slider_mult = float(line.split(":", 1)[1])
                except ValueError:
                    pass
            elif low.startswith("overalldifficulty"):
                try:
                    od = float(line.split(":", 1)[1])
                except ValueError:
                    pass
        elif section == "[TimingPoints]":
            p = line.split(",")
            if len(p) >= 2:
                try:
                    raw_timing.append((float(p[0]), float(p[1])))
                except ValueError:
                    pass
        elif section == "[HitObjects]":
            raw_objs.append(line)

    raw_timing.sort(key=lambda r: r[0])
    timing: list[tuple[float, float, float]] = []
    cur_beat, cur_sv = 500.0, 1.0
    for t, raw in raw_timing:
        if raw > 0:
            cur_beat, cur_sv = raw, 1.0
        else:
            cur_sv = max(0.1, min(10.0, -100.0 / raw)) if raw != 0 else 1.0
        timing.append((t, cur_beat, cur_sv))

    def state_at(t: float) -> tuple[float, float]:
        beat, sv = 500.0, 1.0
        for tt, b, s in timing:
            if tt <= t:
                beat, sv = b, s
            else:
                break
        return beat, sv

    objects: list[tuple[float, float, int]] = []
    for raw in raw_objs:
        p = raw.split(",")
        if len(p) < 4:
            continue
        try:
            t = float(p[2])
            typ = int(p[3])
        except ValueError:
            continue
        if typ & 8:  # spinner
            try:
                end = float(p[5])
            except (IndexError, ValueError):
                end = t + 500.0
            objects.append((t, end + _SPINNER_TAIL_MS, typ))
        elif typ & 2:  # slider
            dur = 100.0
            try:
                slides = int(p[6])
                length = float(p[7])
                beat, sv = state_at(t)
                denom = slider_mult * 100.0 * sv
                if denom > 0:
                    dur = length / denom * beat * max(slides, 1)
            except (IndexError, ValueError, ZeroDivisionError):
                pass
            objects.append((t, t + dur + _SLIDER_TAIL_MS, typ))
        else:  # circle
            objects.append((t, t + _TAP_HOLD_MS, typ))

    objects.sort(key=lambda o: o[0])
    return od, objects


def _assign_judgements(n_obj: int, n300: int, n100: int, n50: int, nmiss: int) -> dict[int, str]:
    """Spread the score's 100/50/miss counts evenly across object indices so the
    re-judged replay accuracy matches the stable score. Everything else is a 300."""
    assigned: dict[int, str] = {}
    if n_obj <= 0:
        return assigned

    def spread(count: int, label: str) -> None:
        if count <= 0:
            return
        placed = 0
        # even stride over all slots; skip slots already taken
        for k in range(count):
            target = int((k + 0.5) * n_obj / count)
            idx = min(max(target, 0), n_obj - 1)
            # find nearest free slot
            for off in range(n_obj):
                for cand in (idx + off, idx - off):
                    if 0 <= cand < n_obj and cand not in assigned:
                        assigned[cand] = label
                        placed += 1
                        break
                else:
                    continue
                break
            if placed >= count:
                break

    # misses first (most impactful), then 50s, then 100s
    spread(nmiss, "miss")
    spread(n50, "50")
    spread(n100, "100")
    return assigned


def synthesize_relax_replay(
    payload: bytes,
    osu_text: str,
    mods_bitmask: int = 0,
    n300: int = 0,
    n100: int = 0,
    n50: int = 0,
    nmiss: int = 0,
) -> bytes:
    """Return a new compressed replay payload with relax taps injected.

    `payload` is bancho's raw LZMA frame stream; `osu_text` the beatmap. Cursor
    frames are preserved verbatim; only the key field gets synthesized taps,
    alternating K1/K2 per object. When the score's hit counts are supplied, the
    taps reproduce the real 300/100/50/miss distribution (off-hits are nudged into
    the matching OD window; misses get no tap) so the replay re-judges to the same
    accuracy as the stable score instead of a perfect FC.
    """
    try:
        text_frames = lzma.decompress(payload, format=lzma.FORMAT_ALONE).decode("ascii", "ignore")
    except lzma.LZMAError:
        return payload  # not the format we expect; leave untouched

    od, objects = _parse_beatmap(osu_text)
    if not objects:
        return payload

    # OD -> ± hit windows (ms, map-time). HR/EZ adjust OD.
    eff_od = od
    if mods_bitmask & _MOD_HR:
        eff_od = min(eff_od * 1.4, 10.0)
    elif mods_bitmask & _MOD_EZ:
        eff_od = eff_od * 0.5
    w300 = max(80.0 - 6.0 * eff_od, 6.0)
    w100 = max(140.0 - 8.0 * eff_od, w300 + 8.0)
    w50 = max(200.0 - 10.0 * eff_od, w100 + 8.0)
    off_100 = (w300 + w100) / 2.0  # safely inside the 100 window
    off_50 = (w100 + w50) / 2.0  # safely inside the 50 window

    judgements = _assign_judgements(len(objects), n300, n100, n50, nmiss)

    # Build per-object hold intervals with the right key/offset (skip misses).
    # Pick a key that's free (its previous hold already ended) so every tap is a
    # fresh key-DOWN — otherwise a long slider holding K1 into a later K1 object
    # would swallow that object's hit. Fall back to alternating.
    intervals: list[tuple[float, float, int]] = []
    last_end = {_TAP_K1: -1e18, _TAP_K2: -1e18}
    prev = _TAP_K2
    for i, (t, hold_end, _typ) in enumerate(objects):
        judge = judgements.get(i, "300")
        if judge == "miss":
            continue
        offset = off_100 if judge == "100" else off_50 if judge == "50" else 0.0
        start = t - _TAP_LEAD_MS + offset
        end = max(hold_end, t + _TAP_HOLD_MS) + (offset if offset else 0.0)

        alt = _TAP_K1 if prev == _TAP_K2 else _TAP_K2
        if last_end[alt] <= start:
            keybit = alt
        elif last_end[_TAP_K1] <= start or last_end[_TAP_K2] <= start:
            keybit = _TAP_K1 if last_end[_TAP_K1] <= start else _TAP_K2
        else:  # both still held — use the one freeing soonest
            keybit = _TAP_K1 if last_end[_TAP_K1] <= last_end[_TAP_K2] else _TAP_K2
        last_end[keybit] = end
        prev = keybit
        intervals.append((start, end, keybit))

    if not intervals:
        return payload

    tokens = text_frames.split(",")
    cum: list[float] = []
    idx_of: list[int] = []
    t = 0.0
    for i, tok in enumerate(tokens):
        if not tok:
            continue
        parts = tok.split("|")
        if len(parts) != 4 or parts[0] == "-12345":
            continue
        try:
            t += float(parts[0])
        except ValueError:
            continue
        cum.append(t)
        idx_of.append(i)

    if not cum:
        return payload

    # cum isn't strictly monotonic (osu! lead-in frames), so search a sorted view.
    order = sorted(range(len(cum)), key=lambda k: cum[k])
    sorted_t = [cum[k] for k in order]
    keys = [0] * len(cum)

    import bisect

    for start, end, keybit in intervals:
        lo = bisect.bisect_left(sorted_t, start)
        hi = bisect.bisect_right(sorted_t, end)
        for j in range(lo, hi):
            keys[order[j]] |= keybit

    for entry, tok_idx in enumerate(idx_of):
        kb = keys[entry]
        if not kb:
            continue
        parts = tokens[tok_idx].split("|")
        old = int(parts[3]) if parts[3].isdigit() else 0
        parts[3] = str(old | kb)
        tokens[tok_idx] = "|".join(parts)

    return _lzma_alone(",".join(tokens).encode("ascii", "ignore"))


def _score_info_block(
    *,
    online_id: int,
    user_id: int,
    api_mods: list[APIMod],
    statistics: Mapping[str, int],
    maximum_statistics: Mapping[str, int],
    rank: str,
    total_score_without_mods: int,
) -> bytes:
    """LZMA-compressed `LegacyReplaySoloScoreInfo` JSON appended to the .osr.

    This is what gives the replay its modern identity:
    - `online_id` -> ScoreInfo.OnlineID (matches the leaderboard score id), and
    - `user_id`   -> RealmUser.OnlineID (so lazer knows it's the same player).
    """
    obj: dict[str, Any] = {
        "online_id": int(online_id),
        "mods": list(api_mods),
        "statistics": {k: v for k, v in statistics.items() if v},
        "maximum_statistics": {k: v for k, v in maximum_statistics.items() if v},
        "client_version": "",
        "rank": rank,
        "user_id": int(user_id),
        "pauses": [],
    }
    if total_score_without_mods:
        obj["total_score_without_mods"] = int(total_score_without_mods)
    payload = json.dumps(obj, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return _lzma_alone(payload)


def build_osr(
    *,
    mode: int,
    beatmap_md5: str,
    username: str,
    n300: int,
    n100: int,
    n50: int,
    ngeki: int,
    nkatu: int,
    nmiss: int,
    header_score: int,
    max_combo: int,
    perfect: bool,
    mods_bitmask: int,
    played_at: datetime,
    raw_replay: bytes,
    online_id: int,
    user_id: int,
    rank: str,
    api_mods: list[APIMod],
    statistics: Mapping[str, int],
    maximum_statistics: Mapping[str, int],
    total_score_without_mods: int = 0,
) -> bytes:
    """Assemble a full lazer-recognised `.osr` from a stable score + its raw replay.

    `header_score` should be the lazer *standardised* total score (it becomes the
    displayed score on the replay result screen); `api_mods` must include the
    `CL` (Classic) mod and overrides the header mod bitmask on decode.
    """
    if played_at.tzinfo is None:
        played_at = played_at.replace(tzinfo=UTC)
    ticks = int((played_at.timestamp() + _TICKS_EPOCH_OFFSET) * 10_000_000)

    buf = bytearray()
    buf += struct.pack("<b", int(mode))  # ruleset
    buf += struct.pack("<i", _LAZER_VERSION)  # game/replay version (lazer)
    buf += _osu_string(beatmap_md5)  # beatmap md5
    buf += _osu_string(username or "")  # player name
    buf += b"\x00"  # replay md5 (absent)
    buf += struct.pack(
        "<HHHHHH",
        _u16(n300),
        _u16(n100),
        _u16(n50),
        _u16(ngeki),
        _u16(nkatu),
        _u16(nmiss),
    )
    buf += struct.pack("<i", _i32(header_score))  # total score (standardised)
    buf += struct.pack("<H", _u16(max_combo))
    buf += struct.pack("<b", 1 if perfect else 0)
    buf += struct.pack("<i", int(mods_bitmask) & 0x7FFFFFFF)  # legacy mods (JSON mods override)
    buf += b"\x00"  # life bar graph (absent)
    buf += struct.pack("<q", ticks)
    buf += _osu_byte_array(raw_replay)  # compressed replay frames
    buf += struct.pack("<q", int(online_id))  # LegacyOnlineID footer
    buf += _osu_byte_array(
        _score_info_block(
            online_id=online_id,
            user_id=user_id,
            api_mods=api_mods,
            statistics=statistics,
            maximum_statistics=maximum_statistics,
            rank=rank,
            total_score_without_mods=total_score_without_mods,
        ),
    )
    return bytes(buf)
