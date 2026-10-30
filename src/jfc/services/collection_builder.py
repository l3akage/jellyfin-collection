"""Service for building collections from Kometa configurations."""

import random
import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional

from loguru import logger

import asyncio

from jfc.clients.imdb import IMDbClient
from jfc.clients.jellyfin import JellyfinClient
from jfc.clients.mdblist import MDBListClient
from jfc.clients.radarr import RadarrClient
from jfc.clients.sonarr import SonarrClient
from jfc.clients.tmdb import TMDbClient
from jfc.clients.trakt import TraktClient
from jfc.core.config import Settings, get_settings
from jfc.models.collection import (
    Collection,
    CollectionConfig,
    CollectionFilter,
    CollectionItem,
    CollectionOrder,
    SyncMode,
)
from jfc.models.media import LibraryItem, MediaItem, MediaType, Movie, Series
from jfc.models.report import CollectionReport
from jfc.services.library_query import LibraryQuery
from jfc.services.media_matcher import MediaMatcher
from jfc.services.poster_generator import PosterGenerator


_TRAKT_LIST_RE = re.compile(r"trakt\.tv/users/([^/]+)/lists/([^/?#]+)", re.IGNORECASE)


def _parse_trakt_list_url(url: str) -> Optional[tuple[str, str]]:
    """`https://trakt.tv/users/<user>/lists/<slug>?...` -> (user, slug)."""
    match = _TRAKT_LIST_RE.search(url)
    return (match.group(1), match.group(2)) if match else None


