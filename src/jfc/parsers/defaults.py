"""Built-in equivalents of Kometa's `- default:` collection files.

Each builder returns a Kometa-style document (`{"collections": {...}}`) that goes through the
normal parser, or None when the default is not supported. Supported: imdb, network, streaming,
universe (the franchises listed in `UNIVERSES`).
"""

from typing import Any, Optional

from loguru import logger

from jfc.models.media import MediaType

# TMDb network IDs
NETWORKS: dict[str, int] = {
    "A&E": 129, "ABC": 2, "AMC": 174, "Animal Planet": 91, "BBC": 4, "BET": 24,
    "Cartoon Network": 56, "Channel 4": 26, "CBS": 16, "Comedy Central": 47, "Discovery": 64,
    "Disney Channel": 54, "E!": 76, "ESPN": 29, "Food Network": 11, "FOX": 19, "HBO": 49,
    "HGTV": 210, "History": 65, "ITV": 9, "Lifetime": 34, "MTV": 33, "National Geographic": 43,
    "NBC": 6, "Nickelodeon": 13, "Paramount Network": 2076, "PBS": 14, "Showtime": 67,
    "Starz": 318, "The CW": 71, "TLC": 84, "TNT": 41, "USA": 30,
}

# Streaming services: key -> (display name, TMDb watch provider IDs)
STREAMING: dict[str, tuple[str, list[int]]] = {
    "amazon": ("Amazon", [9, 119]),
    "appletv": ("Apple TV+", [350]),
    "britbox": ("BritBox", [151]),
    "crave": ("Crave", [230]),
    "crunchyroll": ("Crunchyroll", [283]),
    "discovery": ("discovery+", [520]),
    "disney": ("Disney+", [337]),
    "hayu": ("Hayu", [296]),
    "hulu": ("Hulu", [15]),
    "max": ("Max", [1899]),
    "netflix": ("Netflix", [8]),
    "now": ("Now", [39]),
    "paramount": ("Paramount+", [531]),
    "peacock": ("Peacock", [386, 387]),
    "showtime": ("Showtime", [37]),
}

# Movie universes: key -> (collection name, builder dict)
UNIVERSES: dict[str, tuple[str, dict[str, Any]]] = {
    "mcu": ("Marvel Cinematic Universe", {"tmdb_keyword": 180547}),
    "trek": (
        "Star Trek Universe",
        {"trakt_list": "https://trakt.tv/users/rzepkowski/lists/star-trek-movies?sort=rank,asc"},
    ),
    "star": (
        "Star Wars Universe",
        {"trakt_list": "https://trakt.tv/users/rzepkowski/lists/star-wars-movies?sort=rank,asc"},
    ),
    "wizard": ("Wizarding World", {"tmdb_collection": [1241, 435259]}),
}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _format_name(variables: dict[str, Any], key: str, default: str, key_name: str) -> str:
    """Collection name: `name_<key>` override, else name_format (with <<key_name>>), else default."""
    override = variables.get(f"name_{key}")
    if override:
        return str(override)
    name_format = variables.get("name_format")
    if name_format:
        return str(name_format).replace("<<key_name>>", key_name)
    return default


