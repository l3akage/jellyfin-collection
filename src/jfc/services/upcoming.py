"""Upcoming movies / shows (replacement for UMTK: "Upcoming Movies & TV Shows for Kometa").

For titles that Radarr/Sonarr will get soon but that are not downloaded yet, JFC

1. creates a small placeholder video in a folder that is part of a Jellyfin library
   (`<root>/<Title> (<Year>) [tmdbid-N]/...`), so the title shows up in Jellyfin with
   artwork and metadata,
2. tags the item `Coming Soon` plus `Expected:<date>` (movies) / `Premieres:<date>` (shows)
   for releases in the future, which an overlay renderer such as JellyTag turns into a banner,
3. removes placeholders again once the real file arrived or the title left the window.

Only folders containing the marker file `.jfc-placeholder` are ever deleted.
"""

import asyncio
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

from loguru import logger

MARKER = ".jfc-placeholder"
COMING_SOON = "Coming Soon"
MOVIE_DATE_TAG = "Expected"
SHOW_DATE_TAG = "Premieres"
_FOLDER_ID_RE = re.compile(r"\[(tmdbid|tvdbid)-(\d+)\]")
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class UpcomingSection:
    library: Optional[str] = None
    placeholder_root: Optional[str] = None
    future_days: int = 30
    past_days: int = 14
    exclude_tags: list[str] = field(default_factory=lambda: ["exclude", "private"])
    include_in_cinemas: bool = False  # movies only


@dataclass
class UpcomingSettings:
    enabled: bool = False
    placeholder_video: Optional[str] = None
    cleanup: bool = True
    movies: UpcomingSection = field(default_factory=UpcomingSection)
    tv: UpcomingSection = field(default_factory=UpcomingSection)

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "UpcomingSettings":
        data = data or {}
        settings = cls(
            enabled=bool(data.get("enabled", False)),
            placeholder_video=data.get("placeholder_video"),
            cleanup=bool(data.get("cleanup", True)),
        )
        for key in ("movies", "tv"):
            section = getattr(settings, key)
            for name, value in (data.get(key) or {}).items():
                if hasattr(section, name) and value is not None:
                    setattr(section, name, value)
        return settings


@dataclass
class UpcomingTitle:
    title: str
    year: Optional[int]
    tmdb_id: Optional[int]
    tvdb_id: Optional[int]
    release: date
    future: bool
    is_series: bool = False

    @property
    def folder_name(self) -> str:
        return placeholder_folder_name(self.title, self.year, self.tvdb_id if self.is_series else self.tmdb_id, self.is_series)

    @property
    def tags(self) -> list[str]:
        tags = [COMING_SOON]
        if self.future:
            tags.append(f"{SHOW_DATE_TAG if self.is_series else MOVIE_DATE_TAG}:{self.release.isoformat()}")
        return tags


def placeholder_folder_name(title: str, year: Optional[int], external_id: Optional[int], is_series: bool) -> str:
    clean = _ILLEGAL.sub("", title).strip().rstrip(".")
    suffix = f" ({year})" if year else ""
    id_part = f" [{'tvdbid' if is_series else 'tmdbid'}-{external_id}]" if external_id else ""
    return f"{clean}{suffix}{id_part}"


def _to_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def select_upcoming_movies(
    movies: list[dict[str, Any]],
    tag_labels: dict[int, str],
    today: date,
    section: UpcomingSection,
) -> list[UpcomingTitle]:
    """Radarr movies without file that release within [today-past_days, today+future_days]."""
    excluded = {t.lower() for t in section.exclude_tags}
    result: list[UpcomingTitle] = []
    for movie in movies:
        if movie.get("hasFile") or not movie.get("monitored", True):
            continue
        labels = {tag_labels.get(t, "").lower() for t in movie.get("tags", [])}
        if labels & excluded:
            continue

        release = _to_date(movie.get("digitalRelease")) or _to_date(movie.get("physicalRelease"))
        if release is None and section.include_in_cinemas:
            release = _to_date(movie.get("inCinemas"))
        if release is None:
            continue

        delta = (release - today).days
        if -section.past_days <= delta <= section.future_days:
            result.append(
                UpcomingTitle(
                    title=movie.get("title") or "Unknown",
                    year=movie.get("year"),
                    tmdb_id=movie.get("tmdbId"),
                    tvdb_id=None,
                    release=release,
                    future=delta > 0,
                )
            )
    return sorted(result, key=lambda t: t.release)


