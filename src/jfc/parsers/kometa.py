"""Parser for Kometa (Plex Meta Manager) YAML configuration files."""

import copy
import re
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml
from loguru import logger

from jfc.models.media import infer_media_type
from jfc.parsers.defaults import build_default_collections
from jfc.parsers.seasonal import dynamic_range
from jfc.models.collection import (
    CollectionConfig,
    CollectionFilter,
    CollectionOrder,
    CollectionSchedule,
    CollectionTemplate,
    SyncMode,
)


_DROP = object()


class _OptionalMissing(Exception):
    """Raised internally when an optional template variable is not provided."""


class KometaParser:
    """Parser for Kometa YAML configuration files."""

    def __init__(self, config_path: Path):
        """
        Initialize parser.

        Args:
            config_path: Path to config directory or main config file
        """
        self.config_path = config_path
        self._templates: dict[str, CollectionTemplate] = {}
        self._raw_templates: dict[str, dict[str, Any]] = {}
        self._global_variables: dict[str, Any] = {}

    def parse_config(self, file_path: Optional[Path] = None) -> dict[str, Any]:
        """
        Parse main config.yml file.

        Args:
            file_path: Path to config file (uses config_path/config.yml if None)

        Returns:
            Parsed configuration dictionary
        """
        path = file_path or self.config_path / "config.yml"

        if not path.exists():
            logger.warning(f"Config file not found: {path}")
            return {}

        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        logger.info(f"Parsed config from {path}")
        return config or {}

    def parse_library_config(self, library_config: dict[str, Any]) -> dict[str, Any]:
        """
        Parse library configuration from main config.

        Args:
            library_config: Library section from config.yml

        Returns:
            Parsed library configuration
        """
        result = {
            "collection_files": [],
            "collection_entries": [],
            "operations": library_config.get("operations", {}),
            "radarr": library_config.get("radarr"),
            "sonarr": library_config.get("sonarr"),
        }

        # Parse collection files (plain paths, `file:` entries and Kometa `default:` entries)
        for cf in library_config.get("collection_files") or []:
            if isinstance(cf, dict) and "file" in cf:
                result["collection_files"].append(cf["file"])
                result["collection_entries"].append(
                    {
                        "file": cf["file"],
                        "template_variables": cf.get("template_variables") or {},
                    }
                )
            elif isinstance(cf, dict) and "default" in cf:
                result["collection_entries"].append(
                    {
                        "default": cf["default"],
                        "template_variables": cf.get("template_variables") or {},
                    }
                )
            elif isinstance(cf, str):
                result["collection_files"].append(cf)
                result["collection_entries"].append({"file": cf, "template_variables": {}})

        return result

    def parse_collection_file(
        self,
        file_path: Path,
        template_variables: Optional[dict[str, Any]] = None,
    ) -> list[CollectionConfig]:
        """
        Parse a collection YAML file (e.g., Films.yml, Series.yml).

        Args:
            file_path: Path to collection file
            template_variables: Variables from the `template_variables:` of the config entry
                (usable as `<<name>>` in the file)

        Returns:
            List of parsed collection configurations
        """
        if not file_path.exists():
            logger.warning(f"Collection file not found: {file_path}")
            return []

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            logger.error(f"Invalid YAML in {file_path}: {e}")
            return []

        if not data:
            return []

        return self.parse_collection_data(data, template_variables, source=str(file_path))

    def parse_collection_data(
        self,
        data: dict[str, Any],
        template_variables: Optional[dict[str, Any]] = None,
        source: str = "<data>",
    ) -> list[CollectionConfig]:
        """Parse an already loaded collection document (templates + collections)."""
        # Parse templates first
        self._parse_templates(data.get("templates") or {})
        self._global_variables = dict(template_variables or {})

        collections = []
        for name, config in (data.get("collections") or {}).items():
            try:
                name = self._substitute_name(str(name))
                collections.append(self._parse_collection(name, config or {}))
            except Exception as e:
                logger.error(f"Failed to parse collection '{name}': {e}")

        for dyn_name, dyn_config in (data.get("dynamic_collections") or {}).items():
            collections.extend(self._parse_dynamic_collection(str(dyn_name), dyn_config or {}))

        logger.info(f"Parsed {len(collections)} collections from {source}")
        return collections

    def _substitute_name(self, name: str) -> str:
        """Collection names may use `<<var>>` from the entry's template_variables."""
        if "<<" not in name:
            return name
        try:
            return str(self._substitute(name, self._global_variables, set()))
        except KeyError:
            return name

    def _parse_templates(self, templates: dict[str, Any]) -> None:
        """Store template definitions (raw bodies; resolved per collection)."""
        for name, config in templates.items():
            config = config or {}
            self._raw_templates[str(name)] = config
            self._templates[str(name)] = CollectionTemplate(
                name=str(name),
                sync_mode=SyncMode(config.get("sync_mode", "sync")),
                visible_library=config.get("visible_library", True),
                visible_home=config.get("visible_home", False),
                visible_shared=config.get("visible_shared", False),
                filters=self._parse_filters(config.get("filters") or {}),
                schedule=CollectionSchedule.from_kometa(config.get("schedule")),
            )

    # ------------------------------------------------------------------
    # Template resolution
    # ------------------------------------------------------------------

    _VAR_PATTERN = re.compile(r"<<([^<>]+)>>")
    _TEMPLATE_META_KEYS = {"optional", "default", "conditionals", "move_prefix", "move_collection_prefix"}

    def _template_specs(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalize the `template:` key (dict, list of dicts, or name string) to a list of dicts."""
        raw = config.get("template")
        if raw is None:
            return []
        if isinstance(raw, str):
            return [{"name": raw}]
        if isinstance(raw, dict):
            return [raw]
        if isinstance(raw, list):
            specs: list[dict[str, Any]] = []
            for entry in raw:
                if isinstance(entry, dict):
                    specs.append(entry)
                elif isinstance(entry, str):
                    specs.append({"name": entry})
            return specs
        return []

    def _substitute(self, value: Any, variables: dict[str, Any], optional: set[str]) -> Any:
        """Replace `<<var>>` placeholders. Returns `_DROP` when an optional variable is missing."""
        if isinstance(value, str):
            matches = list(self._VAR_PATTERN.finditer(value))
            if not matches:
                return value

            # Whole-value placeholder keeps native types (lists, ints, dicts)
            if len(matches) == 1 and matches[0].span() == (0, len(value)):
                key = matches[0].group(1).strip()
                if key in variables and variables[key] is not None:
                    return copy.deepcopy(variables[key])
                if key in optional:
                    return _DROP
                raise KeyError(f"Template variable '{key}' is not defined")

            def repl(match: re.Match) -> str:
                key = match.group(1).strip()
                if key in variables and variables[key] is not None:
                    return str(variables[key])
                if key in optional:
                    raise _OptionalMissing(key)
                raise KeyError(f"Template variable '{key}' is not defined")

            try:
                return self._VAR_PATTERN.sub(repl, value)
            except _OptionalMissing:
                return _DROP

        if isinstance(value, dict):
            result: dict[Any, Any] = {}
            for key, val in value.items():
                new_val = self._substitute(val, variables, optional)
                if new_val is _DROP:
                    continue
                result[key] = new_val
            return result

        if isinstance(value, list):
            items = []
            for val in value:
                new_val = self._substitute(val, variables, optional)
                if new_val is _DROP:
                    continue
                items.append(new_val)
            return items

        return value

    def _resolve_collection_dict(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        """Merge referenced templates into the collection config and resolve `<<variables>>`."""
        specs = self._template_specs(config)
        merged: dict[str, Any] = {}

        dynamic = config.get("dynamic")
        dynamic_dates = dynamic_range(dynamic, date.today().year) if dynamic else None

        for spec in specs:
            template_name = spec.get("name")
            body = self._raw_templates.get(str(template_name)) if template_name else None
            if body is None:
                logger.warning(f"Collection '{name}': template '{template_name}' not found")
                continue

            variables: dict[str, Any] = {"collection_name": name, "mapping_name": name}
            variables.update(self._global_variables)
            variables.update((body.get("default") or {}))
            variables.update({k: v for k, v in spec.items() if k != "name"})
            if dynamic_dates:
                variables["start_date"], variables["end_date"] = dynamic_dates

            optional = set(self._normalize_string_list(body.get("optional")))
            optional.update(str(k) for k in (body.get("default") or {}))
            body_items = {k: v for k, v in body.items() if k not in self._TEMPLATE_META_KEYS}

            try:
                resolved = self._substitute(body_items, variables, optional)
            except KeyError as e:
                logger.warning(f"Collection '{name}': {e}; template '{template_name}' skipped")
                continue

            for key, val in resolved.items():
                if key == "filters" and isinstance(val, dict) and isinstance(merged.get("filters"), dict):
                    merged["filters"] = {**merged["filters"], **val}
                else:
                    merged[key] = val

        own = {k: v for k, v in config.items() if k not in ("template", "dynamic")}
        # Resolve global template_variables (e.g. <<collection_name>>) in the collection itself
        own_vars = {"collection_name": name, "mapping_name": name, **self._global_variables}
        try:
            own = self._substitute(own, own_vars, set())
        except KeyError:
            pass

        for key, val in own.items():
            if key == "filters" and isinstance(val, dict) and isinstance(merged.get("filters"), dict):
                merged["filters"] = {**merged["filters"], **val}
            else:
                merged[key] = val

        return merged

    def _parse_dynamic_collection(self, mapping_name: str, config: dict[str, Any]) -> list[CollectionConfig]:
        """
        Dynamic collections. Currently `type: tmdb_collection` over `data`/`include`
        is not resolvable offline, so only explicit `include` lists are expanded.
        """
        include = config.get("include")
        if not include:
            logger.warning(
                f"dynamic_collections '{mapping_name}' needs a runtime provider and is skipped "
                "(only entries with an explicit include list are expanded)"
            )
            return []

        results: list[CollectionConfig] = []
        template = config.get("template")
        dyn_type = config.get("type")
        for key in include if isinstance(include, list) else [include]:
            collection_name = str(key)
            raw: dict[str, Any] = {"template": copy.deepcopy(template)} if template else {}
            if isinstance(raw.get("template"), dict):
                raw["template"].setdefault("name", None)
                raw["template"]["key"] = key
                raw["template"]["value"] = key
            if dyn_type == "tmdb_collection":
                raw["tmdb_collection"] = [key]
            try:
                results.append(self._parse_collection(collection_name, raw))
            except Exception as e:
                logger.error(f"Failed to parse dynamic collection '{collection_name}': {e}")
        return results

    # ------------------------------------------------------------------
    # Collection parsing
    # ------------------------------------------------------------------

    def _parse_collection(self, name: str, config: dict[str, Any]) -> CollectionConfig:
        """Parse a single collection configuration (templates are merged first)."""
        template_specs = self._template_specs(config)
        template_name = template_specs[0].get("name") if template_specs else None
        template = self._templates.get(template_name) if template_name else None

        config = self._resolve_collection_dict(name, config)

        # Filters (CollectionFilter for provider results) + raw dict for library queries
        raw_filters = config.get("filters") or {}
        filters = self._parse_filters(raw_filters)
        if config.get("schedule"):
            schedule = CollectionSchedule.from_kometa(str(config["schedule"]))
        elif template:
            schedule = template.schedule
        else:
            schedule = CollectionSchedule()

        # Parse tmdb_discover if present
        tmdb_discover = None
        if "tmdb_discover" in config:
            tmdb_discover = self._normalize_tmdb_discover(config["tmdb_discover"])

        # Parse trakt_chart if present
        trakt_chart = None
        if "trakt_chart" in config:
            trakt_chart = config["trakt_chart"]

        # Parse IMDb builders
        imdb_chart = self._normalize_imdb_builder(config.get("imdb_chart"))
        imdb_list = self._normalize_imdb_builder(config.get("imdb_list"))
        radarr_taglist = self._normalize_tag_builder(config.get("radarr_taglist"))
        sonarr_taglist = self._normalize_tag_builder(config.get("sonarr_taglist"))

        plex_search = config.get("plex_search")
        collection_order = self._parse_collection_order(config.get("collection_order"))

        trakt_lists = self._normalize_string_list(config.get("trakt_list"))
        mdblist_lists = self._normalize_string_list(config.get("mdblist_list"))

        sync_value = str(config.get("sync_mode", template.sync_mode.value if template else "sync")).lower()

        return CollectionConfig(
            name=name,
            summary=config.get("summary"),
            sort_title=config.get("sort_title"),
            poster=config.get("poster"),
            url_poster=config.get("url_poster"),
            collection_order=collection_order,
            visible_library=config.get("visible_library", template.visible_library if template else True),
            visible_home=config.get("visible_home", template.visible_home if template else False),
            visible_shared=config.get("visible_shared", template.visible_shared if template else False),
            sync_mode=SyncMode(sync_value if sync_value in ("sync", "append") else "sync"),
            minimum_items=int(config.get("minimum_items", 1) or 1),
            delete_below_minimum=self._as_bool(config.get("delete_below_minimum", False)),
            delete_not_scheduled=self._as_bool(config.get("delete_not_scheduled", False)),
            build_collection=self._as_bool(config.get("build_collection", True)),
            schedule=schedule,
            filters=filters,
            # Builder sources
            tmdb_trending_weekly=config.get("tmdb_trending_weekly"),
            tmdb_trending_daily=config.get("tmdb_trending_daily"),
            tmdb_popular=config.get("tmdb_popular"),
            tmdb_now_playing=config.get("tmdb_now_playing"),
            tmdb_airing_today=config.get("tmdb_airing_today"),
            tmdb_on_the_air=config.get("tmdb_on_the_air"),
            tmdb_discover=tmdb_discover,
            tmdb_list=config.get("tmdb_list"),
            tmdb_movie=self._int_list(config.get("tmdb_movie")),
            tmdb_show=self._int_list(config.get("tmdb_show")),
            tmdb_collection=self._int_list(config.get("tmdb_collection", config.get("tmdb_collection_details"))),
            tmdb_keyword=self._int_list(config.get("tmdb_keyword")),
            tmdb_director=self._int_list(config.get("tmdb_director", config.get("tmdb_director_details"))),
            tvdb_show=self._int_list(config.get("tvdb_show")),
            tvdb_movie=self._int_list(config.get("tvdb_movie")),
            trakt_trending=config.get("trakt_trending"),
            trakt_popular=config.get("trakt_popular"),
            trakt_chart=trakt_chart,
            trakt_list=trakt_lists[0] if trakt_lists else None,
            trakt_lists=trakt_lists,
            mdblist_list=mdblist_lists[0] if mdblist_lists else None,
            mdblist_lists=mdblist_lists,
            imdb_chart=imdb_chart,
            imdb_list=imdb_list,
            radarr_taglist=radarr_taglist,
            sonarr_taglist=sonarr_taglist,
            plex_search=plex_search,
            plex_all=self._as_bool(config.get("plex_all", False)),
            library_filters=raw_filters if isinstance(raw_filters, dict) else {},
            # Labels
            item_label=self._label_list(config.get("item_label")),
            non_item_remove_label=self._label_list(config.get("non_item_remove_label")),
            item_label_remove=self._label_list(config.get("item_label.remove")),
            radarr_add_missing=self._optional_bool(config.get("radarr_add_missing")),
            sonarr_add_missing=self._optional_bool(config.get("sonarr_add_missing")),
            # Tags
            item_radarr_tag=config.get("item_radarr_tag"),
            item_sonarr_tag=config.get("item_sonarr_tag"),
            # Limit
            limit=tmdb_discover.get("limit") if tmdb_discover else config.get("limit"),
            # Template reference
            template=template_name,
        )

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "1")
        return bool(value)

    @staticmethod
    def _optional_bool(value: Any) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "1")
        return bool(value)

    @staticmethod
    def _int_list(value: Any) -> list[int]:
        """Normalize `1, 2, 3` / [1, 2] / 5 to a list of ints (invalid entries are skipped)."""
        if value is None:
            return []
        if isinstance(value, str):
            values: list[Any] = [v for v in re.split(r"[,\s]+", value) if v]
        elif isinstance(value, list):
            values = value
        else:
            values = [value]
        result: list[int] = []
        for v in values:
            try:
                result.append(int(str(v).strip()))
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _label_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return [str(value)]

    def _parse_filters(
        self,
        filters: dict[str, Any],
        base: Optional[CollectionFilter] = None,
    ) -> CollectionFilter:
        """Parse filter configuration."""
        if base:
            # Start with base filter values
            result = base.model_copy()
        else:
            result = CollectionFilter()

        # Year filters
        if "year.gte" in filters:
            result.year_gte = filters["year.gte"]
        if "year.lte" in filters:
            result.year_lte = filters["year.lte"]

        # Rating filters
        if "vote_average.gte" in filters:
            result.vote_average_gte = filters["vote_average.gte"]
        if "critic_rating.gte" in filters:
            result.critic_rating_gte = filters["critic_rating.gte"]

        # Vote count filters
        if "tmdb_vote_count.gte" in filters:
            result.tmdb_vote_count_gte = filters["tmdb_vote_count.gte"]

        # Country filters
        if "country.not" in filters:
            result.country_not = filters["country.not"]
        if "origin_country.not" in filters:
            result.origin_country_not = filters["origin_country.not"]

        # Language filter (e.g., exclude Japanese: ["ja"])
        if "original_language.not" in filters:
            lang = filters["original_language.not"]
            if isinstance(lang, str):
                result.original_language_not = [lang]
            else:
                result.original_language_not = lang

        # Genre filters (for post-filtering, TMDb discover handles these separately)
        if "without_genres" in filters:
            genres = filters["without_genres"]
            if isinstance(genres, int):
                result.without_genres = [genres]
            elif isinstance(genres, str):
                result.without_genres = [int(g) for g in genres.split(",")]
            else:
                result.without_genres = genres

        if "with_genres" in filters:
            genres = filters["with_genres"]
            if isinstance(genres, int):
                result.with_genres = [genres]
            elif isinstance(genres, str):
                result.with_genres = [int(g) for g in genres.split(",")]
            else:
                result.with_genres = genres

        return result

    def _parse_collection_order(self, value: str | None) -> CollectionOrder:
        """Parse collection_order value to enum."""
        if not value:
            return CollectionOrder.CUSTOM

        value_lower = value.lower().strip()

        # Map common names to enum values
        order_mapping = {
            # Custom (keep source order)
            "custom": CollectionOrder.CUSTOM,
            # Alphabetical
            "alpha": CollectionOrder.SORT_NAME,
            "alphabetical": CollectionOrder.SORT_NAME,
            "sortname": CollectionOrder.SORT_NAME,
            "name": CollectionOrder.SORT_NAME,
            # Release date
            "release": CollectionOrder.PREMIERE_DATE,
            "premieredate": CollectionOrder.PREMIERE_DATE,
            "release_date": CollectionOrder.PREMIERE_DATE,
            "date": CollectionOrder.PREMIERE_DATE,
            # Date added
            "added": CollectionOrder.DATE_CREATED,
            "datecreated": CollectionOrder.DATE_CREATED,
            "date_added": CollectionOrder.DATE_CREATED,
            # Community rating
            "rating": CollectionOrder.COMMUNITY_RATING,
            "communityrating": CollectionOrder.COMMUNITY_RATING,
            "audience_rating": CollectionOrder.COMMUNITY_RATING,
            # Critic rating
            "critic": CollectionOrder.CRITIC_RATING,
            "criticrating": CollectionOrder.CRITIC_RATING,
            "critic_rating": CollectionOrder.CRITIC_RATING,
            # Random
            "random": CollectionOrder.RANDOM,
        }

        if value_lower in order_mapping:
            return order_mapping[value_lower]

        logger.warning(f"Unknown collection_order '{value}', defaulting to 'custom'")
        return CollectionOrder.CUSTOM

    def _normalize_imdb_builder(self, value: Any) -> Optional[dict[str, Any]]:
        """Normalize imdb_chart/imdb_list config into {list_ids, limit?}."""
        if value is None:
            return None

        result: dict[str, Any] = {}

        if isinstance(value, dict):
            list_ids = self._normalize_string_list(value.get("list_ids"))
            if list_ids:
                result["list_ids"] = list_ids
            if value.get("limit") is not None:
                result["limit"] = value.get("limit")

            return result if result.get("list_ids") else None

        list_ids = self._normalize_string_list(value)
        if not list_ids:
            return None

        return {"list_ids": list_ids}

    def _normalize_string_list(self, value: Any) -> list[str]:
        """Normalize scalar/list values to a clean string list."""
        if value is None:
            return []

        values = value if isinstance(value, list) else [value]
        normalized: list[str] = []

        for item in values:
            item_str = str(item).strip()
            if item_str:
                normalized.append(item_str)

        return normalized

    def _normalize_tag_builder(self, value: Any) -> Optional[dict[str, Any]]:
        """Normalize radarr_taglist/sonarr_taglist into {tags, limit?}."""
        if value is None:
            return None

        result: dict[str, Any] = {}

        if isinstance(value, dict):
            tags = self._normalize_string_list(value.get("tags"))
            if tags:
                result["tags"] = tags
            if value.get("limit") is not None:
                result["limit"] = value.get("limit")
            return result if result.get("tags") else None

        tags = self._normalize_string_list(value)
        if not tags:
            return None

        return {"tags": tags}

    def _normalize_tmdb_discover(self, discover: dict[str, Any]) -> dict[str, Any]:
        """Normalize TMDb discover parameters."""
        result = {}

        # Direct mappings
        direct_fields = [
            "sort_by",
            "vote_average.gte",
            "vote_average.lte",
            "vote_count.gte",
            "vote_count.lte",
            "watch_region",
            "with_watch_monetization_types",
            "with_original_language",
            "with_release_type",
            "region",
            "limit",
            "with_status",
        ]

        for field in direct_fields:
            if field in discover:
                result[field] = discover[field]

        # Handle genres (can be comma-separated or list)
        if "with_genres" in discover:
            genres = discover["with_genres"]
            if isinstance(genres, str):
                result["with_genres"] = [int(g) for g in genres.split(",")]
            elif isinstance(genres, int):
                result["with_genres"] = [genres]
            else:
                result["with_genres"] = genres

        if "without_genres" in discover:
            genres = discover["without_genres"]
            if isinstance(genres, str):
                result["without_genres"] = [int(g) for g in genres.split(",")]
            elif isinstance(genres, int):
                result["without_genres"] = [genres]
            else:
                result["without_genres"] = genres

        # Networks / keywords / companies (comma or pipe separated, or lists)
        for key in ("with_networks", "with_keywords", "with_companies"):
            if key in discover:
                result[key] = self._int_list(
                    discover[key].replace("|", ",") if isinstance(discover[key], str) else discover[key]
                )

        # Handle watch providers (pipe-separated in Kometa)
        if "with_watch_providers" in discover:
            providers = discover["with_watch_providers"]
            if isinstance(providers, str):
                result["with_watch_providers"] = [int(p) for p in providers.split("|")]
            elif isinstance(providers, int):
                result["with_watch_providers"] = [providers]
            else:
                result["with_watch_providers"] = providers

        # Handle dates
        date_fields = [
            "primary_release_date.gte",
            "primary_release_date.lte",
            "first_air_date.gte",
            "first_air_date.lte",
        ]

        for field in date_fields:
            if field in discover:
                value = discover[field]
                if isinstance(value, str):
                    try:
                        result[field] = date.fromisoformat(value)
                    except ValueError:
                        logger.warning(f"Invalid date format for {field}: {value}")
                elif isinstance(value, date):
                    result[field] = value

        return result

    def get_all_collections(self) -> dict[str, list[CollectionConfig]]:
        """
        Parse all collections from config.

        Returns:
            Dictionary mapping library names to their collections
        """
        config = self.parse_config()
        result = {}

        for library_name, library_config in config.get("libraries", {}).items():
            parsed = self.parse_library_config(library_config)
            collections = []

            media_type = infer_media_type(library_name)

            for entry in parsed["collection_entries"]:
                variables = entry.get("template_variables") or {}

                if "default" in entry:
                    default_data = build_default_collections(str(entry["default"]), media_type, variables)
                    if default_data is None:
                        logger.warning(
                            f"Library '{library_name}': Kometa default '{entry['default']}' is not supported, skipped"
                        )
                        continue
                    collections.extend(
                        self.parse_collection_data(default_data, source=f"default:{entry['default']}")
                    )
                    continue

                cf_path = entry["file"]
                # Handle relative paths
                if cf_path.startswith("config/"):
                    cf_path = cf_path.replace("config/", "", 1)

                full_path = self.config_path / cf_path
                collections.extend(self.parse_collection_file(full_path, variables))

            # Apply library-level Sonarr/Radarr settings to each collection
            sonarr_config = parsed.get("sonarr") or {}
            radarr_config = parsed.get("radarr") or {}

            for collection in collections:
                # Sonarr overrides
                if sonarr_config.get("root_folder_path"):
                    collection.sonarr_root_folder = sonarr_config["root_folder_path"]
                if sonarr_config.get("tag"):
                    collection.sonarr_tag = sonarr_config["tag"]
                if sonarr_config.get("quality_profile"):
                    collection.sonarr_quality_profile = sonarr_config["quality_profile"]

                # Radarr overrides
                if radarr_config.get("root_folder_path"):
                    collection.radarr_root_folder = radarr_config["root_folder_path"]
                if radarr_config.get("tag"):
                    collection.radarr_tag = radarr_config["tag"]
                if radarr_config.get("quality_profile"):
                    collection.radarr_quality_profile = radarr_config["quality_profile"]

            result[library_name] = collections
            logger.info(f"Library '{library_name}': {len(collections)} collections")

        return result
