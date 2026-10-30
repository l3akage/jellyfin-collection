"""Series status tags (replacement for TSSK: "TV Show Status for Kometa").

Writes Jellyfin tags on series so that an overlay renderer (JellyTag) can show a status bar:

    NewShow                        added to the library within the last N days
    NewSeasonSoon:<season>:<date>  season premiere within N days
    NewSeasonStarted:<season>      season premiere aired within the last N days
    UpcomingEpisode:<date>         next episode within N days
    UpcomingFinale:<date>          series finale within N days
    SeasonFinale:<season>          season finale aired within the last N days
    FinalEpisode                   series finale aired within the last N days
    ReturningShows / EndedShows / CanceledShows   TMDb status

`<date>` is ISO (YYYY-MM-DD). Plain names never carry parameters, so Kometa-style label
filters keep working (`label: NewShow`).
"""

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

from loguru import logger

from jfc.models.media import LibraryItem, MediaType

STATUS_TAG_NAMES = (
    "NewShow",
    "NewSeasonSoon",
    "NewSeasonStarted",
    "UpcomingEpisode",
    "UpcomingFinale",
    "SeasonFinale",
    "FinalEpisode",
    "ReturningShows",
    "EndedShows",
    "CanceledShows",
)

COMING_SOON_LABEL = "Coming Soon"


@dataclass
class StatusTagSettings:
    """Mirrors the TSSK options of the same name."""

    library: Optional[str] = None
    recent_days_new_show: int = 7
    future_days_new_season: int = 31
    recent_days_new_season_started: int = 7
    future_days_upcoming_episode: int = 31
    future_days_upcoming_finale: int = 31
    recent_days_season_finale: int = 7
    recent_days_final_episode: int = 7
    process_new_shows: bool = True
    process_new_season_soon: bool = True
    process_new_season_started: bool = True
    process_upcoming_episode: bool = True
    process_upcoming_finale: bool = True
    process_season_finale: bool = True
    process_final_episode: bool = True
    process_returning_shows: bool = True
    process_ended_shows: bool = True
    process_canceled_shows: bool = True
    ignore_finales_tag: str = "ignorefinales"

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "StatusTagSettings":
        data = data or {}
        settings = cls()
        for key, value in data.items():
            normalized = {"process_new_show": "process_new_shows"}.get(key, key)
            if hasattr(settings, normalized) and value is not None:
                default = getattr(settings, normalized)
                try:
                    if default is None or isinstance(default, str):
                        setattr(settings, normalized, str(value))
                    elif isinstance(default, bool):
                        setattr(settings, normalized, bool(value))
                    else:
                        setattr(settings, normalized, int(value))
                except (TypeError, ValueError):
                    logger.warning(f"status_tags: invalid value for '{key}': {value!r}")
        return settings


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def compute_status_tags(
    *,
    schedule: Optional[dict[str, Any]],
    date_added: Optional[datetime],
    current_tags: list[str],
    today: date,
    settings: StatusTagSettings,
) -> list[str]:
    """Desired status tags of one series."""
    tags: list[str] = []
    lowered = {t.lower() for t in current_tags}
    ignore_finales = settings.ignore_finales_tag.lower() in lowered
    coming_soon = COMING_SOON_LABEL.lower() in lowered

    # NewShow: purely library based, works without TMDb data
    if settings.process_new_shows and date_added and not coming_soon:
        added = date_added.date()
        if 0 <= (today - added).days <= settings.recent_days_new_show:
            tags.append("NewShow")

    if not schedule:
        return tags

    status = str(schedule.get("status") or "").lower()
    ended = status == "ended"
    canceled = status == "canceled"
    last = schedule.get("last_episode") or {}
    nxt = schedule.get("next_episode") or {}

    next_date = _parse_date(nxt.get("air_date"))
    last_date = _parse_date(last.get("air_date"))
    next_season = nxt.get("season_number")
    last_season = last.get("season_number")
    next_is_finale = str(nxt.get("episode_type") or "").lower() == "finale"
    last_is_finale = str(last.get("episode_type") or "").lower() == "finale"

    days_to_next = (next_date - today).days if next_date else None
    days_since_last = (today - last_date).days if last_date else None

    has_upcoming = False

    if next_date and days_to_next is not None and days_to_next >= 0:
        is_premiere = nxt.get("episode_number") == 1 and (next_season or 0) >= 2
        if (
            settings.process_new_season_soon
            and is_premiere
            and days_to_next <= settings.future_days_new_season
        ):
            tags.append(f"NewSeasonSoon:{next_season}:{next_date.isoformat()}")
            has_upcoming = True

        if (
            settings.process_upcoming_finale
            and not ignore_finales
            and next_is_finale
            and (ended or canceled)
            and days_to_next <= settings.future_days_upcoming_finale
        ):
            tags.append(f"UpcomingFinale:{next_date.isoformat()}")
            has_upcoming = True

        if (
            settings.process_upcoming_episode
            and not is_premiere
            and days_to_next <= settings.future_days_upcoming_episode
        ):
            tags.append(f"UpcomingEpisode:{next_date.isoformat()}")
            has_upcoming = True

    if last_date and days_since_last is not None and days_since_last >= 0:
        if (
            settings.process_new_season_started
            and last.get("episode_number") == 1
            and (last_season or 0) >= 2
            and days_since_last <= settings.recent_days_new_season_started
        ):
            tags.append(f"NewSeasonStarted:{last_season}")

        if last_is_finale and not ignore_finales:
            if ended or canceled:
                if settings.process_final_episode and days_since_last <= settings.recent_days_final_episode:
                    tags.append("FinalEpisode")
            elif settings.process_season_finale and days_since_last <= settings.recent_days_season_finale:
                tags.append(f"SeasonFinale:{last_season}")

    if status == "returning series" and settings.process_returning_shows and not has_upcoming:
        tags.append("ReturningShows")
    if ended and settings.process_ended_shows:
        tags.append("EndedShows")
    if canceled and settings.process_canceled_shows:
        tags.append("CanceledShows")

    return tags


