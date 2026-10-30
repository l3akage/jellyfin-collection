"""Evaluates Kometa-style `plex_all` / `plex_search` / `filters:` against Jellyfin library items.

Supported attributes (with `.not`, `.is`, `.isnot`, `.gt`, `.gte`, `.lt`, `.lte`, `.regex`,
`.begins`, `.ends`, `.before`, `.after` modifiers where they make sense):

    title, genre, year, release, added, label, edition, resolution, has_dolby_vision, hdr,
    audio_language, subtitle_language, studio, network, audience_rating, critic_rating,
    runtime, content_rating, filepath, tmdb_status

Boolean groups: `all:` (default), `any:` and `not:`, each a dict or a list of dicts.
"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from loguru import logger

from jfc.clients.jellyfin import normalize_language
from jfc.models.media import LibraryItem

_GROUP_KEYS = {"all", "any", "not", "and", "or"}
_SORT_KEYS = {"limit", "sort_by"}
_NUMERIC_MODIFIERS = {"gt", "gte", "lt", "lte"}

# TMDb status strings by the short names used in Kometa's `tmdb_status`
_TMDB_STATUS = {
    "returning": "returning series",
    "planned": "planned",
    "production": "in production",
    "ended": "ended",
    "canceled": "canceled",
    "pilot": "pilot",
}


def _as_values(value: Any) -> list[Any]:
    """Scalars, lists and Kometa's comma separated strings to a value list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str) and "," in value:
        return [v.strip() for v in value.split(",") if v.strip()]
    return [value]


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    return None


