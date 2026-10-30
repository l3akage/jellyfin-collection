"""Media item models."""

from datetime import date, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class MediaType(str, Enum):
    """Media type enumeration."""

    MOVIE = "movie"
    SERIES = "series"
    EPISODE = "episode"
    SEASON = "season"


class MediaItem(BaseModel):
    """Base media item model."""

    title: str
    year: Optional[int] = None
    media_type: MediaType

    # External IDs
    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None
    tvdb_id: Optional[int] = None

    # Jellyfin ID (if exists in library)
    jellyfin_id: Optional[str] = None

    # Metadata
    overview: Optional[str] = None
    genres: list[str | int] = Field(default_factory=list)  # Can be genre names or IDs
    original_language: Optional[str] = None
    original_country: Optional[str] = None
    vote_average: Optional[float] = None
    vote_count: Optional[int] = None
    popularity: Optional[float] = None

    # Release info
    release_date: Optional[date] = None
    status: Optional[str] = None  # Released, In Production, Ended, etc.

    # Images
    poster_path: Optional[str] = None
    backdrop_path: Optional[str] = None

    @property
    def display_title(self) -> str:
        """Get display title with year."""
        if self.year:
            return f"{self.title} ({self.year})"
        return self.title


class Movie(MediaItem):
    """Movie-specific model."""

    media_type: MediaType = MediaType.MOVIE

    # Movie-specific fields
    runtime: Optional[int] = None  # minutes
    budget: Optional[int] = None
    revenue: Optional[int] = None
    tagline: Optional[str] = None

    # Collection info (e.g., "The Dark Knight Collection")
    belongs_to_collection: Optional[str] = None


class Series(MediaItem):
    """Series-specific model."""

    media_type: MediaType = MediaType.SERIES

    # Series-specific fields
    first_air_date: Optional[date] = None
    last_air_date: Optional[date] = None
    number_of_seasons: Optional[int] = None
    number_of_episodes: Optional[int] = None
    episode_run_time: list[int] = Field(default_factory=list)
    in_production: bool = False

    # Network info
    networks: list[str] = Field(default_factory=list)


class ProviderMatch(BaseModel):
    """Match result from a provider search."""

    item: MediaItem
    confidence: float = Field(ge=0.0, le=1.0)
    source: str  # tmdb, trakt, jellyfin, etc.

    @property
    def is_exact_match(self) -> bool:
        """Check if this is an exact match."""
        return self.confidence >= 0.95


class LibraryItem(BaseModel):
    """Item from a media library (Jellyfin)."""

    jellyfin_id: str
    title: str
    year: Optional[int] = None
    media_type: MediaType

    # External IDs
    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None
    tvdb_id: Optional[int] = None

    # Library info
    library_id: str
    library_name: str

    # File info
    path: Optional[str] = None
    file_name: Optional[str] = None
    genres: list[str] = Field(default_factory=list)

    # Detailed fields (only filled when the library is loaded with detailed=True)
    date_added: Optional[datetime] = None
    premiere_date: Optional[date] = None
    tags: list[str] = Field(default_factory=list)
    studios: list[str] = Field(default_factory=list)
    resolution: Optional[str] = None  # "4k", "1080", "720", "576", "480", "sd"
    dolby_vision: bool = False
    hdr: bool = False
    audio_languages: list[str] = Field(default_factory=list)  # ISO 639-1 where known
    subtitle_languages: list[str] = Field(default_factory=list)
    edition: Optional[str] = None  # parsed from "{edition-...}" in the file name
    status: Optional[str] = None  # Series: Continuing / Ended
    community_rating: Optional[float] = None
    critic_rating: Optional[float] = None
    official_rating: Optional[str] = None
    runtime_minutes: Optional[int] = None
    overview: Optional[str] = None

    def to_media_item(self) -> MediaItem:
        """Convert to MediaItem."""
        return MediaItem(
            title=self.title,
            year=self.year,
            media_type=self.media_type,
            tmdb_id=self.tmdb_id,
            imdb_id=self.imdb_id,
            tvdb_id=self.tvdb_id,
            jellyfin_id=self.jellyfin_id,
            genres=self.genres,
        )


def infer_media_type(library_name: str) -> MediaType:
    """Infer the media type of a Jellyfin library from its name (movie by default)."""
    name_lower = library_name.lower()

    if any(keyword in name_lower for keyword in ["film", "movie", "cinéma"]):
        return MediaType.MOVIE

    if any(keyword in name_lower for keyword in ["série", "serie", "tv", "show", "cartoon"]):
        return MediaType.SERIES

    return MediaType.MOVIE