def _imdb(media_type: MediaType, variables: dict[str, Any]) -> dict[str, Any]:
    is_movie = media_type == MediaType.MOVIE
    collections: dict[str, Any] = {}

    # IMDb sometimes answers its chart pages with a bot challenge. `top_list` / `popular_list`
    # (a Trakt or MDBList list URL) replace the chart in that case.
    def source(chart_id: str, limit: int, alternative: Any) -> dict[str, Any]:
        if alternative:
            url = str(alternative)
            return {"mdblist_list": url} if "mdblist.com" in url else {"trakt_list": url}
        return {"imdb_chart": {"list_ids": [chart_id], "limit": limit}}

    top_name = str(variables.get("name_top") or ("IMDb Top 250 Movies" if is_movie else "IMDb Top 250 TV"))
    collections[top_name] = {
        **source("top" if is_movie else "toptv", 250, variables.get("top_list")),
        "sync_mode": "sync",
        "collection_order": "custom",
    }

    popular_name = str(variables.get("name_popular") or ("IMDb Popular" if is_movie else "IMDb Popular"))
    collections[popular_name] = {
        **source("moviemeter" if is_movie else "tvmeter", 100, variables.get("popular_list")),
        "sync_mode": "sync",
        "collection_order": "custom",
    }

    if is_movie and _truthy(variables.get("use_lowest")):
        lowest_name = str(variables.get("name_lowest") or "IMDb Lowest Rated Movies")
        collections[lowest_name] = {
            "imdb_chart": {"list_ids": ["bottom"], "limit": 100},
            "sync_mode": "sync",
        }

    return {"collections": collections}


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def _network(media_type: MediaType, variables: dict[str, Any]) -> Optional[dict[str, Any]]:
    if media_type == MediaType.MOVIE:
        return None

    include = _as_list(variables.get("include"))
    exclude = {n.lower() for n in _as_list(variables.get("exclude"))}
    addons: dict[str, list[str]] = variables.get("addons") or {}
    sync_mode = str(variables.get("sync_mode") or "sync")
    sort_by = variables.get("sort_by")

    collections: dict[str, Any] = {}
    for name in include:
        if name.lower() in exclude:
            continue
        network_id = NETWORKS.get(name)
        if network_id is None:
            logger.warning(f"Network default: unknown network '{name}' (skipped)")
            continue

        network_ids = [network_id]
        for addon in _as_list(addons.get(name)):
            addon_id = NETWORKS.get(addon)
            if addon_id is not None:
                network_ids.append(addon_id)

        collection_name = _format_name(variables, name.lower(), f"{name} Shows", name)
        collections[collection_name] = {
            "tmdb_discover": {
                "with_networks": network_ids,
                "sort_by": "popularity.desc",
                "limit": 300,
            },
            "sync_mode": sync_mode,
            "collection_order": "alpha" if sort_by and str(sort_by).startswith("title") else "custom",
        }

    return {"collections": collections}


def _streaming(media_type: MediaType, variables: dict[str, Any]) -> dict[str, Any]:
    include = _as_list(variables.get("include")) or list(STREAMING)
    exclude = {n.lower() for n in _as_list(variables.get("exclude"))}
    sync_mode = str(variables.get("sync_mode") or "sync")
    sort_by = variables.get("sort_by")
    is_movie = media_type == MediaType.MOVIE
    suffix = "Movies" if is_movie else "Shows"

    collections: dict[str, Any] = {}
    for key in include:
        key = key.lower()
        if key in exclude:
            continue
        service = STREAMING.get(key)
        if service is None:
            logger.warning(f"Streaming default: unknown service '{key}' (skipped)")
            continue
        display, provider_ids = service

        discover: dict[str, Any] = {
            "with_watch_providers": provider_ids,
            "sort_by": "popularity.desc",
            "limit": 300,
        }
        if variables.get("watch_region"):
            discover["watch_region"] = str(variables["watch_region"])

        name = _format_name(variables, key, f"{display} {suffix}", display)
        collections[name] = {
            "tmdb_discover": discover,
            "sync_mode": sync_mode,
            "collection_order": "alpha" if sort_by and str(sort_by).startswith("title") else "custom",
        }

    return {"collections": collections}


def _universe(media_type: MediaType, variables: dict[str, Any]) -> Optional[dict[str, Any]]:
    if media_type != MediaType.MOVIE:
        return None

    exclude = {n.lower() for n in _as_list(variables.get("exclude"))}
    include = [n.lower() for n in _as_list(variables.get("include"))]
    keys = include or list(UNIVERSES)

    collections: dict[str, Any] = {}
    for key in keys:
        if key in exclude:
            continue
        entry = UNIVERSES.get(key)
        if entry is None:
            # Kometa ships more universes (avp, arrow, ...); those are not built in.
            continue
        default_name, builder = entry
        collections[_format_name(variables, key, default_name, default_name)] = {
            **builder,
            "sync_mode": "sync",
            "collection_order": "release",
        }

    return {"collections": collections}


def build_default_collections(
    name: str,
    media_type: MediaType,
    variables: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Return a Kometa-style document for `- default: <name>`, or None if unsupported."""
    variables = variables or {}
    key = name.strip().lower()

    if key == "imdb":
        return _imdb(media_type, variables)
    if key == "network":
        return _network(media_type, variables)
    if key == "streaming":
        return _streaming(media_type, variables)
    if key == "universe":
        return _universe(media_type, variables)
    return None