class CollectionBuilder:
    """Builds and updates collections in Jellyfin from Kometa configurations."""

    # Overridden from settings in __init__
    radarr_add_missing_default: bool = True
    sonarr_add_missing_default: bool = True
    max_removal_ratio: float = 0.5
    MIN_REMOVALS_FOR_GUARD = 5

    def __init__(
        self,
        jellyfin: JellyfinClient,
        tmdb: TMDbClient,
        trakt: Optional[TraktClient] = None,
        imdb: Optional[IMDbClient] = None,
        radarr: Optional[RadarrClient] = None,
        sonarr: Optional[SonarrClient] = None,
        poster_generator: Optional[PosterGenerator] = None,
        dry_run: bool = False,
        mdblist: Optional[MDBListClient] = None,
    ):
        """
        Initialize collection builder.

        Args:
            jellyfin: Jellyfin API client
            tmdb: TMDb API client
            trakt: Optional Trakt API client
            imdb: Optional IMDb client
            radarr: Optional Radarr client for adding missing movies
            sonarr: Optional Sonarr client for adding missing series
            poster_generator: Optional AI poster generator
            dry_run: If True, don't make any changes
        """
        self.jellyfin = jellyfin
        self.tmdb = tmdb
        self.trakt = trakt
        self.imdb = imdb
        self.radarr = radarr
        self.sonarr = sonarr
        self.poster_generator = poster_generator
        self.dry_run = dry_run
        self.mdblist = mdblist

        # Per-run caches (see reset_run_caches)
        self._detailed_libraries: dict[str, list[LibraryItem]] = {}
        self._tmdb_status_cache: dict[int, Optional[str]] = {}

        settings = get_settings()
        self.matcher = MediaMatcher(jellyfin, preload_limit=settings.matcher_preload_limit)
        self.radarr_only_released = settings.radarr_only_released
        self.max_removal_ratio = getattr(settings, "max_removal_ratio", 0.5)
        self.radarr_add_missing_default = getattr(settings.radarr, "add_missing", True)
        self.sonarr_add_missing_default = getattr(settings.sonarr, "add_missing", True)

    def reset_run_caches(self) -> None:
        """Forget library snapshots so a new run sees items added to Jellyfin meanwhile."""
        self._detailed_libraries.clear()
        self._tmdb_status_cache.clear()

    async def build_collection(
        self,
        config: CollectionConfig,
        library_name: str,
        library_id: str,
        media_type: MediaType,
    ) -> tuple[Collection, CollectionReport]:
        """
        Build a collection from configuration.

        Args:
            config: Collection configuration
            library_name: Name of the Jellyfin library
            library_id: Jellyfin library ID
            media_type: Type of media (movie/series)

        Returns:
            Tuple of (Built collection with items, Collection report)
        """
        start_time = time.time()
        logger.info(f"Building collection: {config.name}")

        # Initialize report
        report = CollectionReport(
            name=config.name,
            library=library_name,
            schedule=config.schedule.schedule_type.value,
            source_provider=self._get_source_provider(config),
        )

        # Fetch items from providers
        items = await self._fetch_items(config, library_id, media_type)
        report.items_fetched = len(items)
        report.fetched_titles = [item.title for item in items]

        # Store original source items (before filtering) for poster generation
        # This ensures the poster reflects the true trending/source order
        source_items = [
            CollectionItem(
                title=item.title,
                year=item.year,
                tmdb_id=item.tmdb_id,
                imdb_id=item.imdb_id,
                tvdb_id=item.tvdb_id,
                media_type=item.media_type.value,
                overview=item.overview,
                genres=item.genres if item.genres else None,
                poster_path=item.poster_path,
            )
            for item in items
        ]

        # Apply filters
        filtered_items = self._apply_filters(items, config)
        report.items_after_filter = len(filtered_items)

        # Match items to library
        collection_items = []
        for item in filtered_items:
            if item.jellyfin_id:
                # Already a Jellyfin library item (library queries)
                lib_item = LibraryItem(
                    jellyfin_id=item.jellyfin_id,
                    title=item.title,
                    year=item.year,
                    media_type=item.media_type,
                    tmdb_id=item.tmdb_id,
                    imdb_id=item.imdb_id,
                    tvdb_id=item.tvdb_id,
                    library_id=library_id,
                    library_name=library_name,
                )
            else:
                lib_item = await self.matcher.find_in_library(item, library_id)

            collection_item = CollectionItem(
                title=item.title,
                year=item.year,
                tmdb_id=item.tmdb_id,
                imdb_id=item.imdb_id,
                tvdb_id=item.tvdb_id,
                jellyfin_id=lib_item.jellyfin_id if lib_item else None,
                media_type=item.media_type.value,  # "movie" or "series"
                matched=lib_item is not None,
                in_library=lib_item is not None,
                # Preserve metadata for AI poster generation
                overview=item.overview,
                genres=item.genres if item.genres else None,
                poster_path=item.poster_path,
            )
            collection_items.append(collection_item)

            # Track matched/missing titles
            if lib_item:
                report.matched_titles.append(item.title)
            else:
                report.missing_titles.append(item.title)

        collection = Collection(
            config=config,
            library_name=library_name,
            library_id=library_id,
            items=collection_items,
            source_items=source_items,  # Original items for poster generation
        )
        collection.update_stats()

        # Update report stats
        report.items_matched = collection.matched_items
        report.items_missing = collection.missing_items
        report.calculate_match_rate()
        report.duration_seconds = time.time() - start_time

        logger.info(
            f"Collection '{config.name}': {collection.matched_items}/{collection.total_items} "
            f"items matched, {collection.missing_items} missing"
        )

        return collection, report

    def _get_source_provider(self, config: CollectionConfig) -> str:
        """Determine the primary source provider from config."""
        sources = []
        if config.tmdb_trending_weekly or config.tmdb_trending_daily:
            sources.append("TMDb Trending")
        if config.tmdb_popular:
            sources.append("TMDb Popular")
        if config.tmdb_now_playing:
            sources.append("TMDb Now Playing")
        if config.tmdb_airing_today or config.tmdb_on_the_air:
            sources.append("TMDb Airing")
        if config.tmdb_discover:
            sources.append("TMDb Discover")
        if config.tmdb_list:
            sources.append("TMDb List")
        if config.trakt_trending:
            sources.append("Trakt Trending")
        if config.trakt_popular:
            sources.append("Trakt Popular")
        if config.trakt_chart:
            chart = config.trakt_chart.get("chart", "unknown")
            sources.append(f"Trakt {chart.capitalize()}")
        if config.imdb_chart:
            sources.append("IMDb Chart")
        if config.imdb_list:
            sources.append("IMDb List")
        if config.radarr_taglist:
            sources.append("Radarr Taglist")
        if config.sonarr_taglist:
            sources.append("Sonarr Taglist")
        if config.plex_search or config.plex_all:
            sources.append("Library Search")
        if config.tmdb_movie or config.tmdb_show or config.tvdb_show:
            sources.append("ID List")
        if config.tmdb_collection or config.tmdb_keyword or config.tmdb_director:
            sources.append("TMDb Franchise")
        if config.trakt_lists:
            sources.append("Trakt List")
        if config.mdblist_lists:
            sources.append("MDBList")
        return ", ".join(sources) if sources else "Unknown"

    async def sync_collection(
        self,
        collection: Collection,
        report: CollectionReport,
        media_type: MediaType = MediaType.MOVIE,
        add_missing_to_arr: bool = True,
        force_poster: bool = False,
        posters_only: bool = False,
    ) -> tuple[int, int, Optional[Path]]:
        """
        Sync collection to Jellyfin.

        Args:
            collection: Collection to sync
            report: Collection report to update with sync info
            media_type: Type of media (for poster category)
            add_missing_to_arr: Whether to add missing items to Radarr/Sonarr
            force_poster: Force regeneration of AI poster
            posters_only: Only generate/upload poster, skip item sync

        Returns:
            Tuple of (items_added, items_removed, poster_path)
        """
        if self.dry_run:
            logger.info(f"[DRY RUN] Would sync collection: {collection.config.name}")
            return (0, 0, None)

        # Label-only collections (Kometa `build_collection: false`): no Jellyfin collection
        if not collection.config.build_collection:
            if not posters_only:
                await self.apply_labels(collection, report)
            return (0, 0, None)

        # Get or create Jellyfin collection
        jellyfin_collections = await self.jellyfin.get_collections()
        existing_matches = [
            c for c in jellyfin_collections if c.get("Name") == collection.config.name
        ]
        existing = None
        if existing_matches:
            existing = max(existing_matches, key=lambda c: c.get("ChildCount", 0))
            if len(existing_matches) > 1:
                ids = ", ".join(c.get("Id", "unknown") for c in existing_matches)
                logger.warning(
                    f"Found {len(existing_matches)} collections named "
                    f"'{collection.config.name}'. Using ID {existing.get('Id')} "
                    f"(largest ChildCount). IDs: {ids}"
                )

        report.collection_existed = existing is not None

        # A provider that returned nothing (e.g. IMDb answering with a bot challenge, a failed API
        # call) must neither create an empty collection nor empty an existing one. Library
        # queries are different: "no match" is a valid result there.
        is_library_query = collection.config.plex_all or bool(collection.config.plex_search)
        if not posters_only and collection.total_items == 0 and not is_library_query:
            logger.warning(
                f"'{collection.config.name}': source returned no items, collection left unchanged"
            )
            return (0, 0, None)

        # Kometa minimum_items / delete_below_minimum. "Nothing matched" is ambiguous (the library
        # may still be indexing), so an existing collection is never deleted in that case; it is
        # left to the wipe guard below.
        if (
            not posters_only
            and collection.total_items > 0
            and collection.matched_items < collection.config.minimum_items
        ):
            if collection.matched_items > 0:
                logger.info(
                    f"'{collection.config.name}': {collection.matched_items} matched items "
                    f"< minimum_items={collection.config.minimum_items}, collection not built"
                )
                if existing and collection.config.delete_below_minimum:
                    await self.jellyfin.delete_collection(existing["Id"])
                    logger.info(f"Deleted '{collection.config.name}' (below minimum)")
                return (0, 0, None)
            if not existing:
                return (0, 0, None)

        if existing:
            collection.jellyfin_id = existing["Id"]
        else:
            collection.jellyfin_id = await self.jellyfin.create_collection(
                collection.config.name
            )

        to_add: set[str] = set()
        to_remove: set[str] = set()
        to_add_list: list[str] = []
        to_remove_list: list[str] = []

        # Skip item sync if posters_only mode
        if not posters_only:
            # Get current items in collection
            current_ids = set(await self.jellyfin.get_collection_items(collection.jellyfin_id))

            # Sort items according to collection_order
            sorted_items = self._sort_items_for_collection(
                collection.items,
                collection.config.collection_order,
            )

            # Get target items in sorted order (only matched ones)
            target_ids_list = [
                item.jellyfin_id for item in sorted_items if item.jellyfin_id
            ]
            target_ids = set(target_ids_list)

            # Safety: never wipe a populated collection when nothing matched. This happens when
            # Jellyfin is still indexing (e.g. right after a server upgrade/restart) and the library
            # looks empty; emptying the collection would lose it until the next run.
            if not target_ids_list and current_ids and collection.total_items > 0:
                logger.warning(
                    f"No items matched for '{collection.config.name}' "
                    f"({collection.total_items} fetched) but collection has {len(current_ids)} items: "
                    f"skipping sync to avoid wiping it (library may be incomplete)"
                )
                return (0, 0, None)

            # Calculate changes based on sync mode
            # Preserve source order for additions (important for ranked lists like IMDb Top 250)
            to_add_list = [item_id for item_id in target_ids_list if item_id not in current_ids]
            to_add = set(to_add_list)
            if collection.config.sync_mode == SyncMode.SYNC:
                to_remove = current_ids - target_ids
                to_remove_list = list(to_remove)
            else:
                to_remove = set()
                to_remove_list = []

            # Mass-removal guard: an unusually large removal usually means the library listing was
            # incomplete, not that the collection really lost most of its items
            if (
                to_remove
                and len(to_remove) >= self.MIN_REMOVALS_FOR_GUARD
                and len(to_remove) > self.max_removal_ratio * len(current_ids)
            ):
                logger.warning(
                    f"'{collection.config.name}': sync would remove {len(to_remove)} of "
                    f"{len(current_ids)} items (limit {self.max_removal_ratio:.0%}); removals skipped. "
                    "Check that the Jellyfin library is complete, or raise settings.max_removal_ratio."
                )
                target_ids_list = target_ids_list + [i for i in current_ids if i not in target_ids]
                target_ids = set(target_ids_list)
                to_remove = set()
                to_remove_list = []

            # Track added/removed titles for report
            added_jellyfin_ids = to_add
            for item in collection.items:
                if item.jellyfin_id in added_jellyfin_ids:
                    report.added_titles.append(item.title)

            # Determine if we need to reorder (clear and re-add all)
            # Jellyfin displays items in the order they were added
            needs_reorder = (
                (
                    collection.config.collection_order != CollectionOrder.CUSTOM
                    or (
                        collection.config.collection_order == CollectionOrder.CUSTOM
                        and collection.config.sync_mode == SyncMode.SYNC
                        and existing is not None
                    )
                )
                and (to_add or to_remove or not existing)
            )

            if needs_reorder and target_ids_list:
                # Clear all items and re-add in sorted order
                if current_ids:
                    removed_ok = await self.jellyfin.remove_from_collection(
                        collection.jellyfin_id, list(current_ids)
                    )
                    if not removed_ok:
                        raise RuntimeError(
                            f"Failed to clear collection '{collection.config.name}' before reorder"
                        )
                added_ok = await self.jellyfin.add_to_collection(
                    collection.jellyfin_id, target_ids_list
                )
                if not added_ok:
                    raise RuntimeError(
                        f"Failed to re-add items while reordering '{collection.config.name}'"
                    )
                logger.info(
                    f"Reordered '{collection.config.name}' ({len(target_ids_list)} items, "
                    f"order={collection.config.collection_order.value})"
                )
            else:
                # Simple add/remove (no reordering needed)
                if to_add_list:
                    added_ok = await self.jellyfin.add_to_collection(
                        collection.jellyfin_id, to_add_list
                    )
                    if not added_ok:
                        raise RuntimeError(
                            f"Failed to add items to collection '{collection.config.name}'"
                        )
                    logger.info(f"Added {len(to_add_list)} items to '{collection.config.name}'")

                if to_remove_list:
                    removed_ok = await self.jellyfin.remove_from_collection(
                        collection.jellyfin_id, to_remove_list
                    )
                    if not removed_ok:
                        raise RuntimeError(
                            f"Failed to remove items from collection '{collection.config.name}'"
                        )
                    logger.info(f"Removed {len(to_remove_list)} items from '{collection.config.name}'")

            # Update report
            report.items_added_to_collection = len(to_add_list)
            report.items_removed_from_collection = len(to_remove_list)

            # Update metadata (including DisplayOrder for Jellyfin sorting)
            display_order = self._get_jellyfin_display_order(collection.config.collection_order)
            await self.jellyfin.update_collection_metadata(
                collection.jellyfin_id,
                overview=collection.config.summary,
                sort_name=collection.config.sort_title,
                display_order=display_order,
            )

        # Upload poster (manual or AI-generated)
        _, poster_path = await self._upload_poster(collection, media_type, force_regenerate=force_poster)

        # Labels (Kometa item_label / non_item_remove_label / item_label.remove)
        if not posters_only:
            await self.apply_labels(collection, report)

        # Add missing items to Radarr/Sonarr
        if add_missing_to_arr and not posters_only:
            radarr_count, sonarr_count = await self._add_missing_to_arr(collection, report)
            report.items_sent_to_radarr = radarr_count
            report.items_sent_to_sonarr = sonarr_count

        return (len(to_add_list), len(to_remove_list), poster_path)

    async def expand_dynamic(
        self,
        parser: Any,
        config: CollectionConfig,
        library_id: str,
        media_type: MediaType,
    ) -> list[CollectionConfig]:
        """
        Expand a Kometa `dynamic_collections` entry of `type: tmdb_collection` into one collection
        per TMDb franchise with enough movies in the library (franchise = Jellyfin provider id
        `TmdbCollection`).
        """
        if config.dynamic_type != "tmdb_collection" or media_type != MediaType.MOVIE:
            return []

        spec = config.dynamic_spec
        minimum = parser.dynamic_minimum(spec)
        excluded = {str(x) for x in (spec.get("exclude") or [])}
        included = {str(x) for x in (spec.get("include") or [])}

        counts: dict[int, int] = {}
        for item in await self.get_detailed_library(library_id, media_type):
            if item.tmdb_collection_id:
                counts[item.tmdb_collection_id] = counts.get(item.tmdb_collection_id, 0) + 1

        candidates = [
            cid for cid, n in sorted(counts.items())
            if n >= minimum and str(cid) not in excluded and (not included or str(cid) in included)
        ]
        logger.info(f"[Dynamic] '{config.name}': {len(candidates)} TMDb franchises with >= {minimum} movies")

        remove_suffix = str(spec.get("remove_suffix") or "")
        remove_prefix = str(spec.get("remove_prefix") or "")
        results: list[CollectionConfig] = []
        for cid in candidates:
            details = await self.tmdb.get_collection_details(cid)
            if not details:
                continue
            title = details[0]
            if remove_suffix and title.endswith(remove_suffix):
                title = title[: -len(remove_suffix)].rstrip()
            if remove_prefix and title.startswith(remove_prefix):
                title = title[len(remove_prefix):].lstrip()
            results.append(parser.build_dynamic_instance(spec, cid, title))
        return results

    async def delete_collection_by_name(self, name: str) -> int:
        """Delete every Jellyfin collection with this name (seasonal collections out of season)."""
        if self.dry_run:
            logger.info(f"[DRY RUN] Would delete collection: {name}")
            return 0

        deleted = 0
        for existing in await self.jellyfin.get_collections():
            if existing.get("Name") == name and await self.jellyfin.delete_collection(existing["Id"]):
                deleted += 1
        if deleted:
            logger.info(f"Deleted out-of-season collection '{name}'")
        return deleted

    async def apply_labels(self, collection: Collection, report: Optional[CollectionReport] = None) -> None:
        """
        Maintain Jellyfin tags for the Kometa label options of a collection:

        - `item_label`: tag added to every matched item
        - `non_item_remove_label`: tag removed from library items that are not matched
        - `item_label.remove`: tag removed from every matched item
        """
        config = collection.config
        if not (config.item_label or config.non_item_remove_label or config.item_label_remove):
            return
        if not collection.library_id:
            logger.warning(f"Cannot apply labels for '{config.name}': library unknown")
            return

        media_type = self._media_type_of(collection)
        library = await self.get_detailed_library(collection.library_id, media_type)
        by_id = {item.jellyfin_id: item for item in library}
        matched_ids = {i.jellyfin_id for i in collection.items if i.jellyfin_id}

        add_ops: dict[str, list[str]] = {}
        remove_ops: dict[str, list[str]] = {}

        def current_tags(item_id: str) -> set[str]:
            item = by_id.get(item_id)
            return {t.lower() for t in item.tags} if item else set()

        for item_id in matched_ids:
            tags = current_tags(item_id)
            missing = [label for label in config.item_label if label.lower() not in tags]
            if missing:
                add_ops.setdefault(item_id, []).extend(missing)
            stale = [label for label in config.item_label_remove if label.lower() in tags]
            if stale:
                remove_ops.setdefault(item_id, []).extend(stale)

        if config.non_item_remove_label:
            for item in library:
                if item.jellyfin_id in matched_ids:
                    continue
                tags = current_tags(item.jellyfin_id)
                stale = [label for label in config.non_item_remove_label if label.lower() in tags]
                if stale:
                    remove_ops.setdefault(item.jellyfin_id, []).extend(stale)

        if self.dry_run:
            logger.info(
                f"[DRY RUN] Labels for '{config.name}': +{len(add_ops)} items, -{len(remove_ops)} items"
            )
            return

        semaphore = asyncio.Semaphore(4)

        async def apply(item_id: str) -> bool:
            async with semaphore:
                ok = await self.jellyfin.update_item_tags(
                    item_id, add=add_ops.get(item_id), remove=remove_ops.get(item_id)
                )
            if ok and item_id in by_id:
                tags = [t for t in by_id[item_id].tags if t.lower() not in {r.lower() for r in remove_ops.get(item_id, [])}]
                tags.extend(t for t in add_ops.get(item_id, []) if t.lower() not in {x.lower() for x in tags})
                by_id[item_id].tags = tags
            return ok

        touched = sorted(set(add_ops) | set(remove_ops))
        if not touched:
            return
        results = await asyncio.gather(*(apply(i) for i in touched))
        failed = results.count(False)
        logger.info(
            f"Labels for '{config.name}': +{len(add_ops)} / -{len(remove_ops)} items"
            + (f" ({failed} failed)" if failed else "")
        )

    @staticmethod
    def _media_type_of(collection: Collection) -> MediaType:
        for item in collection.items:
            if item.media_type == "series":
                return MediaType.SERIES
            if item.media_type == "movie":
                return MediaType.MOVIE
        from jfc.models.media import infer_media_type

        return infer_media_type(collection.library_name)

    async def _fetch_items(
        self,
        config: CollectionConfig,
        library_id: str,
        media_type: MediaType,
    ) -> list[MediaItem]:
        """Fetch items from configured providers."""
        items: list[MediaItem] = []

        # TMDb Trending
        if config.tmdb_trending_weekly:
            if media_type == MediaType.MOVIE:
                items.extend(
                    await self.tmdb.get_trending_movies("week", config.tmdb_trending_weekly)
                )
            else:
                items.extend(
                    await self.tmdb.get_trending_series("week", config.tmdb_trending_weekly)
                )

        if config.tmdb_trending_daily:
            if media_type == MediaType.MOVIE:
                items.extend(
                    await self.tmdb.get_trending_movies("day", config.tmdb_trending_daily)
                )
            else:
                items.extend(
                    await self.tmdb.get_trending_series("day", config.tmdb_trending_daily)
                )

        # TMDb Popular
        if config.tmdb_popular:
            if media_type == MediaType.MOVIE:
                items.extend(await self.tmdb.get_popular_movies(config.tmdb_popular))
            else:
                items.extend(await self.tmdb.get_popular_series(config.tmdb_popular))

        # TMDb Now Playing (movies only)
        if config.tmdb_now_playing and media_type == MediaType.MOVIE:
            items.extend(await self.tmdb.get_now_playing_movies(config.tmdb_now_playing))

        # TMDb Airing Today / On The Air (series only)
        if config.tmdb_airing_today and media_type != MediaType.MOVIE:
            items.extend(await self.tmdb.get_airing_today_series(config.tmdb_airing_today))

        if config.tmdb_on_the_air and media_type != MediaType.MOVIE:
            items.extend(await self.tmdb.get_on_the_air_series(config.tmdb_on_the_air))

        # TMDb Discover
        if config.tmdb_discover:
            discover_items = await self._fetch_tmdb_discover(
                config.tmdb_discover, media_type, config.filters
            )
            items.extend(discover_items)

        # TMDb List
        if config.tmdb_list:
            list_ids = self._normalize_tmdb_list_ids(config.tmdb_list)
            for list_id in list_ids:
                items.extend(
                    await self.tmdb.get_list(
                        list_id=list_id,
                        media_type=media_type,
                    )
                )

        # IMDb
        if self.imdb:
            if config.imdb_chart:
                items.extend(await self._fetch_imdb_chart(config.imdb_chart, media_type))
            if config.imdb_list:
                items.extend(await self._fetch_imdb_list(config.imdb_list, media_type))
        elif config.imdb_chart or config.imdb_list:
            logger.warning("IMDb builders configured but IMDb client is not available")

        # Arr tag lists
        if config.radarr_taglist:
            if self.radarr and media_type == MediaType.MOVIE:
                items.extend(await self._fetch_radarr_taglist(config.radarr_taglist))
            elif not self.radarr:
                logger.warning("radarr_taglist configured but Radarr client is not available")

        if config.sonarr_taglist:
            if self.sonarr and media_type == MediaType.SERIES:
                items.extend(await self._fetch_sonarr_taglist(config.sonarr_taglist))
            elif not self.sonarr:
                logger.warning("sonarr_taglist configured but Sonarr client is not available")

        # Trakt
        if self.trakt:
            if config.trakt_trending:
                if media_type == MediaType.MOVIE:
                    items.extend(
                        await self.trakt.get_trending_movies(config.trakt_trending)
                    )
                else:
                    items.extend(
                        await self.trakt.get_trending_series(config.trakt_trending)
                    )

            if config.trakt_popular:
                if media_type == MediaType.MOVIE:
                    items.extend(
                        await self.trakt.get_popular_movies(config.trakt_popular)
                    )
                else:
                    items.extend(
                        await self.trakt.get_popular_series(config.trakt_popular)
                    )

            if config.trakt_chart:
                chart_items = await self._fetch_trakt_chart(config.trakt_chart, media_type)
                items.extend(chart_items)

        # IDs / lists from TMDb, TVDb, Trakt and MDBList
        items.extend(await self._fetch_id_sources(config, media_type))

        # Library queries (Kometa plex_all / plex_search)
        if config.plex_all or config.plex_search:
            items.extend(await self._fetch_library_query(config, library_id, media_type))

        # Deduplicate by external IDs while preserving order
        seen_keys: set[tuple[str, str]] = set()
        unique_items: list[MediaItem] = []

        for item in items:
            dedupe_key: Optional[tuple[str, str]] = None
            if item.tmdb_id:
                dedupe_key = ("tmdb", str(item.tmdb_id))
            elif item.imdb_id:
                dedupe_key = ("imdb", item.imdb_id)
            elif item.tvdb_id:
                dedupe_key = ("tvdb", str(item.tvdb_id))
            elif item.title:
                year = item.year if item.year else 0
                dedupe_key = ("title", f"{item.title.lower()}::{year}")

            if not dedupe_key:
                continue

            if dedupe_key in seen_keys:
                continue

            seen_keys.add(dedupe_key)
            unique_items.append(item)

        return unique_items

    async def get_detailed_library(self, library_id: str, media_type: MediaType) -> list[LibraryItem]:
        """All items of a library with tags, streams and dates (cached for the current run)."""
        cached = self._detailed_libraries.get(library_id)
        if cached is not None:
            return cached

        items = await self.jellyfin.get_library_items(
            library_id=library_id,
            media_type=media_type,
            limit=self.matcher.preload_limit,
            detailed=True,
        )
        self._detailed_libraries[library_id] = items
        logger.info(f"[Jellyfin] Loaded {len(items)} detailed items for library {library_id}")
        return items

    async def _load_tmdb_statuses(self, items: list[LibraryItem]) -> dict[int, str]:
        """TMDb status ('Returning Series', 'Ended', 'Canceled', ...) for series with a TMDb ID."""
        wanted = sorted({i.tmdb_id for i in items if i.tmdb_id and i.tmdb_id not in self._tmdb_status_cache})
        semaphore = asyncio.Semaphore(8)

        async def fetch(tmdb_id: int) -> None:
            async with semaphore:
                try:
                    self._tmdb_status_cache[tmdb_id] = await self.tmdb.get_series_status(tmdb_id)
                except Exception as e:
                    logger.warning(f"TMDb status lookup failed for {tmdb_id}: {e}")
                    self._tmdb_status_cache[tmdb_id] = None

        if wanted:
            logger.info(f"[TMDb] Looking up status of {len(wanted)} series")
            await asyncio.gather(*(fetch(t) for t in wanted))

        return {i.tmdb_id: self._tmdb_status_cache[i.tmdb_id] for i in items if self._tmdb_status_cache.get(i.tmdb_id)}

    async def _fetch_library_query(
        self,
        config: CollectionConfig,
        library_id: str,
        media_type: MediaType,
    ) -> list[MediaItem]:
        """
        Items of the Jellyfin library matching `plex_all` + `filters` and/or `plex_search`.

        See `LibraryQuery` for the supported attributes. Returned items already carry their
        Jellyfin ID.
        """
        search = config.plex_search if isinstance(config.plex_search, dict) else {}
        conditions: dict[str, Any] = {"all": [c for c in (search, config.library_filters) if c]}

        library_items = await self.get_detailed_library(library_id, media_type)

        query = LibraryQuery()
        if LibraryQuery.uses_attribute(conditions, "tmdb_status"):
            if media_type == MediaType.SERIES:
                query.set_tmdb_statuses(await self._load_tmdb_statuses(library_items))
            else:
                logger.warning(f"[{config.name}] tmdb_status filter only applies to series libraries")

        matched = query.filter(library_items, conditions)
        matched = LibraryQuery.sort(matched, search.get("sort_by"))

        limit = search.get("limit")
        try:
            if limit:
                matched = matched[: max(1, int(limit))]
        except (TypeError, ValueError):
            pass

        items: list[MediaItem] = []
        for lib_item in matched:
            media_item = lib_item.to_media_item()
            media_item.media_type = media_type
            items.append(media_item)

        logger.info(f"[Jellyfin] Library query '{config.name}': {len(items)} of {len(library_items)} items")
        return items

    async def _fetch_id_sources(self, config: CollectionConfig, media_type: MediaType) -> list[MediaItem]:
        """tmdb_movie/show, tvdb_movie/show, tmdb_collection/keyword/director, trakt_list, mdblist_list."""
        items: list[MediaItem] = []
        is_movie = media_type == MediaType.MOVIE

        # Explicit IDs
        if is_movie:
            for tmdb_id in config.tmdb_movie:
                details = await self.tmdb.get_movie_details(tmdb_id)
                items.append(details or Movie(title=f"tmdb:{tmdb_id}", tmdb_id=tmdb_id))
            for tvdb_id in config.tvdb_movie:
                logger.debug(f"tvdb_movie {tvdb_id} is not resolvable for movies, skipped")
        else:
            for tmdb_id in config.tmdb_show:
                details = await self.tmdb.get_series_details(tmdb_id)
                items.append(details or Series(title=f"tmdb:{tmdb_id}", tmdb_id=tmdb_id))
            for tvdb_id in config.tvdb_show:
                items.append(Series(title=f"tvdb:{tvdb_id}", tvdb_id=tvdb_id))

        # TMDb franchises / keywords / directors (movies)
        if is_movie:
            for collection_id in config.tmdb_collection:
                items.extend(await self.tmdb.get_collection_parts(collection_id))
            for keyword_id in config.tmdb_keyword:
                items.extend(await self.tmdb.discover_movies(with_keywords=[keyword_id], limit=200))
            for person_id in config.tmdb_director:
                items.extend(await self.tmdb.get_director_movies(person_id))
        else:
            for keyword_id in config.tmdb_keyword:
                items.extend(await self.tmdb.discover_series(with_keywords=[keyword_id], limit=200))

        # Trakt lists
        for list_url in config.trakt_lists:
            parsed = _parse_trakt_list_url(list_url)
            if not parsed:
                logger.warning(f"[{config.name}] Unsupported Trakt list URL: {list_url}")
                continue
            if not self.trakt:
                logger.warning(f"[{config.name}] trakt_list configured but Trakt is not authenticated")
                break
            try:
                items.extend(await self.trakt.get_list(parsed[0], parsed[1], media_type))
            except Exception as e:
                logger.warning(f"[{config.name}] Trakt list {list_url} failed: {e}")

        # MDBList lists
        for list_url in config.mdblist_lists:
            if not self.mdblist:
                logger.warning(f"[{config.name}] mdblist_list configured but no MDBList client available")
                break
            try:
                items.extend(await self.mdblist.get_list(list_url, media_type))
            except Exception as e:
                logger.warning(f"[{config.name}] MDBList {list_url} failed: {e}")

        return items

    async def _fetch_tmdb_discover(
        self,
        discover: dict[str, Any],
        media_type: MediaType,
        filters: Optional[CollectionFilter] = None,
    ) -> list[MediaItem]:
        """Fetch items from TMDb discover endpoint with optimized filters."""
        base_limit = discover.get("limit", 20)

        # Calculate dynamic limit multiplier based on exclusion filters
        # Since TMDb doesn't support excluding languages/countries, we fetch more
        # items to compensate for post-filtering losses
        limit_multiplier = 1.0
        if filters:
            if filters.original_language_not:
                # Each excluded language might filter ~20-50% of results
                limit_multiplier += 0.5 * len(filters.original_language_not)
            if filters.origin_country_not:
                # Each excluded country might filter ~20-50% of results
                limit_multiplier += 0.5 * len(filters.origin_country_not)

        # Cap the multiplier at 4x to avoid excessive API calls
        limit_multiplier = min(limit_multiplier, 4.0)
        adjusted_limit = int(base_limit * limit_multiplier)

        if limit_multiplier > 1.0:
            logger.debug(
                f"Adjusted limit from {base_limit} to {adjusted_limit} "
                f"(multiplier: {limit_multiplier:.1f}x) to compensate for exclusion filters"
            )

        # Merge filters into discover parameters
        # Priority: discover params > filter params (discover is more specific)
        vote_avg_gte = discover.get("vote_average.gte")
        if vote_avg_gte is None and filters and filters.vote_average_gte:
            vote_avg_gte = filters.vote_average_gte

        vote_count_gte = discover.get("vote_count.gte")
        if vote_count_gte is None and filters and filters.tmdb_vote_count_gte:
            vote_count_gte = filters.tmdb_vote_count_gte

        if media_type == MediaType.MOVIE:
            # Convert year filter to date filter if not already set
            release_date_gte = discover.get("primary_release_date.gte")
            if release_date_gte is None and filters and filters.year_gte:
                release_date_gte = date(filters.year_gte, 1, 1)

            return await self.tmdb.discover_movies(
                sort_by=discover.get("sort_by", "popularity.desc"),
                with_genres=discover.get("with_genres"),
                without_genres=discover.get("without_genres"),
                vote_average_gte=vote_avg_gte,
                vote_average_lte=discover.get("vote_average.lte"),
                vote_count_gte=vote_count_gte,
                vote_count_lte=discover.get("vote_count.lte"),
                primary_release_date_gte=release_date_gte,
                primary_release_date_lte=discover.get("primary_release_date.lte"),
                with_watch_providers=discover.get("with_watch_providers"),
                watch_region=discover.get("watch_region"),
                with_original_language=discover.get("with_original_language"),
                with_release_type=discover.get("with_release_type"),
                region=discover.get("region"),
                with_keywords=discover.get("with_keywords"),
                with_companies=discover.get("with_companies"),
                limit=adjusted_limit,
            )
        else:
            # Convert year filter to date filter if not already set
            first_air_date_gte = discover.get("first_air_date.gte")
            if first_air_date_gte is None and filters and filters.year_gte:
                first_air_date_gte = date(filters.year_gte, 1, 1)

            return await self.tmdb.discover_series(
                sort_by=discover.get("sort_by", "popularity.desc"),
                with_genres=discover.get("with_genres"),
                without_genres=discover.get("without_genres"),
                vote_average_gte=vote_avg_gte,
                vote_count_gte=vote_count_gte,
                vote_count_lte=discover.get("vote_count.lte"),
                first_air_date_gte=first_air_date_gte,
                first_air_date_lte=discover.get("first_air_date.lte"),
                with_watch_providers=discover.get("with_watch_providers"),
                watch_region=discover.get("watch_region"),
                with_status=discover.get("with_status"),
                with_original_language=discover.get("with_original_language"),
                with_origin_country=discover.get("with_origin_country"),
                with_networks=discover.get("with_networks"),
                with_keywords=discover.get("with_keywords"),
                with_companies=discover.get("with_companies"),
                limit=adjusted_limit,
            )

    def _normalize_tmdb_list_ids(
        self,
        tmdb_list: str | int | list[str | int],
    ) -> list[int]:
        """Normalize tmdb_list config value to a list of TMDb list IDs."""
        values = tmdb_list if isinstance(tmdb_list, list) else [tmdb_list]
        normalized_ids: list[int] = []

        for value in values:
            list_id = self._extract_tmdb_list_id(value)
            if list_id is not None:
                normalized_ids.append(list_id)

        return normalized_ids

    def _extract_tmdb_list_id(self, value: str | int) -> Optional[int]:
        """Extract numeric TMDb list ID from int, string, or list URL."""
        if isinstance(value, int):
            return value

        raw_value = str(value).strip()
        if raw_value.isdigit():
            return int(raw_value)

        # Accept full URL format like: https://www.themoviedb.org/list/710
        match = re.search(r"/list/(\d+)", raw_value)
        if match:
            return int(match.group(1))

        logger.warning(f"Invalid tmdb_list value '{value}' expected ID or TMDb list URL")
        return None

    async def _fetch_imdb_chart(
        self,
        imdb_chart: dict[str, Any],
        media_type: MediaType,
    ) -> list[MediaItem]:
        """Fetch items from IMDb charts and resolve to TMDb items."""
        if not self.imdb:
            return []

        chart_ids = self._normalize_imdb_ids(imdb_chart.get("list_ids"))
        limit = imdb_chart.get("limit")
        imdb_ids: list[str] = []

        for chart_id in chart_ids:
            chart_limit = int(limit) if limit else 250
            imdb_ids.extend(await self.imdb.get_chart(chart_id, limit=chart_limit))

        return await self._resolve_imdb_ids(imdb_ids, media_type, limit=limit)

    async def _fetch_imdb_list(
        self,
        imdb_list: dict[str, Any],
        media_type: MediaType,
    ) -> list[MediaItem]:
        """Fetch items from IMDb custom lists and resolve to TMDb items."""
        if not self.imdb:
            return []

        list_ids = self._normalize_imdb_ids(imdb_list.get("list_ids"))
        limit = imdb_list.get("limit")
        imdb_ids: list[str] = []

        for list_id in list_ids:
            list_limit = int(limit) if limit else 250
            imdb_ids.extend(await self.imdb.get_list(list_id, limit=list_limit))

        return await self._resolve_imdb_ids(imdb_ids, media_type, limit=limit)

    async def _fetch_radarr_taglist(self, config: dict[str, Any]) -> list[MediaItem]:
        """Fetch movies from Radarr filtered by tag names."""
        if not self.radarr:
            return []

        tags = self._normalize_imdb_ids(config.get("tags"))
        limit = config.get("limit")
        max_items = int(limit) if limit else None

        movies = await self.radarr.get_movies()
        tag_map = {
            tag.get("id"): str(tag.get("label", "")).strip().lower()
            for tag in await self.radarr.get_tags()
        }
        tag_set = {tag.lower() for tag in tags}

        results: list[MediaItem] = []
        for movie in movies:
            movie_tag_ids = movie.get("tags", [])
            movie_tag_names = {
                tag_map[tag_id] for tag_id in movie_tag_ids if tag_id in tag_map and tag_map[tag_id]
            }
            if not movie_tag_names.intersection(tag_set):
                continue

            results.append(
                Movie(
                    title=movie.get("title", "Unknown"),
                    year=movie.get("year"),
                    tmdb_id=movie.get("tmdbId"),
                    imdb_id=movie.get("imdbId"),
                    overview=movie.get("overview"),
                    genres=movie.get("genres", []),
                )
            )

            if max_items and len(results) >= max_items:
                break

        logger.info(f"[Radarr] Taglist ({', '.join(tags)}): fetched {len(results)} items")
        return results

    async def _fetch_sonarr_taglist(self, config: dict[str, Any]) -> list[MediaItem]:
        """Fetch series from Sonarr filtered by tag names."""
        if not self.sonarr:
            return []

        tags = self._normalize_imdb_ids(config.get("tags"))
        limit = config.get("limit")
        max_items = int(limit) if limit else None

        series_list = await self.sonarr.get_series()
        tag_map = {
            tag.get("id"): str(tag.get("label", "")).strip().lower()
            for tag in await self.sonarr.get_tags()
        }
        tag_set = {tag.lower() for tag in tags}

        results: list[MediaItem] = []
        for series in series_list:
            series_tag_ids = series.get("tags", [])
            series_tag_names = {
                tag_map[tag_id] for tag_id in series_tag_ids if tag_id in tag_map and tag_map[tag_id]
            }
            if not series_tag_names.intersection(tag_set):
                continue

            results.append(
                Series(
                    title=series.get("title", "Unknown"),
                    year=series.get("year"),
                    tmdb_id=series.get("tmdbId"),
                    tvdb_id=series.get("tvdbId"),
                    imdb_id=series.get("imdbId"),
                    overview=series.get("overview"),
                    genres=series.get("genres", []),
                )
            )

            if max_items and len(results) >= max_items:
                break

        logger.info(f"[Sonarr] Taglist ({', '.join(tags)}): fetched {len(results)} items")
        return results

    def _normalize_imdb_ids(self, values: Any) -> list[str]:
        """Normalize IMDb chart/list ID values into a clean list."""
        if values is None:
            return []

        raw_values = values if isinstance(values, list) else [values]
        normalized: list[str] = []

        for value in raw_values:
            value_str = str(value).strip()
            if value_str:
                normalized.append(value_str)

        return normalized

    async def _resolve_imdb_ids(
        self,
        imdb_ids: list[str],
        media_type: MediaType,
        limit: Optional[int] = None,
    ) -> list[MediaItem]:
        """Resolve IMDb IDs to TMDb media items."""
        resolved: list[MediaItem] = []
        seen: set[str] = set()
        max_items = int(limit) if limit else None

        for imdb_id in imdb_ids:
            if imdb_id in seen:
                continue
            seen.add(imdb_id)

            item = await self.tmdb.find_by_imdb_id(imdb_id, media_type=media_type)
            if item:
                resolved.append(item)

            if max_items and len(resolved) >= max_items:
                break

        return resolved

    async def _fetch_trakt_chart(
        self,
        chart_config: dict[str, Any],
        media_type: MediaType,
    ) -> list[MediaItem]:
        """Fetch items from Trakt chart."""
        if not self.trakt:
            return []

        chart = chart_config.get("chart", "watched")
        period = chart_config.get("time_period", "weekly")
        limit = chart_config.get("limit", 20)

        if chart == "watched":
            if media_type == MediaType.MOVIE:
                return await self.trakt.get_watched_movies(period, limit)
            else:
                return await self.trakt.get_watched_series(period, limit)

        if chart == "trending":
            if media_type == MediaType.MOVIE:
                return await self.trakt.get_trending_movies(limit)
            else:
                return await self.trakt.get_trending_series(limit)

        if chart == "popular":
            if media_type == MediaType.MOVIE:
                return await self.trakt.get_popular_movies(limit)
            else:
                return await self.trakt.get_popular_series(limit)

        return []

    def _apply_filters(
        self,
        items: list[MediaItem],
        config: CollectionConfig,
    ) -> list[MediaItem]:
        """Apply collection filters to items."""
        filters = config.filters
        filtered = []

        for item in items:
            # Year filters
            if filters.year_gte and item.year and item.year < filters.year_gte:
                logger.debug(f"Filtered out '{item.title}': year={item.year} < {filters.year_gte}")
                continue
            if filters.year_lte and item.year and item.year > filters.year_lte:
                logger.debug(f"Filtered out '{item.title}': year={item.year} > {filters.year_lte}")
                continue

            # Rating filters
            if filters.vote_average_gte and item.vote_average:
                if item.vote_average < filters.vote_average_gte:
                    continue
            if filters.critic_rating_gte and item.vote_average:
                if item.vote_average < filters.critic_rating_gte:
                    continue

            # Vote count filters
            if filters.tmdb_vote_count_gte and item.vote_count:
                if item.vote_count < filters.tmdb_vote_count_gte:
                    continue

            # Country filters
            if filters.country_not and item.original_country:
                if item.original_country in filters.country_not:
                    logger.debug(f"Filtered out '{item.title}': country={item.original_country}")
                    continue
            if filters.origin_country_not and item.original_country:
                if item.original_country in filters.origin_country_not:
                    logger.debug(f"Filtered out '{item.title}': origin_country={item.original_country}")
                    continue

            # Language filter (e.g., exclude Japanese anime)
            if filters.original_language_not and item.original_language:
                if item.original_language in filters.original_language_not:
                    logger.debug(f"Filtered out '{item.title}': language={item.original_language}")
                    continue

            # Genre filters (genres stored as list of IDs)
            if filters.without_genres and item.genres:
                # item.genres can be list of ints or list of strings
                item_genre_ids = [
                    g if isinstance(g, int) else int(g) if str(g).isdigit() else 0
                    for g in item.genres
                ]
                if any(g in item_genre_ids for g in filters.without_genres):
                    logger.debug(f"Filtered out '{item.title}': excluded genre")
                    continue

            if filters.with_genres and item.genres:
                item_genre_ids = [
                    g if isinstance(g, int) else int(g) if str(g).isdigit() else 0
                    for g in item.genres
                ]
                if not any(g in item_genre_ids for g in filters.with_genres):
                    logger.debug(f"Filtered out '{item.title}': missing required genre")
                    continue

            filtered.append(item)

        # Apply limit
        if config.limit:
            filtered = filtered[: config.limit]

        return filtered

    def _sort_items_for_collection(
        self,
        items: list[CollectionItem],
        order: CollectionOrder,
    ) -> list[CollectionItem]:
        """
        Sort collection items according to specified order.

        For 'custom' order, items are kept in their original source order.
        For other orders, items are sorted and will be added to Jellyfin
        in that order (achieving the desired display order).

        Args:
            items: Collection items to sort
            order: Sort order to apply

        Returns:
            Sorted list of items
        """
        if order == CollectionOrder.CUSTOM:
            # Keep original order from source
            return items

        if order == CollectionOrder.RANDOM:
            shuffled = items.copy()
            random.shuffle(shuffled)
            return shuffled

        # Define sort key functions
        def sort_key_name(x: CollectionItem) -> str:
            return (x.sort_name or x.title).lower()

        def sort_key_premiere(x: CollectionItem) -> tuple:
            # Use year as fallback, sort descending (newest first)
            d = x.premiere_date or (date(x.year, 1, 1) if x.year else date.min)
            return (d, x.title)

        def sort_key_rating(x: CollectionItem) -> tuple:
            # Sort descending (highest first) - use negative
            return (-(x.community_rating or 0), x.title)

        def sort_key_critic(x: CollectionItem) -> tuple:
            return (-(x.critic_rating or 0), x.title)

        def sort_key_created(x: CollectionItem) -> tuple:
            return (x.date_created or date.min, x.title)

        sort_keys = {
            CollectionOrder.SORT_NAME: sort_key_name,
            CollectionOrder.PREMIERE_DATE: sort_key_premiere,
            CollectionOrder.COMMUNITY_RATING: sort_key_rating,
            CollectionOrder.CRITIC_RATING: sort_key_critic,
            CollectionOrder.DATE_CREATED: sort_key_created,
        }

        key_func = sort_keys.get(order)
        if key_func:
            # Descending for dates (newest first)
            reverse = order in {
                CollectionOrder.PREMIERE_DATE,
                CollectionOrder.DATE_CREATED,
            }
            return sorted(items, key=key_func, reverse=reverse)

        return items

    def _get_jellyfin_display_order(self, order: CollectionOrder) -> str:
        """
        Map CollectionOrder to Jellyfin DisplayOrder value.

        Jellyfin DisplayOrder values:
        - "Default" - Default order (insertion order)
        - "SortName" - Alphabetical by sort name
        - "PremiereDate" - By release/premiere date
        - "DateCreated" - By date added to library
        - "CommunityRating" - By community rating

        Args:
            order: Collection order enum

        Returns:
            Jellyfin DisplayOrder string value
        """
        order_mapping = {
            CollectionOrder.CUSTOM: "Default",
            CollectionOrder.SORT_NAME: "SortName",
            # Keep app-level newest-first insertion order for release date.
            # Jellyfin's PremiereDate display order is often oldest-first.
            CollectionOrder.PREMIERE_DATE: "Default",
            CollectionOrder.DATE_CREATED: "DateCreated",
            CollectionOrder.COMMUNITY_RATING: "CommunityRating",
            CollectionOrder.CRITIC_RATING: "CommunityRating",  # Jellyfin uses same field
            CollectionOrder.RANDOM: "Default",  # No random in Jellyfin, use default
        }
        return order_mapping.get(order, "Default")

    async def _upload_poster(
        self,
        collection: Collection,
        media_type: MediaType,
        force_regenerate: bool = False,
    ) -> tuple[bool, Optional[Path]]:
        """
        Upload poster image for collection if configured.

        If no poster is configured but AI generation is enabled,
        generates a poster automatically using OpenAI.

        Args:
            collection: Collection with poster config
            media_type: Type of media (for AI category mapping)
            force_regenerate: Force regeneration of AI poster

        Returns:
            Tuple of (success, poster_path)
        """
        if not collection.jellyfin_id:
            return False, None

        settings = get_settings()
        poster_path: Optional[Path] = None

        # 1. Force regenerate with AI if requested and enabled
        if force_regenerate and self.poster_generator and settings.openai.enabled:
            # Map media type to category
            category = self._get_poster_category(collection.library_name, media_type)

            # Use source_items (original provider order) for poster generation
            # This ensures the poster reflects the true trending list, not just available items
            poster_items = collection.source_items if collection.source_items else collection.items
            media_items = self._collection_items_to_media_items(poster_items)

            logger.info(f"Force regenerating AI poster for '{collection.config.name}'...")
            poster_path = await self.poster_generator.generate_poster(
                config=collection.config,
                items=media_items,
                category=category,
                library=collection.library_name,
                force_regenerate=True,
                explicit_refs=settings.openai.explicit_refs,
            )
            if poster_path:
                logger.success(f"Generated AI poster: {poster_path.name}")

        # 2. Check for manually configured poster
        elif collection.config.poster:
            manual_path = settings.get_posters_path() / collection.config.poster
            if manual_path.exists():
                poster_path = manual_path
            elif self.poster_generator and settings.openai.enabled:
                # Manual poster not found, fallback to AI
                logger.debug(f"Manual poster '{collection.config.poster}' not found, using AI")

        # 3. If no manual poster and AI generation enabled, generate one
        if not poster_path and self.poster_generator and settings.openai.enabled:
            # Map media type to category
            category = self._get_poster_category(collection.library_name, media_type)

            # Use source_items (original provider order) for poster generation
            poster_items = collection.source_items if collection.source_items else collection.items
            media_items = self._collection_items_to_media_items(poster_items)

            logger.info(f"Generating AI poster for '{collection.config.name}'...")
            poster_path = await self.poster_generator.generate_poster(
                config=collection.config,
                items=media_items,
                category=category,
                library=collection.library_name,
                force_regenerate=False,
                explicit_refs=settings.openai.explicit_refs,
            )
            if poster_path:
                logger.success(f"Generated AI poster: {poster_path.name}")

        if not poster_path or not poster_path.exists():
            return False, None

        try:
            success = await self.jellyfin.upload_collection_poster(
                collection.jellyfin_id,
                poster_path,
            )
            if success:
                logger.success(f"Uploaded poster for '{collection.config.name}'")
            return success, poster_path
        except FileNotFoundError:
            logger.warning(
                f"Poster file not found for '{collection.config.name}': {poster_path}"
            )
            return False, poster_path
        except ValueError as e:
            logger.warning(f"Invalid poster for '{collection.config.name}': {e}")
            return False, poster_path
        except Exception as e:
            logger.error(f"Failed to upload poster for '{collection.config.name}': {e}")
            return False, poster_path

    def _get_poster_category(self, library_name: str, media_type: MediaType) -> str:
        """
        Map library/media type to poster category.

        Args:
            library_name: Name of the Jellyfin library
            media_type: Type of media

        Returns:
            Category string: FILMS, SÉRIES, or CARTOONS
        """
        lib_lower = library_name.lower()

        # Check for cartoon/animation library
        if any(x in lib_lower for x in ["cartoon", "animation", "anime", "enfant", "kids"]):
            return "CARTOONS"

        # Based on media type
        if media_type == MediaType.SERIES:
            return "SÉRIES"

        return "FILMS"

    def _collection_items_to_media_items(
        self,
        items: list[CollectionItem],
    ) -> list[MediaItem]:
        """
        Convert CollectionItems back to MediaItems for poster generation context.

        Includes title, year, overview, and genres for AI poster generation.

        Args:
            items: Collection items

        Returns:
            List of MediaItem objects
        """
        media_items = []
        for item in items:
            media_type = MediaType.SERIES if item.media_type == "series" else MediaType.MOVIE
            if media_type == MediaType.MOVIE:
                media_item = Movie(
                    tmdb_id=item.tmdb_id,
                    title=item.title,
                    year=item.year,
                    overview=item.overview,
                    genres=item.genres,
                )
            else:
                media_item = Series(
                    tmdb_id=item.tmdb_id,
                    title=item.title,
                    year=item.year,
                    overview=item.overview,
                    genres=item.genres,
                )
            media_items.append(media_item)
        return media_items

    async def _add_missing_to_arr(
        self,
        collection: Collection,
        report: CollectionReport,
    ) -> tuple[int, int]:
        """Add missing items to Radarr/Sonarr.

        Uses library-level settings from collection config if available,
        otherwise falls back to client defaults.

        Returns:
            Tuple of (radarr_count, sonarr_count)
        """
        missing = [item for item in collection.items if not item.in_library]
        radarr_count = 0
        sonarr_count = 0

        if not missing:
            return (0, 0)

        # Get library-level settings (item-level tag takes priority over library-level)
        config = collection.config

        radarr_enabled = (
            config.radarr_add_missing
            if config.radarr_add_missing is not None
            else self.radarr_add_missing_default
        )
        sonarr_enabled = (
            config.sonarr_add_missing
            if config.sonarr_add_missing is not None
            else self.sonarr_add_missing_default
        )

        for item in missing:
            # Use media_type to determine Sonarr vs Radarr
            is_series = item.media_type == "series"
            if (is_series and not sonarr_enabled) or (not is_series and not radarr_enabled):
                continue

            if is_series and self.sonarr:
                # Sonarr settings: item_sonarr_tag > sonarr_tag > client default
                tag = config.item_sonarr_tag or config.sonarr_tag
                tags = [tag] if tag else None

                # Get tvdb_id - fetch from TMDb if not already available
                tvdb_id = item.tvdb_id
                if not tvdb_id and item.tmdb_id:
                    # Fetch series details from TMDb to get tvdb_id
                    series_details = await self.tmdb.get_series_details(item.tmdb_id)
                    if series_details:
                        tvdb_id = series_details.tvdb_id

                if not tvdb_id:
                    logger.warning(f"Cannot add '{item.title}' to Sonarr: no TVDB ID found")
                    continue

                try:
                    added = await self.sonarr.add_series(
                        tvdb_id=tvdb_id,
                        root_folder=config.sonarr_root_folder,
                        quality_profile=config.sonarr_quality_profile,
                        tags=tags,
                    )
                    # Only count real additions (skips existing/excluded/blocklisted)
                    if added:
                        sonarr_count += 1
                        report.sonarr_titles.append(item.title)
                except Exception as e:
                    logger.warning(f"Failed to add '{item.title}' to Sonarr: {e}")

            elif not is_series and self.radarr and item.tmdb_id:
                # Skip movies still in theaters / unreleased (no digital or physical release)
                if self.radarr_only_released:
                    try:
                        released = await self.tmdb.is_movie_released(item.tmdb_id)
                    except Exception as e:
                        logger.warning(f"Cannot check release status for '{item.title}': {e}")
                        released = False
                    if not released:
                        logger.debug(f"Skipping '{item.title}' for Radarr: not released at home yet")
                        continue

                # Radarr settings: item_radarr_tag > radarr_tag > client default
                tag = config.item_radarr_tag or config.radarr_tag
                tags = [tag] if tag else None

                try:
                    added = await self.radarr.add_movie(
                        tmdb_id=item.tmdb_id,
                        root_folder=config.radarr_root_folder,
                        quality_profile=config.radarr_quality_profile,
                        tags=tags,
                    )
                    # Only count real additions (skips existing/excluded/blocklisted)
                    if added:
                        radarr_count += 1
                        report.radarr_titles.append(item.title)
                except Exception as e:
                    logger.warning(f"Failed to add '{item.title}' to Radarr: {e}")

        return (radarr_count, sonarr_count)
