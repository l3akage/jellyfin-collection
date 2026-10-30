"""Tests for series status tags (TSSK replacement)."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from jfc.models.media import LibraryItem, MediaType
from jfc.services.status_tags import StatusTagService, StatusTagSettings, compute_status_tags, tag_name

TODAY = date(2026, 10, 30)
S = StatusTagSettings()


def ep(days: int, season: int, number: int, kind: str = "standard") -> dict:
    return {
        "air_date": (TODAY + timedelta(days=days)).isoformat(),
        "season_number": season,
        "episode_number": number,
        "episode_type": kind,
    }


def tags(schedule=None, added_days_ago=100, current=None, settings=S):
    added = datetime(2026, 10, 30, tzinfo=timezone.utc) - timedelta(days=added_days_ago)
    return compute_status_tags(
        schedule=schedule, date_added=added, current_tags=current or [], today=TODAY, settings=settings
    )


def test_new_show():
    assert "NewShow" in tags(added_days_ago=2)
    assert "NewShow" not in tags(added_days_ago=30)
    assert "NewShow" not in tags(added_days_ago=2, current=["Coming Soon"])


def test_new_season_soon_with_date():
    result = tags({"status": "Returning Series", "next_episode": ep(10, 3, 1), "last_episode": ep(-100, 2, 8, "finale")})
    assert f"NewSeasonSoon:3:{(TODAY + timedelta(days=10)).isoformat()}" in result
    assert not any(t.startswith("UpcomingEpisode") for t in result)
    assert "ReturningShows" not in result  # has an upcoming event


def test_new_season_too_far_away():
    result = tags({"status": "Returning Series", "next_episode": ep(60, 3, 1), "last_episode": ep(-100, 2, 8)})
    assert not any(t.startswith("NewSeasonSoon") for t in result)
    assert "ReturningShows" in result


def test_upcoming_episode_and_new_season_started():
    started = tags({"status": "Returning Series", "next_episode": ep(7, 2, 2), "last_episode": ep(-2, 2, 1)})
    assert "NewSeasonStarted:2" in started
    assert f"UpcomingEpisode:{(TODAY + timedelta(days=7)).isoformat()}" in started


def test_season_one_premiere_is_not_new_season():
    result = tags({"status": "Returning Series", "next_episode": ep(5, 1, 2), "last_episode": ep(-2, 1, 1)})
    assert not any(t.startswith("NewSeason") for t in result)


def test_season_finale_vs_final_episode():
    season_end = tags({"status": "Returning Series", "last_episode": ep(-3, 2, 10, "finale")})
    assert "SeasonFinale:2" in season_end and "FinalEpisode" not in season_end
    series_end = tags({"status": "Ended", "last_episode": ep(-3, 5, 10, "finale")})
    assert "FinalEpisode" in series_end and "EndedShows" in series_end
    assert not any(t.startswith("SeasonFinale") for t in series_end)


def test_upcoming_finale_only_when_series_ends():
    ending = tags({"status": "Ended", "next_episode": ep(6, 5, 10, "finale"), "last_episode": ep(-1, 5, 9)})
    assert any(t.startswith("UpcomingFinale:") for t in ending)
    ongoing = tags({"status": "Returning Series", "next_episode": ep(6, 5, 10, "finale"), "last_episode": ep(-1, 5, 9)})
    assert not any(t.startswith("UpcomingFinale") for t in ongoing)


def test_ignore_finales_tag_and_status_tags():
    schedule = {"status": "Ended", "last_episode": ep(-1, 5, 10, "finale")}
    assert "FinalEpisode" not in tags(schedule, current=["ignorefinales"])
    assert "CanceledShows" in tags({"status": "Canceled"})
    assert "EndedShows" not in tags({"status": "Ended"}, settings=StatusTagSettings(process_ended_shows=False))


def test_settings_from_dict_and_tag_name():
    settings = StatusTagSettings.from_dict(
        {"future_days_new_season": 14, "process_canceled_shows": False, "library": "Serien", "unknown": 1}
    )
    assert settings.future_days_new_season == 14 and settings.process_canceled_shows is False
    assert settings.library == "Serien"
    assert tag_name("NewSeasonSoon:2:2026-11-03") == "NewSeasonSoon"


@pytest.mark.asyncio
async def test_service_adds_and_removes_only_its_own_tags():
    item = LibraryItem(
        jellyfin_id="1", title="Show", media_type=MediaType.SERIES, library_id="L", library_name="Serien",
        tmdb_id=10, tags=["Favorite", "EndedShows", "UpcomingEpisode:2026-01-01"],
        date_added=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    jellyfin = MagicMock()
    jellyfin.get_library_items = AsyncMock(return_value=[item])
    jellyfin.update_item_tags = AsyncMock(return_value=True)
    tmdb = MagicMock()
    tmdb.get_series_schedule = AsyncMock(return_value={"status": "Returning Series", "next_episode": ep(3, 2, 4), "last_episode": ep(-4, 2, 3)})

    stats = await StatusTagService(jellyfin, tmdb, S).run("L", today=TODAY)

    assert stats["changed"] == 1
    kwargs = jellyfin.update_item_tags.await_args.kwargs
    assert kwargs["add"] == [f"UpcomingEpisode:{(TODAY + timedelta(days=3)).isoformat()}"]
    assert sorted(kwargs["remove"]) == ["EndedShows", "UpcomingEpisode:2026-01-01"]
    # "Favorite" is not a status tag and must never be touched
