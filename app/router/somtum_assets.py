"""Somtum dual-bancho: serve custom-map assets that osu!'s CDN/mirrors lack.

bancho.py stores uploaded ("private") beatmaps' full zips and backgrounds locally
(`.data/osz/{set_id}.osz`, `.data/bg/{set_id}.jpg`). Those dirs are mounted
read-only into this container (see docker-compose.somtum.yml). lazer can't fetch
these from external mirrors (id >= SOMTUM_SET_ID_FLOOR doesn't exist there), so we
serve them directly:

  GET /somtum/osz/{set_id}      -> the .osz (used by the beatmapset download route)
  GET /somtum/bg/{set_id}       -> full background (beatmapset cover)
  GET /somtum/bg/{set_id}/thumb -> small background (card/list thumbnail)
  GET /somtum/preview/{set_id}  -> per-set audio preview (lazer in-client song preview)
  GET /somtum/user/{id}/banner  -> website user banner (lazer profile cover)

Public (no auth) — same as /file. Local-storage assumption.
"""

from __future__ import annotations

from pathlib import Path

from app.config import settings
from app.models.error import ErrorType, RequestError

from fastapi import APIRouter
from fastapi.responses import FileResponse

somtum_router = APIRouter(prefix="/somtum", include_in_schema=False)

# bancho writes backgrounds as either .jpg or .png; thumbnails as .thumb.jpg
# (small) / .thumbl.jpg (large). Probe in preference order.
_BG_FULL = ("{i}.jpg", "{i}.png")
_BG_THUMB = ("{i}.thumbl.jpg", "{i}.thumb.jpg", "{i}.jpg", "{i}.png")
# bancho clan avatars/banners are stored under <clan_assets>/avatar|banners/{id}.{ext}.
_CLAN_IMG = ("{i}.png", "{i}.jpg", "{i}.jpeg", "{i}.gif", "{i}.webp")
# bancho writes per-set audio previews as .mp3 (some older sets as .ogg).
_AUDIO = ("{i}.mp3", "{i}.ogg")
# the website stores user banners as <banners>/{id}.{ext}.
_USER_BANNER = ("{i}.png", "{i}.jpg", "{i}.jpeg", "{i}.gif", "{i}.webp")
_EXT_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
_AUDIO_MIME = {"mp3": "audio/mpeg", "ogg": "audio/ogg"}


def _first_existing(dir_path: str, candidates: tuple[str, ...], set_id: int) -> Path | None:
    base = Path(dir_path)
    for pat in candidates:
        p = base / pat.format(i=set_id)
        if p.is_file():
            return p
    return None


@somtum_router.get("/osz/{set_id}")
async def get_custom_osz(set_id: int):
    """Serve a custom beatmapset's local .osz zip."""
    p = Path(settings.bancho_osz_dir) / f"{set_id}.osz"
    if not p.is_file():
        raise RequestError(ErrorType.NOT_FOUND)
    return FileResponse(
        path=str(p),
        media_type="application/x-osu-beatmap-archive",
        filename=f"{set_id}.osz",
    )


@somtum_router.get("/bg/{set_id}")
async def get_custom_bg(set_id: int):
    """Serve a custom beatmapset's full background (used as the cover)."""
    p = _first_existing(settings.bancho_bg_dir, _BG_FULL, set_id)
    if p is None:
        raise RequestError(ErrorType.NOT_FOUND)
    return FileResponse(path=str(p), media_type="image/jpeg")


@somtum_router.get("/bg/{set_id}/thumb")
async def get_custom_bg_thumb(set_id: int):
    """Serve a custom beatmapset's thumbnail (card/list cover)."""
    p = _first_existing(settings.bancho_bg_dir, _BG_THUMB, set_id)
    if p is None:
        raise RequestError(ErrorType.NOT_FOUND)
    return FileResponse(path=str(p), media_type="image/jpeg")


@somtum_router.get("/preview/{set_id}")
async def get_custom_preview(set_id: int):
    """Serve a custom beatmapset's audio preview (lazer's in-client song preview).

    osu!'s b.ppy.sh preview CDN has nothing for somtum (id >= 1e8) sets, so the
    importer points their `preview_url` here and we stream bancho's local
    `.data/audio/{set_id}.mp3|ogg`."""
    p = _first_existing(settings.bancho_audio_dir, _AUDIO, set_id)
    if p is None:
        raise RequestError(ErrorType.NOT_FOUND)
    return FileResponse(
        path=str(p),
        media_type=_AUDIO_MIME.get(p.suffix.lstrip("."), "audio/mpeg"),
        headers={"Cache-Control": "public, max-age=604800"},
    )


def _default_user_banner() -> Path | None:
    """The website's shared default banner (banners/default.jpeg), used when a
    user hasn't uploaded their own."""
    for name in ("default.jpeg", "default.jpg", "default.png"):
        p = Path(settings.bancho_user_banner_dir) / name
        if p.is_file():
            return p
    return None


@somtum_router.get("/user/{user_id}/banner")
async def get_user_banner(user_id: int):
    """Serve a user's profile banner (= the website's user banner), used as the
    lazer profile `cover`. The bancho->lazer user trigger can't fill this (it has
    no filesystem access), so it's bridged here from /var/www/assets/banners.
    Falls back to the shared default.jpeg so every profile renders a cover."""
    p = _first_existing(settings.bancho_user_banner_dir, _USER_BANNER, user_id) or _default_user_banner()
    if p is None:
        raise RequestError(ErrorType.NOT_FOUND)
    return FileResponse(path=str(p), media_type=_EXT_MIME.get(p.suffix.lstrip("."), "image/png"))


def clan_avatar_path(team_id: int) -> Path | None:
    """Clan avatar file (team_id == clan id), probing both dir spellings the
    website uses (`avatars/` and `avatar/`)."""
    base = settings.bancho_clan_assets_dir
    return _first_existing(str(Path(base) / "avatars"), _CLAN_IMG, team_id) or _first_existing(
        str(Path(base) / "avatar"), _CLAN_IMG, team_id
    )


def clan_banner_path(team_id: int) -> Path | None:
    return _first_existing(str(Path(settings.bancho_clan_assets_dir) / "banners"), _CLAN_IMG, team_id)


def _default_avatar() -> Path | None:
    for sub in ("avatar", "avatars"):
        p = Path(settings.bancho_clan_assets_dir) / sub / "default.jpg"
        if p.is_file():
            return p
    return None


@somtum_router.get("/team/{team_id}/flag")
async def get_team_flag(team_id: int):
    """Serve a team's flag/avatar (= website clan avatar; team_id == clan id),
    falling back to the shared default.jpg so teams without a custom one still
    render an image."""
    p = clan_avatar_path(team_id) or _default_avatar()
    if p is None:
        raise RequestError(ErrorType.NOT_FOUND)
    return FileResponse(path=str(p), media_type=_EXT_MIME.get(p.suffix.lstrip("."), "image/png"))


@somtum_router.get("/team/{team_id}/banner")
async def get_team_banner(team_id: int):
    """Serve a team's banner/cover (= website clan banner)."""
    p = clan_banner_path(team_id)
    if p is None:
        raise RequestError(ErrorType.NOT_FOUND)
    return FileResponse(path=str(p), media_type=_EXT_MIME.get(p.suffix.lstrip("."), "image/png"))
