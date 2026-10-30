"""Builder tests for library queries, labels and out-of-season handling (fake Jellyfin/TMDb)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from jfc.models.collection import Collection, CollectionConfig, CollectionItem
from jfc.models.media import LibraryItem, MediaType
from jfc.models.report import CollectionReport
from jfc.parsers.kometa import KometaParser
from jfc.services.collection_builder import CollectionBuilder


def lib(i: str, title: str, **kw) -> LibraryItem:
    return LibraryItem(
        jellyfin_id=i, title=title, media_type=kw.pop("media_type", MediaType.SERIES),
        library_id="L", library_name="Serien", **kw,
    )


class FakeJellyfin:
    def __init__(self, items):
        self.items = items
        self.tag_updates: dict[str, tuple] = {}
        self.deleted: list[str] = []

    async def get_library_items(self, **kwargs):
        assert kwargs.get("detailed") is True
        return self.items

    async def update_item_tags(self, item_id, add=None, remove=None):
        self.tag_updates[item_id] = (sorted(add or []), sorted(remove or []))
        return True

    async def get_collections(self):
        return [{"Name": "Seasonal", "Id": "c9"}, {"Name": "Other", "Id": "c1"}]

    async def delete_collection(self, collection_id):
        self.deleted.append(collection_id)
        return True


def make_builder(items, statuses=None) -> CollectionBuilder:
    builder = CollectionBuilder.__new__(CollectionBuilder)
    builder.dry_run = False
    builder.jellyfin = FakeJellyfin(items)
    builder.tmdb = MagicMock()
    builder.tmdb.get_series_status = AsyncMock(side_effect=lambda tid: (statuses or {}).get(tid))
    builder.matcher = MagicMock(preload_limit=1000)
    builder._detailed_libraries = {}
    builder._tmdb_status_cache = {}
    return builder


def parse(tmp_path, text):
    path = tmp_path / "c.yml"
    path.write_text(text, encoding="utf-8")
    return KometaParser(tmp_path).parse_collection_file(path)


@pytest.mark.asyncio
async def test_tmdb_status_filter_and_labels(tmp_path):
    items = [
        lib("1", "Running", tmdb_id=1, tags=["ReturningShows"]),
        lib("2", "Gone", tmdb_id=2, tags=["ReturningShows"]),   # ended now: label must be removed
        lib("3", "New", tmdb_id=3),
    ]
    builder = make_builder(items, {1: "Returning Series", 2: "Ended", 3: "Returning Series"})
    config = parse(
        tmp_path,
        """
collections:
  Returning Shows:
    build_collection: false
    sync_mode: append
    item_label: ReturningShows
    non_item_remove_label: ReturningShows
    plex_all: true
    filters:
      tmdb_status: returning
""",
    )[0]

    media_items = await builder._fetch_library_query(config, "L", MediaType.SERIES)
    assert [m.title for m in media_items] == ["Running", "New"]
    assert all(m.jellyfin_id for m in media_items)

    collection = Collection(
        config=config, library_name="Serien", library_id="L",
        items=[CollectionItem(title=m.title, jellyfin_id=m.jellyfin_id, media_type="series", matched=True) for m in media_items],
    )
    await builder.apply_labels(collection)

    # "Running" already has the label (untouched), "New" gets it, "Gone" loses it
    assert builder.jellyfin.tag_updates == {"3": (["ReturningShows"], []), "2": ([], ["ReturningShows"])}


@pytest.mark.asyncio
async def test_plex_search_all_any_and_limit(tmp_path):
    items = [
        lib("1", "A", media_type=MediaType.MOVIE, edition="IMAX", year=2018),
        lib("2", "B", media_type=MediaType.MOVIE, edition="Imax-E", year=2010),
        lib("3", "C", media_type=MediaType.MOVIE, edition="Director's Cut"),
    ]
    builder = make_builder(items)
    config = parse(
        tmp_path,
        """
collections:
  IMAX:
    plex_search:
      any:
        edition: [Imax, Extended Imax, Imax-E]
      limit: 1
      sort_by: title.desc
