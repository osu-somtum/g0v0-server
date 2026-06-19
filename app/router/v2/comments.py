"""osu! API v2 comments (CommentBundle) backed by bancho's beatmap comments.

The somtum website stores beatmapset-page comments in bancho.py's
`beatmap_comments` table (set_id, user_id, parent_id, content BBCode, pinned).
osu!lazer's beatmap page talks to `/api/v2/comments` (a `CommentBundle`), which
g0v0 didn't implement (-> 404). This module bridges the two so comments sync BOTH
ways: lazer reads/writes the same `beatmap_comments` rows the website uses.

Only `commentable_type=beatmapset` is supported (the only thing the in-client
beatmap page asks for). Votes are not stored by bancho, so vote endpoints are
accepted but always report 0 (keeps the client happy without inventing data).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from app.database.user import User, UserModel
from app.dependencies.database import Database
from app.dependencies.user import ClientUser
from app.models.error import ErrorType, RequestError
from app.service.bbcode_service import bbcode_service
from app.service.stable_import.bancho_db import get_bancho_engine

from .router import router

from fastapi import Path, Query, Request
from sqlalchemy import text
from sqlmodel import col, select

_BEATMAPSET = "beatmapset"


def _iso(dt: datetime | None) -> str | None:
    """ISO-8601 with an explicit UTC marker.

    bancho stores `beatmap_comments` timestamps as naive UTC (DB clock is UTC;
    inserted via `NOW()`). osu!lazer parses these as `DateTimeOffset`, and a
    zoneless string is read as the player's LOCAL time -> comments appear shifted
    by the user's UTC offset. Emit a trailing `Z` (as g0v0 does for `last_visit`)
    so the client converts from UTC to local correctly.
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.isoformat()
    return dt.isoformat() + "Z"


def _message_html(content: str) -> str:
    try:
        return bbcode_service.parse_bbcode(content or "")
    except Exception:
        return content or ""


def _comment_dict(row: Any, replies_count: int) -> dict[str, Any]:
    """Map a bancho `beatmap_comments` row to an osu! Comment object."""
    created = row["created_at"]
    updated = row["updated_at"]
    return {
        "id": int(row["id"]),
        "parent_id": int(row["parent_id"]) if row["parent_id"] is not None else None,
        "user_id": int(row["user_id"]),
        "message": row["content"],
        "message_html": _message_html(row["content"]),
        "replies_count": replies_count,
        "votes_count": 0,
        "commentable_type": _BEATMAPSET,
        "commentable_id": int(row["set_id"]),
        "legacy_name": None,
        "created_at": _iso(created),
        "updated_at": _iso(updated or created),
        "deleted_at": None,
        "edited_at": _iso(updated) if updated else None,
        "edited_by_id": None,
        "pinned": bool(row["pinned"]),
    }