def tag_name(tag: str) -> str:
    """`NewSeasonSoon:2:2026-11-03` -> `NewSeasonSoon`."""
    return tag.split(":", 1)[0]


class StatusTagService:
    """Computes and writes the status tags of a series library."""

    def __init__(self, jellyfin: Any, tmdb: Any, settings: StatusTagSettings, dry_run: bool = False):
        self.jellyfin = jellyfin
        self.tmdb = tmdb
        self.settings = settings
        self.dry_run = dry_run

    async def run(self, library_id: str, today: Optional[date] = None) -> dict[str, int]:
        """Update the status tags of every series in the library. Returns change counters."""
        today = today or date.today()
        series: list[LibraryItem] = await self.jellyfin.get_library_items(
            library_id=library_id, media_type=MediaType.SERIES, limit=50000, detailed=True
        )
        logger.info(f"[StatusTags] Checking {len(series)} series")

        schedules = await self._load_schedules(series)

        known = {n.lower() for n in STATUS_TAG_NAMES}
        semaphore = asyncio.Semaphore(4)
        stats = {"series": len(series), "changed": 0, "failed": 0}

        async def update(item: LibraryItem) -> None:
            desired = compute_status_tags(
                schedule=schedules.get(item.tmdb_id) if item.tmdb_id else None,
                date_added=item.date_added,
                current_tags=item.tags,
                today=today,
                settings=self.settings,
            )
            current_status = [t for t in item.tags if tag_name(t).lower() in known]
            add = [t for t in desired if t not in current_status]
            remove = [t for t in current_status if t not in desired]
            if not add and not remove:
                return

            if self.dry_run:
                logger.info(f"[DRY RUN] {item.title}: +{add} -{remove}")
                stats["changed"] += 1
                return

            async with semaphore:
                ok = await self.jellyfin.update_item_tags(item.jellyfin_id, add=add, remove=remove)
            stats["changed" if ok else "failed"] += 1

        await asyncio.gather(*(update(s) for s in series))
        logger.info(f"[StatusTags] {stats['series']} series, {stats['changed']} updated, {stats['failed']} failed")
        return stats

    async def _load_schedules(self, series: list[LibraryItem]) -> dict[int, dict[str, Any]]:
        ids = sorted({s.tmdb_id for s in series if s.tmdb_id})
        schedules: dict[int, dict[str, Any]] = {}
        semaphore = asyncio.Semaphore(8)
        done = 0
        logger.info(f"[StatusTags] Loading TMDb schedules for {len(ids)} series (this can take a minute)")

        async def fetch(tmdb_id: int) -> None:
            async with semaphore:
                try:
                    info = await self.tmdb.get_series_schedule(tmdb_id)
                except Exception as e:
                    logger.warning(f"[StatusTags] TMDb lookup failed for {tmdb_id}: {e}")
                    return
            if info:
                schedules[tmdb_id] = info
            nonlocal done
            done += 1
            if done % 250 == 0:
                logger.info(f"[StatusTags] TMDb schedules: {done}/{len(ids)}")

        await asyncio.gather(*(fetch(i) for i in ids))
        return schedules
