"""IMDb client for charts and custom lists."""

import json
import re
from typing import Optional

from loguru import logger

from jfc.clients.base import BaseClient


class IMDbClient(BaseClient):
    """Client for fetching IMDb chart/list title IDs."""

    BASE_URL = "https://www.imdb.com"

    CHART_PATHS = {
        "top": "/chart/top/",
        "boxoffice": "/chart/boxoffice/",
        "moviemeter": "/chart/moviemeter/",
        "tvmeter": "/chart/tvmeter/",
        "toptv": "/chart/toptv/",
        "bottom": "/chart/bottom/",
    }

    def __init__(self):
        super().__init__(
            base_url=self.BASE_URL,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:145.0) "
                    "Gecko/20100101 Firefox/145.0"
                ),
            },
        )

    async def get_chart(self, chart_id: str, limit: int = 250) -> list[str]:
        """Get IMDb title IDs from a chart endpoint."""
        chart_key = chart_id.strip().lower()
        path = self.CHART_PATHS.get(chart_key)
        if not path:
            logger.warning(f"Unknown IMDb chart '{chart_id}'")
            return []

        response = await self.get(path)
        if response.status_code == 404:
            logger.warning(f"IMDb chart not found: {chart_id}")
            return []

        response.raise_for_status()
        imdb_ids = self._extract_imdb_ids(response.text, limit=limit)
        if response.status_code == 202 and not imdb_ids:
            logger.warning(
                f"[IMDb] Chart {chart_key}: received HTTP 202 without title ids "
                "(likely challenge response)"
            )
        logger.info(f"[IMDb] Chart {chart_key}: fetched {len(imdb_ids)} ids")
        return imdb_ids

    async def get_list(self, list_id: str, limit: int = 250) -> list[str]:
        """Get IMDb title IDs from a custom list (ls...)."""
        normalized = self._extract_list_id(list_id)
        if not normalized:
            logger.warning(f"Invalid IMDb list id '{list_id}'")
            return []

        response = await self.get(f"/list/{normalized}/")
        if response.status_code == 404:
            logger.warning(f"IMDb list not found: {normalized}")
            return []

        response.raise_for_status()
        imdb_ids = self._extract_imdb_ids(response.text, limit=limit)
        if response.status_code == 202 and not imdb_ids:
            logger.warning(
                f"[IMDb] List {normalized}: received HTTP 202 without title ids "
                "(likely challenge response)"
            )
        logger.info(f"[IMDb] List {normalized}: fetched {len(imdb_ids)} ids")
        return imdb_ids

    def _extract_list_id(self, value: str) -> Optional[str]:
        """Extract ls* list id from raw string or IMDb URL."""
        raw = value.strip()
        if re.fullmatch(r"ls\d+", raw):
            return raw

        match = re.search(r"/list/(ls\d+)", raw)
        if match:
            return match.group(1)

        return None

    def _extract_imdb_ids(self, html: str, limit: int = 250) -> list[str]:
        """Extract unique tt* IDs from IMDb HTML in first-seen order."""
        next_data_ids = self._extract_imdb_ids_from_next_data(html, limit=limit)
        if next_data_ids:
            return next_data_ids

        seen: set[str] = set()
        ids: list[str] = []

        for match in re.finditer(r"/title/(tt\d{7,9})", html):
            imdb_id = match.group(1)
            if imdb_id in seen:
                continue
            seen.add(imdb_id)
            ids.append(imdb_id)
            if len(ids) >= limit:
                break

        return ids

    def _extract_imdb_ids_from_next_data(self, html: str, limit: int = 250) -> list[str]:
        """Extract IDs from IMDb __NEXT_DATA__ payload when present."""
        match = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not match:
            return []

        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []

        seen: set[str] = set()
        ids: list[str] = []

        def walk(value: object) -> None:
            if len(ids) >= limit:
                return

            if isinstance(value, str):
                if re.fullmatch(r"tt\d{7,9}", value) and value not in seen:
                    seen.add(value)
                    ids.append(value)
                return

            if isinstance(value, list):
                for item in value:
                    walk(item)
                    if len(ids) >= limit:
                        break
                return

            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
                    if len(ids) >= limit:
                        break

        walk(payload)
        return ids