async def _fetch_set_comments(set_id: int) -> list[Any]:
    """All comments for a beatmapset, newest first."""
    eng = get_bancho_engine()
    async with eng.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT id, set_id, user_id, parent_id, content, created_at, updated_at, pinned
                    FROM beatmap_comments
                    WHERE set_id = :sid
                    ORDER BY created_at DESC
                    """,
                ),
                {"sid": set_id},
            )
        ).mappings().all()
    return list(rows)


async def _users_payload(db: Database, user_ids: set[int]) -> list[dict[str, Any]]:
    if not user_ids:
        return []
    users = (await db.exec(select(User).where(col(User.id).in_(user_ids)))).all()
    return [await UserModel.transform(u, includes=User.CARD_INCLUDES) for u in users]


async def _commentable_meta(db: Database, set_id: int) -> list[dict[str, Any]]:
    from app.database.beatmapset import Beatmapset

    bs = await db.get(Beatmapset, set_id)
    title = f"{bs.artist} - {bs.title}" if bs is not None else f"Beatmapset {set_id}"
    owner_id = int(bs.user_id) if bs is not None and bs.user_id else 0
    return [
        {
            "id": set_id,
            "title": title,
            "type": _BEATMAPSET,
            "owner_id": owner_id,
            "owner_title": "",
            "url": f"/beatmapsets/{set_id}",
            "current_user_attributes": {"can_new_comment_reason": None},
        }
    ]


async def _bundle(
    db: Database,
    set_id: int,
    *,
    parent_id: int,
    sort: str,
    extra_comment_ids: set[int] | None = None,
) -> dict[str, Any]:
    """Assemble a CommentBundle for a beatmapset (small datasets -> load all)."""
    rows = await _fetch_set_comments(set_id)
    replies_count: dict[int, int] = {}
    for r in rows:
        if r["parent_id"] is not None:
            replies_count[int(r["parent_id"])] = replies_count.get(int(r["parent_id"]), 0) + 1

    def is_top(r: Any) -> bool:
        return r["parent_id"] is None

    if parent_id and parent_id > 0:
        selected = [r for r in rows if r["parent_id"] is not None and int(r["parent_id"]) == parent_id]
    else:
        selected = [r for r in rows if is_top(r)]

    if sort == "old":
        selected = list(reversed(selected))
    elif sort == "top":
        selected = sorted(selected, key=lambda r: bool(r["pinned"]), reverse=True)

    comments = [_comment_dict(r, replies_count.get(int(r["id"]), 0)) for r in selected]

    # Replies views reference their parent via included_comments.
    included: list[dict[str, Any]] = []
    if parent_id and parent_id > 0:
        parent_row = next((r for r in rows if int(r["id"]) == parent_id), None)
        if parent_row is not None:
            included.append(_comment_dict(parent_row, replies_count.get(parent_id, 0)))

    pinned = [_comment_dict(r, replies_count.get(int(r["id"]), 0)) for r in rows if is_top(r) and bool(r["pinned"])]

    if extra_comment_ids:
        extra_rows = [r for r in rows if int(r["id"]) in extra_comment_ids]
        comments = [_comment_dict(r, replies_count.get(int(r["id"]), 0)) for r in extra_rows] or comments

    user_ids = {int(c["user_id"]) for c in (comments + included + pinned)}
    top_level_count = sum(1 for r in rows if is_top(r))

    return {
        "comments": comments,
        "has_more": False,
        "has_more_id": None,
        "included_comments": included,
        "pinned_comments": pinned,
        # bool (not a list) + a List<long>; see the empty-bundle note above.
        "user_follow": False,
        "user_votes": [],
        "users": await _users_payload(db, user_ids),
        "total": len(rows),
        "top_level_count": top_level_count,
        "sort": sort,
        "cursor": None,
        "commentable_meta": await _commentable_meta(db, set_id),
    }


@router.get("/comments", tags=["Comments"], name="Get comments")
async def get_comments(
    db: Database,
    commentable_type: Annotated[str, Query()] = "",
    commentable_id: Annotated[int, Query()] = 0,
    parent_id: Annotated[int, Query()] = 0,
    sort: Annotated[str, Query()] = "new",
    page: Annotated[int, Query()] = 1,
) -> dict[str, Any]:
    """List comments for a beatmapset (bridged from bancho `beatmap_comments`)."""
    if commentable_type and commentable_type != _BEATMAPSET:
        # Only beatmapset comments are bridged; return an empty, valid bundle.
        return {
            "comments": [],
            "has_more": False,
            "has_more_id": None,
            "included_comments": [],
            "pinned_comments": [],
            # osu!lazer's CommentBundle.UserFollow is a `bool` and UserVotes a
            # `List<long>`; a wrong JSON type makes Newtonsoft fail the WHOLE
            # bundle (200 OK but client renders nothing). Keep these exact.
            "user_follow": False,
            "user_votes": [],
            "users": [],
            "total": 0,
            "top_level_count": 0,
            "sort": sort,
            "cursor": None,
            "commentable_meta": [],
        }
    return await _bundle(db, commentable_id, parent_id=parent_id, sort=sort)


@router.get("/comments/{comment_id}", tags=["Comments"], name="Get a comment")
async def get_comment(db: Database, comment_id: Annotated[int, Path()]) -> dict[str, Any]:
    eng = get_bancho_engine()
    async with eng.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT set_id FROM beatmap_comments WHERE id = :id"),
                {"id": comment_id},
            )
        ).mappings().first()
    if row is None:
        raise RequestError(ErrorType.NOT_FOUND)
    return await _bundle(db, int(row["set_id"]), parent_id=0, sort="new", extra_comment_ids={comment_id})


async def _read_comment_payload(request: Request) -> dict[str, Any]:
    """Extract the comment fields from the request.

    osu!lazer POSTs comments as multipart/form-data with bracketed field names
    (`comment[commentable_type]`, `comment[commentable_id]`, `comment[message]`,
    `comment[parent_id]`). Some clients/tools send JSON (`{"comment": {...}}` or a
    flat object). Accept all of these so the previous JSON-only signature can't
    422 (which then crashed the global validation handler on the raw bytes)."""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if isinstance(body, dict):
            inner = body.get("comment")
            return inner if isinstance(inner, dict) else body
        return {}

    # form-encoded (multipart or urlencoded), with bracketed `comment[...]` keys.
    try:
        form = await request.form()
    except Exception:
        return {}
    payload: dict[str, Any] = {}
    for key, value in form.multi_items():
        if key.startswith("comment[") and key.endswith("]"):
            payload[key[len("comment["):-1]] = value
        elif key.startswith("comment."):
            payload[key[len("comment."):]] = value
        else:
            payload.setdefault(key, value)
    return payload


@router.post("/comments", tags=["Comments"], name="Post a comment")
async def post_comment(
    db: Database,
    current_user: ClientUser,
    request: Request,
) -> dict[str, Any]:
    """Create a comment (written to bancho `beatmap_comments` so it also shows on
    the website)."""
    payload = await _read_comment_payload(request)
    if (payload.get("commentable_type") or _BEATMAPSET) != _BEATMAPSET:
        raise RequestError(ErrorType.INVALID_REQUEST)
    set_id = int(payload.get("commentable_id") or 0)
    message = (payload.get("message") or "").strip()
    parent_id = payload.get("parent_id")
    if not set_id or not message:
        raise RequestError(ErrorType.INVALID_REQUEST)

    eng = get_bancho_engine()
    async with eng.begin() as conn:
        result = await conn.execute(
            text(
                """
                INSERT INTO beatmap_comments (set_id, user_id, parent_id, content, created_at)
                VALUES (:sid, :uid, :pid, :content, NOW())
                """,
            ),
            {
                "sid": set_id,
                "uid": int(current_user.id),
                "pid": int(parent_id) if parent_id else None,
                "content": message,
            },
        )
        new_id = int(result.lastrowid)

    return await _bundle(db, set_id, parent_id=0, sort="new", extra_comment_ids={new_id})


@router.delete("/comments/{comment_id}", tags=["Comments"], name="Delete a comment")
async def delete_comment(
    db: Database,
    current_user: ClientUser,
    comment_id: Annotated[int, Path()],
) -> dict[str, Any]:
    """Delete a comment (own comments only) from bancho `beatmap_comments`."""
    eng = get_bancho_engine()
    async with eng.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT set_id, user_id FROM beatmap_comments WHERE id = :id"),
                {"id": comment_id},
            )
        ).mappings().first()
        if row is None:
            raise RequestError(ErrorType.NOT_FOUND)
        if int(row["user_id"]) != int(current_user.id):
            raise RequestError(ErrorType.FORBIDDEN)
        set_id = int(row["set_id"])
        # Remove the comment and any direct replies to it.
        await conn.execute(
            text("DELETE FROM beatmap_comments WHERE id = :id OR parent_id = :id"),
            {"id": comment_id},
        )
    return await _bundle(db, set_id, parent_id=0, sort="new")


@router.post("/comments/{comment_id}/vote", tags=["Comments"], name="Vote a comment")
@router.delete("/comments/{comment_id}/vote", tags=["Comments"], name="Remove comment vote")
async def vote_comment(
    db: Database,
    current_user: ClientUser,
    comment_id: Annotated[int, Path()],
) -> dict[str, Any]:
    """Votes aren't stored by bancho; accept and return the comment (count 0)."""
    eng = get_bancho_engine()
    async with eng.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT set_id FROM beatmap_comments WHERE id = :id"),
                {"id": comment_id},
            )
        ).mappings().first()
    if row is None:
        raise RequestError(ErrorType.NOT_FOUND)
    return await _bundle(db, int(row["set_id"]), parent_id=0, sort="new", extra_comment_ids={comment_id})