def select_upcoming_shows(
    series: list[dict[str, Any]],
    tag_labels: dict[int, str],
    today: date,
    section: UpcomingSection,
) -> list[UpcomingTitle]:
    """Sonarr series without any episode file whose premiere is within the window."""
    excluded = {t.lower() for t in section.exclude_tags}
    result: list[UpcomingTitle] = []
    for show in series:
        if not show.get("monitored", True):
            continue
        if (show.get("statistics") or {}).get("episodeFileCount", 0) > 0:
            continue
        labels = {tag_labels.get(t, "").lower() for t in show.get("tags", [])}
        if labels & excluded:
            continue

        premiere = _to_date(show.get("firstAired"))
        if premiere is None:
            continue
        delta = (premiere - today).days
        if -section.past_days <= delta <= section.future_days:
            result.append(
                UpcomingTitle(
                    title=show.get("title") or "Unknown",
                    year=show.get("year"),
                    tmdb_id=show.get("tmdbId"),
                    tvdb_id=show.get("tvdbId"),
                    release=premiere,
                    future=delta > 0,
                    is_series=True,
                )
            )
    return sorted(result, key=lambda t: t.release)


class PlaceholderStore:
    """Creates and cleans up placeholder folders below one root."""

    def __init__(self, root: Path, video: Optional[Path], dry_run: bool = False):
        if root.parent == root:
            raise ValueError("placeholder_root must not be the filesystem root")
        self.root = root
        self.video = video
        self.dry_run = dry_run

    def existing(self) -> dict[str, Path]:
        """Placeholder folders (marker present) by folder name."""
        if not self.root.is_dir():
            return {}
        return {p.name: p for p in self.root.iterdir() if p.is_dir() and (p / MARKER).exists()}

    def create(self, title: UpcomingTitle) -> bool:
        """Create the placeholder folder + video. Returns True if something was created."""
        folder = self.root / title.folder_name
        if (folder / MARKER).exists():
            return False
        if self.video is None or not self.video.is_file():
            logger.warning(f"[Upcoming] No placeholder video available, skipping '{title.title}'")
            return False
        if self.dry_run:
            logger.info(f"[DRY RUN] Would create placeholder {folder}")
            return True

        suffix = self.video.suffix or ".mp4"
        if title.is_series:
            target_dir = folder / "Season 01"
            target = target_dir / f"{_ILLEGAL.sub('', title.title).strip()} - S01E01{suffix}"
        else:
            target_dir = folder
            target = folder / f"{folder.name}{suffix}"
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.video, target)
        (folder / MARKER).write_text("created by jellyfin-collection\n", encoding="utf-8")
        logger.info(f"[Upcoming] Created placeholder: {folder.name}")
        return True

    def cleanup(self, wanted: set[str]) -> list[str]:
        """Delete placeholder folders not in `wanted`. Only marker folders are touched."""
        removed: list[str] = []
        for name, path in self.existing().items():
            if name in wanted:
                continue
            if self.dry_run:
                logger.info(f"[DRY RUN] Would remove placeholder {path}")
            else:
                shutil.rmtree(path)
                logger.info(f"[Upcoming] Removed placeholder: {name}")
            removed.append(name)
        return removed


def ensure_placeholder_video(configured: Optional[str], data_dir: Path) -> Optional[Path]:
    """The configured placeholder video, or a generated black one (needs ffmpeg)."""
    if configured:
        path = Path(configured)
        if path.is_file():
            return path
        logger.warning(f"[Upcoming] placeholder_video not found: {configured}")

    target = data_dir / "placeholder.mp4"
    if target.is_file():
        return target

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.warning("[Upcoming] ffmpeg not found and no placeholder_video configured")
        return None

    data_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=5:d=3",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(target),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    except (subprocess.SubprocessError, OSError) as e:
        logger.error(f"[Upcoming] Could not generate placeholder video: {e}")
        return None
    return target