""",
    )[0]
    result = await builder._fetch_library_query(config, "L", MediaType.MOVIE)
    assert [m.title for m in result] == ["B"]


@pytest.mark.asyncio
async def test_item_label_remove(tmp_path):
    builder = make_builder([lib("1", "S", tags=["FinalEpisode", "Keep"])])
    config = parse(tmp_path, "collections:\n  Final:\n    plex_all: true\n    item_label.remove: FinalEpisode\n    build_collection: false\n")[0]
    items = await builder._fetch_library_query(config, "L", MediaType.SERIES)
    collection = Collection(
        config=config, library_name="Serien", library_id="L",
        items=[CollectionItem(title=items[0].title, jellyfin_id="1", media_type="series", matched=True)],
    )
    await builder.apply_labels(collection)
    assert builder.jellyfin.tag_updates == {"1": ([], ["FinalEpisode"])}


@pytest.mark.asyncio
async def test_label_only_collection_does_not_create_jellyfin_collection(tmp_path):
    builder = make_builder([lib("1", "S")])
    builder.jellyfin.create_collection = AsyncMock()
    config = CollectionConfig(name="L", build_collection=False, item_label=["X"])
    collection = Collection(
        config=config, library_name="Serien", library_id="L",
        items=[CollectionItem(title="S", jellyfin_id="1", media_type="series", matched=True)],
    )
    await builder.sync_collection(collection, CollectionReport(name="L", library="Serien", schedule="daily", source_provider="x"))
    builder.jellyfin.create_collection.assert_not_awaited()
    assert builder.jellyfin.tag_updates == {"1": (["X"], [])}


@pytest.mark.asyncio
async def test_delete_out_of_season_collection():
    builder = make_builder([])
    assert await builder.delete_collection_by_name("Seasonal") == 1
    assert builder.jellyfin.deleted == ["c9"]


@pytest.mark.asyncio
async def test_below_minimum_deletes_only_with_some_matches():
    builder = make_builder([])
    builder.jellyfin.get_collections = AsyncMock(return_value=[{"Name": "T", "Id": "c1", "ChildCount": 5}])
    builder.jellyfin.delete_collection = AsyncMock(return_value=True)
    config = CollectionConfig(name="T", minimum_items=2, delete_below_minimum=True)

    one = Collection(config=config, library_name="F", library_id="L",
                     items=[CollectionItem(title="a", jellyfin_id="1", matched=True), CollectionItem(title="b")])
    one.update_stats()
    await builder.sync_collection(one, CollectionReport(name="T", library="F", schedule="daily", source_provider="x"))
    builder.jellyfin.delete_collection.assert_awaited_once_with("c1")

    # nothing matched = library may be incomplete: never delete
    builder.jellyfin.delete_collection.reset_mock()
    builder.jellyfin.get_collection_items = AsyncMock(return_value=["x"])
    none = Collection(config=config, library_name="F", library_id="L", items=[CollectionItem(title="a")])
    none.update_stats()
    await builder.sync_collection(none, CollectionReport(name="T", library="F", schedule="daily", source_provider="x"))
    builder.jellyfin.delete_collection.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_item_tags_posts_full_dto_and_only_when_changed():
    from unittest.mock import MagicMock
    from jfc.clients.jellyfin import JellyfinClient

    client = JellyfinClient.__new__(JellyfinClient)
    dto = {"Id": "1", "Name": "Show", "Tags": ["A", "Old"], "LockData": True, "LockedFields": ["Name"], "CommunityRating": 7.4}
    client.get_item_for_update = AsyncMock(return_value=dto)
    posted = {}

    async def post(url, json=None):
        posted["url"], posted["json"] = url, json
        return MagicMock(status_code=204)

    client.post = post
    assert await client.update_item_tags("1", add=["New", "a"], remove=["old"])
    assert posted["url"] == "/Items/1"
    assert posted["json"]["Tags"] == ["A", "New"]
    # untouched properties travel along unchanged
    assert posted["json"]["LockData"] is True and posted["json"]["CommunityRating"] == 7.4

    # already in the desired state: nothing is posted
    posted.clear()
    client.get_item_for_update = AsyncMock(return_value={"Id": "1", "Tags": ["A", "New"]})
    assert await client.update_item_tags("1", add=["New"], remove=["Gone"])
    assert posted == {}


@pytest.mark.asyncio
async def test_empty_provider_result_changes_nothing():
    builder = make_builder([])
    builder.jellyfin.get_collections = AsyncMock(return_value=[{"Name": "IMDb", "Id": "c1", "ChildCount": 250}])
    builder.jellyfin.get_collection_items = AsyncMock(return_value=["a"] * 250)
    builder.jellyfin.remove_from_collection = AsyncMock(return_value=True)
    builder.jellyfin.create_collection = AsyncMock()
    config = CollectionConfig(name="IMDb")  # provider based, e.g. imdb_chart blocked by IMDb
    empty = Collection(config=config, library_name="F", library_id="L", items=[])
    empty.update_stats()
    report = CollectionReport(name="IMDb", library="F", schedule="daily", source_provider="x")
    assert await builder.sync_collection(empty, report) == (0, 0, None)
    builder.jellyfin.remove_from_collection.assert_not_awaited()

    # no existing collection: no empty collection is created either
    builder.jellyfin.get_collections = AsyncMock(return_value=[])
    await builder.sync_collection(empty, report)
    builder.jellyfin.create_collection.assert_not_awaited()


@pytest.mark.asyncio
async def test_library_paging_survives_short_pages():
    """Jellyfin sometimes returns a short page in the middle; loading must continue."""
    from unittest.mock import MagicMock
    from jfc.clients.jellyfin import JellyfinClient

    total = 1200
    calls = []

    async def fake_get(url, params=None):
        start, limit = params["StartIndex"], params["Limit"]
        calls.append((start, limit))
        end = min(start + limit, total)
        count = end - start
        if start == 500:
            count = 46  # server answers short although more items follow
        items = [{"Id": f"i{n}", "Name": f"T{n}", "Type": "Series"} for n in range(start, start + count)]
        return MagicMock(status_code=200, raise_for_status=lambda: None, json=lambda: {"Items": items, "TotalRecordCount": total})

    client = JellyfinClient.__new__(JellyfinClient)
    client.get = fake_get
    JellyfinClient._PAGE_RETRY_DELAY = 0
    items = await client.get_library_items("lib", limit=50000)
    # the persistently short page is retried twice, then treated as a server-side gap, not the end
    assert [c[0] for c in calls] == [0, 500, 500, 500, 1000]
    assert len(items) == 500 + 46 + 200

    # a normal last page still ends the loop
    calls.clear()
    total = 700
    items = await client.get_library_items("lib", limit=50000)
    assert [c[0] for c in calls] == [0, 500]


@pytest.mark.asyncio
async def test_library_paging_recovers_when_retry_succeeds():
    from unittest.mock import MagicMock
    from jfc.clients.jellyfin import JellyfinClient

    attempts = {"n": 0}

    async def fake_get(url, params=None):
        start, limit = params["StartIndex"], params["Limit"]
        count = min(limit, 1200 - start)
        if start == 500:
            attempts["n"] += 1
            if attempts["n"] == 1:
                count = 46
        items = [{"Id": f"i{n}", "Name": f"T{n}", "Type": "Movie"} for n in range(start, start + count)]
        return MagicMock(status_code=200, raise_for_status=lambda: None, json=lambda: {"Items": items, "TotalRecordCount": 1200})

    client = JellyfinClient.__new__(JellyfinClient)
    client.get = fake_get
    JellyfinClient._PAGE_RETRY_DELAY = 0
    assert len(await client.get_library_items("lib", limit=50000)) == 1200


@pytest.mark.asyncio
async def test_library_queries_do_not_collapse_collection_members():
    from unittest.mock import MagicMock
    from jfc.clients.jellyfin import JellyfinClient

    seen = []

    async def fake_get(url, params=None):
        seen.append(params)
        return MagicMock(status_code=200, raise_for_status=lambda: None, json=lambda: {"Items": [], "TotalRecordCount": 0})

    client = JellyfinClient.__new__(JellyfinClient)
    client.get = fake_get
    await client.get_library_items("lib")
    await client.search_items("x")
    await client.find_by_tmdb_id(1, library_id="lib")
    assert seen and all(p.get("CollapseBoxSetItems") is False for p in seen)


@pytest.mark.asyncio
async def test_mass_removal_is_blocked():
    builder = make_builder([])
    builder.max_removal_ratio = 0.5
    current = [f"old{i}" for i in range(20)]
    builder.jellyfin.get_collections = AsyncMock(return_value=[{"Name": "DV", "Id": "c1", "ChildCount": 20}])
    builder.jellyfin.get_collection_items = AsyncMock(return_value=current)
    builder.jellyfin.remove_from_collection = AsyncMock(return_value=True)
    builder.jellyfin.add_to_collection = AsyncMock(return_value=True)
    builder.jellyfin.update_collection_metadata = AsyncMock(return_value=True)
    builder._upload_poster = AsyncMock(return_value=(None, None))
    config = CollectionConfig(name="DV", plex_all=True)  # library query: empty result is valid, but huge removal is not
    collection = Collection(
        config=config, library_name="F", library_id="L",
        items=[CollectionItem(title="n", jellyfin_id="new1", matched=True), CollectionItem(title="m", jellyfin_id="old0", matched=True)],
    )
    collection.update_stats()
    builder.radarr = None
    builder.sonarr = None
    report = CollectionReport(name="DV", library="F", schedule="daily", source_provider="x")
    await builder.sync_collection(collection, report)

    # the 19 items that would have been removed are kept (reorder path re-adds the full list)
    final = builder.jellyfin.add_to_collection.await_args_list[-1].args[1]
    assert set(current) <= set(final) and "new1" in final
    assert report.items_removed_from_collection == 0