class LibraryQuery:
    """Filter library items with Kometa-style conditions."""

    def __init__(self, tmdb_status_lookup: Optional[Callable[[LibraryItem], Optional[str]]] = None):
        # tmdb_id -> TMDb status (filled by `prepare`)
        self._tmdb_status: dict[int, str] = {}
        self._tmdb_status_lookup = tmdb_status_lookup

    # ------------------------------------------------------------------
    # Preparation (async data that filters may need)
    # ------------------------------------------------------------------

    @staticmethod
    def uses_attribute(conditions: Any, attribute: str) -> bool:
        """Whether any (nested) condition key references `attribute`."""
        if isinstance(conditions, dict):
            for key, value in conditions.items():
                if str(key).split(".")[0] == attribute:
                    return True
                if LibraryQuery.uses_attribute(value, attribute):
                    return True
        elif isinstance(conditions, list):
            return any(LibraryQuery.uses_attribute(v, attribute) for v in conditions)
        return False

    def set_tmdb_statuses(self, statuses: dict[int, str]) -> None:
        self._tmdb_status.update({k: str(v).lower() for k, v in statuses.items() if v})

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def filter(
        self,
        items: Iterable[LibraryItem],
        conditions: dict[str, Any],
        *,
        now: Optional[datetime] = None,
    ) -> list[LibraryItem]:
        """Items matching `conditions` (a plex_search body or the `filters:` dict)."""
        reference = now or datetime.now(timezone.utc)
        return [item for item in items if self.matches(item, conditions, now=reference)]

    def matches(self, item: LibraryItem, conditions: Any, *, now: Optional[datetime] = None) -> bool:
        reference = now or datetime.now(timezone.utc)
        return self._eval_group(item, conditions, reference, mode="all")

    def _eval_group(self, item: LibraryItem, conditions: Any, now: datetime, mode: str) -> bool:
        if conditions is None:
            return True
        if isinstance(conditions, list):
            results = [self._eval_group(item, c, now, "all") for c in conditions]
            return any(results) if mode == "any" else all(results)
        if not isinstance(conditions, dict):
            return True

        leaf_results: list[bool] = []
        group_results: list[bool] = []

        for key, value in conditions.items():
            key_str = str(key)
            if key_str in _SORT_KEYS:
                continue
            if key_str in ("all", "and"):
                group_results.append(self._eval_group(item, value, now, "all"))
            elif key_str in ("any", "or"):
                group_results.append(self._eval_group(item, value, now, "any"))
            elif key_str == "not":
                group_results.append(not self._eval_group(item, value, now, "any"))
            else:
                leaf_results.append(self._eval_leaf(item, key_str, value, now))

        results = leaf_results + group_results
        if not results:
            return True
        return any(results) if mode == "any" else all(results)

    # ------------------------------------------------------------------

    def _eval_leaf(self, item: LibraryItem, key: str, expected: Any, now: datetime) -> bool:
        parts = key.split(".")
        attribute = parts[0]
        modifier = parts[1] if len(parts) > 1 else None

        # Kometa also writes `item_label.remove` style keys; those are not filters.
        if attribute in ("item_label",):
            return True

        negate = modifier in ("not", "isnot")
        if modifier in ("not", "isnot", "is"):
            modifier = None

        if attribute == "added":
            return self._eval_added(item, modifier, expected, now, negate)

        actual = self._attribute_values(item, attribute)
        if actual is None:
            logger.debug(f"Unsupported library filter attribute '{attribute}' (ignored)")
            return True

        expected_values = _as_values(expected)

        if attribute in ("has_dolby_vision", "hdr"):
            wanted = str(expected).strip().lower() in ("true", "yes", "1")
            result = bool(actual[0]) == wanted
            return not result if negate else result

        if modifier in _NUMERIC_MODIFIERS:
            result = self._compare_numeric(actual, modifier, expected_values)
        elif modifier == "regex":
            result = any(re.search(str(p), str(a), re.IGNORECASE) for p in expected_values for a in actual)
        elif modifier == "begins":
            result = any(str(a).lower().startswith(str(p).lower()) for p in expected_values for a in actual)
        elif modifier == "ends":
            result = any(str(a).lower().endswith(str(p).lower()) for p in expected_values for a in actual)
        elif modifier in ("before", "after"):
            result = self._compare_date(actual, modifier, expected_values)
        else:
            result = self._equals_any(attribute, actual, expected_values)

        return not result if negate else result

    @staticmethod
    def _normalize(attribute: str, value: Any) -> str:
        text = str(value).strip().lower()
        if attribute in ("audio_language", "subtitle_language"):
            return normalize_language(text) or text
        if attribute == "resolution":
            text = text.replace("p", "").replace("uhd", "4k")
            return "4k" if text in ("2160", "4k") else text
        return text

    def _equals_any(self, attribute: str, actual: list[Any], expected: list[Any]) -> bool:
        actual_norm = {self._normalize(attribute, a) for a in actual if a is not None}
        return any(self._normalize(attribute, e) in actual_norm for e in expected)

    @staticmethod
    def _compare_numeric(actual: list[Any], modifier: str, expected: list[Any]) -> bool:
        targets = [_to_float(e) for e in expected]
        targets = [t for t in targets if t is not None]
        values = [v for v in (_to_float(a) for a in actual) if v is not None]
        if not targets or not values:
            return False
        target = targets[0]
        ops = {
            "gt": lambda v: v > target,
            "gte": lambda v: v >= target,
            "lt": lambda v: v < target,
            "lte": lambda v: v <= target,
        }
        return any(ops[modifier](v) for v in values)

    @staticmethod
    def _compare_date(actual: list[Any], modifier: str, expected: list[Any]) -> bool:
        target = _to_date(expected[0]) if expected else None
        values = [d for d in (_to_date(a) for a in actual) if d is not None]
        if target is None or not values:
            return False
        if modifier == "before":
            return any(v < target for v in values)
        return any(v > target for v in values)

    def _eval_added(
        self, item: LibraryItem, modifier: Optional[str], expected: Any, now: datetime, negate: bool
    ) -> bool:
        """`added: 7` = added within the last 7 days; `.before`/`.after` take dates."""
        if item.date_added is None:
            return negate
        added = item.date_added if item.date_added.tzinfo else item.date_added.replace(tzinfo=timezone.utc)
        value = _as_values(expected)[0] if _as_values(expected) else None

        if modifier in ("before", "after"):
            target = _to_date(value)
            if target is None:
                return True
            result = added.date() < target if modifier == "before" else added.date() > target
        else:
            days = _to_float(value)
            if days is None:
                return True
            result = added >= now - timedelta(days=days)
        return not result if negate else result

    def _attribute_values(self, item: LibraryItem, attribute: str) -> Optional[list[Any]]:
        """Values of `attribute` for the item, or None if the attribute is unknown."""
        if attribute == "title":
            return [item.title]
        if attribute in ("genre", "genres"):
            return list(item.genres)
        if attribute == "year":
            return [item.year] if item.year else []
        if attribute == "release":
            return [item.premiere_date] if item.premiere_date else []
        if attribute == "label":
            return list(item.tags)
        if attribute == "edition":
            return [item.edition] if item.edition else []
        if attribute == "resolution":
            return [item.resolution] if item.resolution else []
        if attribute == "has_dolby_vision":
            return [item.dolby_vision]
        if attribute == "hdr":
            return [item.hdr]
        if attribute == "audio_language":
            return list(item.audio_languages)
        if attribute == "subtitle_language":
            return list(item.subtitle_languages)
        if attribute in ("studio", "network"):
            return list(item.studios)
        if attribute in ("audience_rating", "community_rating"):
            return [item.community_rating] if item.community_rating is not None else []
        if attribute == "critic_rating":
            return [item.critic_rating] if item.critic_rating is not None else []
        if attribute == "runtime":
            return [item.runtime_minutes] if item.runtime_minutes is not None else []
        if attribute == "content_rating":
            return [item.official_rating] if item.official_rating else []
        if attribute in ("filepath", "folder"):
            return [item.path] if item.path else []
        if attribute == "tmdb_status":
            status = self._tmdb_status.get(item.tmdb_id) if item.tmdb_id else None
            return [self._status_alias(status)] if status else []
        return None

    @staticmethod
    def _status_alias(status: str) -> str:
        """TMDb 'Returning Series' -> 'returning' (also accepts the short names directly)."""
        lowered = status.lower()
        for short, long_name in _TMDB_STATUS.items():
            if lowered in (short, long_name):
                return short
        return lowered

    # ------------------------------------------------------------------
    # Sorting
    # ------------------------------------------------------------------

    @staticmethod
    def sort(items: list[LibraryItem], sort_by: Optional[str]) -> list[LibraryItem]:
        """Sort by a Kometa `sort_by` string such as `title.asc` or `audience_rating.desc`."""
        if not sort_by:
            return items
        field, _, direction = str(sort_by).partition(".")
        reverse = direction.lower() == "desc"

        def key(item: LibraryItem) -> Any:
            if field == "title":
                return item.title.lower()
            if field == "year" or field == "release":
                return item.premiere_date or date.min
            if field == "added":
                return item.date_added.replace(tzinfo=None) if item.date_added else datetime.min
            if field in ("audience_rating", "community_rating"):
                return item.community_rating or 0.0
            if field == "critic_rating":
                return item.critic_rating or 0.0
            if field == "runtime":
                return item.runtime_minutes or 0
            return item.title.lower()

        return sorted(items, key=key, reverse=reverse)
