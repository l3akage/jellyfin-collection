"""Tests for upcoming placeholders/tags (UMTK replacement)."""

from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from jfc.models.media import LibraryItem, MediaType
from jfc.services.upcoming import (
    MARKER,
    PlaceholderStore,
    UpcomingSection,
    UpcomingService,
    UpcomingSettings,
    UpcomingTitle,
    placeholder_folder_name,
    select_upcoming_movies,
    select_upcoming_shows,
)

TODAY = date(2026, 10, 30)
SECTION = UpcomingSection(library="Filme", future_days=30, past_days=14)


def movie(**kw):
    base = {"title": "M", "year": 2026, "tmdbId": 1, "monitored": True, "hasFile": False, "tags": []}
    base.update(kw)
    return base


def test_movie_window_and_flags():
    movies = [
        movie(title="soon", tmdbId=1, digitalRelease="2026-11-10T00:00:00Z"),
        movie(title="released", tmdbId=2, digitalRelease="2026-10-25T00:00:00Z"),
        movie(title="too far", tmdbId=3, digitalRelease="2027-03-01T00:00:00Z"),
        movie(title="too old", tmdbId=4, digitalRelease="2026-06-01T00:00:00Z"),
        movie(title="has file", tmdbId=5, digitalRelease="2026-11-10T00:00:00Z", hasFile=True),
        movie(title="unmonitored", tmdbId=6, digitalRelease="2026-11-10T00:00:00Z", monitored=False),
        movie(title="tagged", tmdbId=7, digitalRelease="2026-11-10T00:00:00Z", tags=[9]),
        movie(title="cinema only", tmdbId=8, inCinemas="2026-11-10T00:00:00Z"),
        movie(title="physical", tmdbId=9, physicalRelease="2026-11-12T00:00:00Z"),
    ]
    result = select_upcoming_movies(movies, {9: "Private"}, TODAY, SECTION)
    assert [t.title for t in result] == ["released", "soon", "physical"]
    assert [t.future for t in result] == [False, True, True]

    with_cinema = UpcomingSection(library="Filme", include_in_cinemas=True)
    assert "cinema only" in [t.title for t in select_upcoming_movies(movies, {9: "x"}, TODAY, with_cinema)]


def test_tags():
    future = UpcomingTitle("A", 2026, 1, None, date(2026, 11, 3), True)
    released = UpcomingTitle("B", 2026, 2, None, date(2026, 10, 28), False)
    show = UpcomingTitle("C", 2026, 3, 30, date(2026, 11, 3), True, is_series=True)
    assert future.tags == ["Coming Soon", "Expected:2026-11-03"]
    assert released.tags == ["Coming Soon"]
    assert show.tags == ["Coming Soon", "Premieres:2026-11-03"]


def test_shows_without_files_only():
    series = [
        {"title": "New", "year": 2026, "tvdbId": 10, "tmdbId": 1, "firstAired": "2026-11-05", "monitored": True, "statistics": {"episodeFileCount": 0}, "tags": []},
        {"title": "Have", "tvdbId": 11, "firstAired": "2026-11-05", "monitored": True, "statistics": {"episodeFileCount": 3}, "tags": []},
        {"title": "Old", "tvdbId": 12, "firstAired": "2020-01-01", "monitored": True, "statistics": {"episodeFileCount": 0}, "tags": []},
    ]
    result = select_upcoming_shows(series, {}, TODAY, UpcomingSection())
    assert [t.title for t in result] == ["New"] and result[0].is_series


def test_folder_name_sanitized():
    assert placeholder_folder_name('Mission: Impossible?', 2018, 177677, False) == "Mission Impossible (2018) [tmdbid-177677]"
    assert placeholder_folder_name("Show", None, 5, True) == "Show [tvdbid-5]"


def test_store_create_and_safe_cleanup(tmp_path: Path):
    video = tmp_path / "ph.mp4"
    video.write_bytes(b"x")
    root = tmp_path / "umtk"
    store = PlaceholderStore(root, video)

    a = UpcomingTitle("A", 2026, 1, None, date(2026, 11, 3), True)
    s = UpcomingTitle("S", 2026, 2, 20, date(2026, 11, 3), True, is_series=True)
    assert store.create(a) and store.create(s)
    assert not store.create(a)  # idempotent
    assert (root / a.folder_name / f"{a.folder_name}.mp4").is_file()
    assert (root / s.folder_name / "Season 01" / "S - S01E01.mp4").is_file()

    # a foreign folder without marker must survive cleanup
    foreign = root / "Real Movie (2000)"
    foreign.mkdir()
    (foreign / "movie.mkv").write_bytes(b"real")

    removed = store.cleanup({s.folder_name})
    assert removed == [a.folder_name]
    assert not (root / a.folder_name).exists()
    assert (root / s.folder_name / MARKER).exists()
    assert foreign.exists()


def test_store_refuses_filesystem_root():
    with pytest.raises(ValueError):
        PlaceholderStore(Path("/"), None)


def test_store_without_video_creates_nothing(tmp_path: Path):
    store = PlaceholderStore(tmp_path / "r", None)
    assert not store.create(UpcomingTitle("A", 2026, 1, None, date(2026, 11, 3), True))
    assert not (tmp_path / "r").exists()


def test_settings_from_dict():
    s = UpcomingSettings.from_dict({"enabled": True, "movies": {"library": "Filme", "future_days": 10}, "tv": {"placeholder_root": "/t"}})
    assert s.enabled and s.movies.library == "Filme" and s.movies.future_days == 10 and s.tv.placeholder_root == "/t"
    assert UpcomingSettings.from_dict(None).enabled is False


@pytest.mark.asyncio
async def test_service_tags_and_cleans_stale(tmp_path: Path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    items = [
        LibraryItem(jellyfin_id="m1", title="Soon", media_type=MediaType.MOVIE, library_id="F", library_name="Filme", tmdb_id=1, tags=["Coming Soon", "Expected:2026-01-01"]),
        LibraryItem(jellyfin_id="m2", title="Done", media_type=MediaType.MOVIE, library_id="F", library_name="Filme", tmdb_id=2, tags=["Coming Soon", "Keep"]),
    ]
    jellyfin = MagicMock()
    jellyfin.get_library_items = AsyncMock(return_value=items)
    jellyfin.update_item_tags = AsyncMock(return_value=True)
    jellyfin.refresh_library = AsyncMock(return_value=True)
    radarr = MagicMock()
    radarr.get_movies = AsyncMock(return_value=[movie(title="Soon", tmdbId=1, digitalRelease="2026-11-10T00:00:00Z")])
    radarr.get_tags = AsyncMock(return_value=[])

    settings = UpcomingSettings.from_dict(
        {"enabled": True, "placeholder_video": str(video), "movies": {"library": "Filme", "placeholder_root": str(tmp_path / "umtk")}}
    )
    service = UpcomingService(jellyfin, radarr, None, settings, tmp_path / "data")
    stats = await service.run({"Filme": "F"}, today=TODAY)

    assert stats["created"] == 1 and stats["titles"] == 1
    calls = {c.args[0]: c.kwargs for c in jellyfin.update_item_tags.await_args_list}
    assert calls["m1"] == {"add": ["Expected:2026-11-10"], "remove": ["Expected:2026-01-01"]}
    assert calls["m2"] == {"add": [], "remove": ["Coming Soon"]}  # "Keep" is untouched
    jellyfin.refresh_library.assert_awaited_once()
