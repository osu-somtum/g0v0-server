from enum import IntEnum
from typing import Annotated, Any, Literal

from .score import Rank

from pydantic import BaseModel, BeforeValidator, Field, PlainSerializer


class BeatmapRankStatus(IntEnum):
    GRAVEYARD = -2
    WIP = -1
    PENDING = 0
    RANKED = 1
    APPROVED = 2
    QUALIFIED = 3
    LOVED = 4

    def has_leaderboard(self) -> bool:
        return self in {
            BeatmapRankStatus.RANKED,
            BeatmapRankStatus.APPROVED,
            BeatmapRankStatus.QUALIFIED,
            BeatmapRankStatus.LOVED,
        }

    def has_pp(self) -> bool:
        return self in {
            BeatmapRankStatus.RANKED,
            BeatmapRankStatus.APPROVED,
        }

    def ranked(self) -> bool:
        # https://osu.ppy.sh/wiki/Gameplay/Score/Ranked_score
        return self in {BeatmapRankStatus.RANKED, BeatmapRankStatus.APPROVED, BeatmapRankStatus.LOVED}


class Genre(IntEnum):
    ANY = 0
    UNSPECIFIED = 1
    VIDEO_GAME = 2
    ANIME = 3
    ROCK = 4
    POP = 5
    OTHER = 6
    NOVELTY = 7
    HIP_HOP = 9
    ELECTRONIC = 10
    METAL = 11
    CLASSICAL = 12
    FOLK = 13
    JAZZ = 14


class Language(IntEnum):
    ANY = 0
    UNSPECIFIED = 1
    ENGLISH = 2
    JAPANESE = 3
    CHINESE = 4
    INSTRUMENTAL = 5
    KOREAN = 6
    FRENCH = 7
    GERMAN = 8
    SWEDISH = 9
    ITALIAN = 10
    SPANISH = 11
    RUSSIAN = 12
    POLISH = 13
    OTHER = 14


def _parse_list(v: Any):
    if isinstance(v, str):
        return v.split(".")
    return v


def _parse_played(v: Any):
    """osu! sends played=played|unplayed (string); older/bool inputs are coerced."""
    if isinstance(v, bool):
        return "played" if v else None
    if v in (None, "", "null", "all"):
        return None
    if v in ("played", "unplayed"):
        return v
    return None


_LANGUAGE_NAMES = frozenset(
    {
        "any", "unspecified", "english", "japanese", "chinese", "instrumental",
        "korean", "french", "german", "swedish", "spanish", "italian", "russian",
        "polish", "other",
    },
)


def _parse_language(v: Any):
    """osu! sends `l` as a numeric language id; our model uses name strings. Map
    unknown/numeric values to "any" (no filter) instead of 422-ing the search."""
    if isinstance(v, str) and v in _LANGUAGE_NAMES:
        return v
    return "any"


class SearchQueryModel(BaseModel):
    """Beatmap search query parameters model."""

    # model_config = ConfigDict(serialize_by_alias=True)

    q: str = Field("", description="Search keywords")
    c: Annotated[
        list[Literal["recommended", "converts", "follows", "spotlights", "featured_artists", "somtum"]],
        BeforeValidator(_parse_list),
        PlainSerializer(lambda x: ".".join(x)),
    ] = Field(
        default_factory=list,
        description=(
            "General filters: recommended / converts (include converts) / "
            "follows (followed mappers) / spotlights / featured_artists / "
            "somtum (osu!somtum-uploaded sets only)"
        ),
    )
    m: int | None = Field(None, description="Game mode", alias="m")
    s: Literal[
        "any",
        "leaderboard",
        "ranked",
        "qualified",
        "loved",
        "favourites",
        "pending",
        "wip",
        "graveyard",
        "mine",
    ] = Field(
        default="leaderboard",
        description=(
            "Category: any / leaderboard (has leaderboard) / ranked / "
            "qualified / loved / favourites / pending / wip / graveyard / mine"
        ),
    )
    l: Annotated[  # noqa: E741
        Literal[
        "any",
        "unspecified",
        "english",
        "japanese",
        "chinese",
        "instrumental",
        "korean",
        "french",
        "german",
        "swedish",
        "spanish",
        "italian",
        "russian",
        "polish",
        "other",
        ],
        BeforeValidator(_parse_language),
    ] = Field(
        default="any",
        description=(
            "Language: any / unspecified / english / japanese / chinese / "
            "instrumental / korean / french / german / swedish / spanish / "
            "italian / russian / polish / other"
        ),
    )
    sort: Literal[
        "title_asc",
        "artist_asc",
        "difficulty_asc",
        "updated_asc",
        "ranked_asc",
        "rating_asc",
        "plays_asc",
        "favourites_asc",
        "relevance_asc",
        "nominations_asc",
        "title_desc",
        "artist_desc",
        "difficulty_desc",
        "updated_desc",
        "ranked_desc",
        "rating_desc",
        "plays_desc",
        "favourites_desc",
        "relevance_desc",
        "nominations_desc",
    ] = Field(
        ...,
        description=(
            "Sort by: title / artist / difficulty / updated / ranked / "
            "rating / plays / favourites / relevance / nominations"
        ),
    )
    e: Annotated[
        list[Literal["video", "storyboard"]],
        BeforeValidator(_parse_list),
        PlainSerializer(lambda x: ".".join(x)),
    ] = Field(default_factory=list, description="Extra: video / storyboard")
    r: Annotated[list[Rank], BeforeValidator(_parse_list), PlainSerializer(lambda x: ".".join(x))] = Field(
        default_factory=list, description="Achieved ranks"
    )
    played: Annotated[Literal["played", "unplayed"] | None, BeforeValidator(_parse_played)] = Field(
        default=None,
        description="Played filter: played / unplayed (osu! sends a string, not a bool)",
    )
    nsfw: bool = Field(
        default=False,
        description="Include explicit content",
    )
    cursor_string: str | None = Field(
        default=None,
        description="Cursor string for pagination",
    )


SearchQueryModel.model_rebuild()
