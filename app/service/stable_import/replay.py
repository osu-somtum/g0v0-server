"""Build a complete osu! `.osr` replay from bancho's raw replay data.

bancho.py stores only the raw replay payload the client uploaded (the LZMA frame
data) at `.data/osr/{id}.osr` and lets the osu!stable client reconstruct the `.osr`
header from leaderboard metadata. osu!lazer's replay download expects a *full* `.osr`
file, so we prepend the standard header (built from the score row) and append the
online-score-id footer.

Format ref: https://osu.ppy.sh/wiki/en/Client/File_formats/osr_%28file_format%29
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

# .NET epoch offset: ticks = (unix_seconds + 62135596800) * 10_000_000
_TICKS_EPOCH_OFFSET = 62135596800


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


def _u16(x: Any) -> int:
    return min(max(int(x), 0), 65535)


def _i32(x: Any) -> int:
    return min(max(int(x), 0), 2147483647)


def build_osr(row: Mapping[str, Any], username: str, raw_replay: bytes, online_id: int = 0) -> bytes:
    """Assemble a full `.osr` from a bancho score row + its raw replay payload."""
    play_time: datetime = row["play_time"]
    if play_time.tzinfo is None:
        play_time = play_time.replace(tzinfo=UTC)
    ticks = int((play_time.timestamp() + _TICKS_EPOCH_OFFSET) * 10_000_000)

    buf = bytearray()
    buf += struct.pack("<b", int(row["mode"]))  # ruleset
    buf += struct.pack("<i", 20210520)  # game version (any plausible value)
    buf += _osu_string(row["map_md5"])  # beatmap md5
    buf += _osu_string(username or "")  # player name
    buf += b"\x00"  # replay md5 (absent)
    buf += struct.pack(
        "<HHHHHH",
        _u16(row["n300"]),
        _u16(row["n100"]),
        _u16(row["n50"]),
        _u16(row["ngeki"]),
        _u16(row["nkatu"]),
        _u16(row["nmiss"]),
    )
    buf += struct.pack("<i", _i32(row["score"]))  # total score (stable)
    buf += struct.pack("<H", _u16(row["max_combo"]))
    buf += struct.pack("<b", 1 if int(row["perfect"]) else 0)
    buf += struct.pack("<i", int(row["mods"]) & 0x7FFFFFFF)  # mods bitmask
    buf += b"\x00"  # life bar graph (absent)
    buf += struct.pack("<q", ticks)
    buf += struct.pack("<i", len(raw_replay))
    buf += raw_replay
    buf += struct.pack("<q", online_id)
    return bytes(buf)