UPCOMING_TAG_PREFIXES = (COMING_SOON.lower(), MOVIE_DATE_TAG.lower(), SHOW_DATE_TAG.lower())


def _is_upcoming_tag(tag: str) -> bool:
    return tag.split(":", 1)[0].lower() in UPCOMING_TAG_PREFIXES


class UpcomingService:
    """Placeholders + tags for upcoming movies (Radarr) and shows (Sonarr)."""

    def __init__(
        self,
        jellyfin: Any,
        radarr: Any,
        sonarr: Any,
        settings: UpcomingSettings,
        data_dir: Path,
        dry_run: bool = False,
    ):
        self.jellyfin = jellyfin
        self.radarr = radarr
        self.sonarr = sonarr
        self.settings = settings
        self.data_dir = data_dir
        self.dry_run = dry_run

    async def run(self, library_ids: dict[str, str], today: Optional[date] = None) -> dict[str, int]:
        today = today or date.today()
        stats = {"titles": 0, "created": 0, "removed": 0, "tagged": 0}
        video = ensure_placeholder_video(self.settings.placeholder_video, self.data_dir)

        jobs: list[tuple[UpcomingSection, list[UpcomingTitle], bool]] = []
        if self.radarr and self.settings.movies.library:
            movies = await self.radarr.get_movies()
            labels = {t["id"]: t["label"] for t in await self.radarr.get_tags()}
            jobs.append((self.settings.movies, select_upcoming_movies(movies, labels, today, self.settings.movies), False))
        if self.sonarr and self.settings.tv.library:
            series = await self.sonarr.get_series()
            labels = {t["id"]: t["label"] for t in await self.sonarr.get_tags()}
            jobs.append((self.settings.tv, select_upcoming_shows(series, labels, today, self.settings.tv), True))

        refresh_needed = False
        for section, titles, is_series in jobs:
            stats["titles"] += len(titles)
            if section.placeholder_root:
                store = PlaceholderStore(Path(section.placeholder_root), video, self.dry_run)
                for title in titles:
                    if store.create(title):
                        stats["created"] += 1
                        refresh_needed = True
                if self.settings.cleanup:
                    removed = store.cleanup({t.folder_name for t in titles})
                    stats["removed"] += len(removed)
                    refresh_needed = refresh_needed or bool(removed)

            library_id = library_ids.get(section.library or "")
            if library_id:
                stats["tagged"] += await self._apply_tags(library_id, titles, is_series)
            else:
                logger.warning(f"[Upcoming] Library '{section.library}' not found in Jellyfin")

        if refresh_needed and not self.dry_run:
            await self.jellyfin.refresh_library()

        logger.info(f"[Upcoming] {stats}")
        return stats

    async def _apply_tags(self, library_id: str, titles: list[UpcomingTitle], is_series: bool) -> int:
        items = await self.jellyfin.get_library_items(
            library_id=library_id, media_type=None, limit=50000, detailed=True
        )
        by_key: dict[tuple[str, int], UpcomingTitle] = {}
        for title in titles:
            if is_series and title.tvdb_id:
                by_key[("tvdb", title.tvdb_id)] = title
            if title.tmdb_id:
                by_key[("tmdb", title.tmdb_id)] = title

        semaphore = asyncio.Semaphore(4)
        changed = 0

        async def update(item: Any) -> bool:
            match = None
            if item.tvdb_id and ("tvdb", item.tvdb_id) in by_key:
                match = by_key[("tvdb", item.tvdb_id)]
            elif item.tmdb_id and ("tmdb", item.tmdb_id) in by_key:
                match = by_key[("tmdb", item.tmdb_id)]

            desired = match.tags if match else []
            current = [t for t in item.tags if _is_upcoming_tag(t)]
            add = [t for t in desired if t not in current]
            remove = [t for t in current if t not in desired]
            if not add and not remove:
                return False
            if self.dry_run:
                logger.info(f"[DRY RUN] {item.title}: +{add} -{remove}")
                return True
            async with semaphore:
                return bool(await self.jellyfin.update_item_tags(item.jellyfin_id, add=add, remove=remove))

        results = await asyncio.gather(*(update(i) for i in items))
        changed = sum(1 for r in results if r)
        return changed
