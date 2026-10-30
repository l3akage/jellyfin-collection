"""MDBList client (public list JSON)."""

import re
from typing import Any, Optional

from loguru import logger

from jfc.clients.base import BaseClient
from jfc.models.media import MediaItem, MediaType, Movie, Series


class MDBListClient(BaseClient):
    """Fetches the items of an MDBList list."""

    BASE_URL = "https://mdblist.com"

    def __init__(self, api_key: Optional[str] = None):
        super().__init__(base_url=self.BASE_URL, headers={"Accept": "application/json"})
        self.api_key = api_key

    @staticmethod
    def _json_path(url_or_path: str) -> Optional[str]:
        """List URL (`https://mdblist.com/lists/user/slug`, share links too) to its /json path."""
        match = re.search(r"mdblist\.com(/lists/[^?#]+)", url_or_path)
        path = match.group(1) if match else (url_or_path if url_or_path.startswith("/lists/") else None)
        if not path:
            return None
        path = path.rstrip("/")
        return path if path.endswith("/json") else f"{path}/json"

    async def get_list(self, url: str, media_type: Optional[MediaType] = None) -> list[MediaItem]:
        path = self._json_path(url)
        if not path:
            logger.warning(f"[MDBList] Unsupported list URL: {url}")
            return []

        params = {"apikey": self.api_key} if self.api_key else None
        response = await self.get(path, params=params)
        if response.status_code in (404, 403):
            logger.warning(f"[MDBList] List not accessible ({response.status_code}): {url}")
            return []
        response.raise_for_status()

        data: Any = response.json()
        entries = data if isinstance(data, list) else data.get("items") or data.get("movies", []) + data.get("shows", [])

        items: list[MediaItem] = []
        for entry in entries:
            kind = str(entry.get("mediatype") or entry.get("type") or "").lower()
            is_show = kind in ("show", "tv", "series")
            if media_type == MediaType.MOVIE and is_show:
                continue
            if media_type == MediaType.SERIES and not is_show:
                continue

            fields = dict(
                title=entry.get("title") or "Unknown",
                year=entry.get("release_year") or entry.get("year"),
                # MDBList list JSON puts the TMDb id in `id` and leaves `tmdb_id` empty
                tmdb_id=_to_int(entry.get("tmdb_id") or entry.get("tmdbid") or entry.get("id")),
                imdb_id=entry.get("imdb_id") or entry.get("imdbid"),
            )
            if is_show:
                items.append(Series(tvdb_id=_to_int(entry.get("tvdb_id") or entry.get("tvdbid")), **fields))
            else:
                items.append(Movie(**fields))

        logger.info(f"[MDBList] {url}: fetched {len(items)} items")
        return items


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
