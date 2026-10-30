"""Tests for Kometa templates, seasonal ranges, defaults and label options."""

from datetime import date, timedelta
from pathlib import Path

import yaml

from jfc.models.collection import CollectionSchedule, ScheduleType
from jfc.models.media import MediaType
from jfc.parsers.defaults import build_default_collections
from jfc.parsers.kometa import KometaParser
from jfc.parsers.seasonal import dynamic_range, easter_sunday


def _parse(tmp_path: Path, content: str, variables=None):
    path = tmp_path / "c.yml"
    path.write_text(content, encoding="utf-8")
    return KometaParser(tmp_path).parse_collection_file(path, variables)


class TestTemplates:
    def test_variables_and_collection_name(self, tmp_path: Path):
        collections = _parse(
            tmp_path,
            """
templates:
  Holiday:
    summary: "Bis <<end_date>> verfügbar: <<collection_name>>"
    schedule: range(<<start_date>>-<<end_date>>)
    delete_not_scheduled: true
collections:
  Halloween:
    template:
      name: Holiday
      start_date: 10/01
      end_date: 10/31
    tmdb_movie: 1, 2
""",
        )
        c = collections[0]
        assert c.summary == "Bis 10/31 verfügbar: Halloween"
        assert c.schedule.schedule_type == ScheduleType.RANGE
        assert c.schedule.range_start == (10, 1) and c.schedule.range_end == (10, 31)
        assert c.delete_not_scheduled is True
        assert c.tmdb_movie == [1, 2]

    def test_list_variable_keeps_type_and_optional_is_dropped(self, tmp_path: Path):
        collections = _parse(
            tmp_path,
            """
templates:
  Movies:
    optional: [tmdb_movie, trakt_list]
    tmdb_movie: <<tmdb_movie>>
    trakt_list: <<trakt_list>>
collections:
  A:
    template: {name: Movies, tmdb_movie: [10, 11]}
""",
        )
        c = collections[0]
        assert c.tmdb_movie == [10, 11]
        assert c.trakt_lists == []

    def test_template_variables_from_config_entry(self, tmp_path: Path):
        collections = _parse(
            tmp_path,
            """
collections:
  <<name>> Movies:
    tmdb_keyword: <<kw>>
""",
            {"name": "Space", "kw": 42},
        )
        assert collections[0].name == "Space Movies"
        assert collections[0].tmdb_keyword == [42]

    def test_unknown_template_does_not_crash(self, tmp_path: Path):
        collections = _parse(tmp_path, "collections:\n  A:\n    template: {name: Nope}\n    tmdb_movie: 5\n")
        assert collections[0].tmdb_movie == [5]

    def test_invalid_yaml_returns_empty(self, tmp_path: Path):
        assert _parse(tmp_path, "collections:\n  A: {x: https://a?b=1,2}\n") == []

    def test_labels_and_build_collection(self, tmp_path: Path):
        c = _parse(
            tmp_path,
            """
collections:
  ComingSoon:
    build_collection: false
    item_label: Coming Soon
    non_item_remove_label: Coming Soon
    tvdb_show: 1, 2,
      3
  Final:
    plex_all: true
    item_label.remove: FinalEpisode
    filters:
      label: A, B
""",
        )
        assert c[0].build_collection is False
        assert c[0].item_label == ["Coming Soon"]
        assert c[0].non_item_remove_label == ["Coming Soon"]
        assert c[0].tvdb_show == [1, 2, 3]
        assert c[1].item_label_remove == ["FinalEpisode"]
        assert c[1].plex_all is True
        assert c[1].library_filters == {"label": "A, B"}

    def test_add_missing_override(self, tmp_path: Path):
        c = _parse(tmp_path, "collections:\n  A:\n    tmdb_movie: 1\n    radarr_add_missing: true\n")
        assert c[0].radarr_add_missing is True and c[0].sonarr_add_missing is None


class TestSchedule:
    def test_range_wraps_year_end(self):
        schedule = CollectionSchedule.from_kometa("range(12/30-01/02)")
        assert schedule.is_active_on(date(2026, 12, 31))
        assert schedule.is_active_on(date(2027, 1, 2))
        assert not schedule.is_active_on(date(2027, 1, 3))
        assert not schedule.is_active_on(date(2026, 12, 29))

    def test_range_same_year(self):
        schedule = CollectionSchedule.from_kometa("range(10/01-10/31)")
        assert schedule.is_active_on(date(2026, 10, 15))
        assert not schedule.is_active_on(date(2026, 11, 1))

    def test_invalid_range_is_never(self):
        assert CollectionSchedule.from_kometa("range(foo)").schedule_type == ScheduleType.NEVER


class TestSeasonal:
    def test_easter_sunday(self):
        assert easter_sunday(2026) == date(2026, 4, 5)
        assert easter_sunday(2027) == date(2027, 3, 28)

    def test_dynamic_ranges(self):
        assert dynamic_range("good_friday_dates", 2026) == ("04/03", "04/04")
        assert dynamic_range("easter_dates", 2026) == ("04/03", "04/06")
        assert dynamic_range("chinese_new_year_dates", 2026) == ("02/17", "02/20")
        assert dynamic_range("shark_week_dates", 2026) is None

    def test_dynamic_overrides_template_dates(self, tmp_path: Path):
        c = _parse(
            tmp_path,
            """
templates:
  Holiday:
    schedule: range(<<start_date>>-<<end_date>>)
collections:
  Ostern:
    dynamic: easter_dates
    template: {name: Holiday, start_date: 01/01, end_date: 01/02}
""",
        )
        good_friday = easter_sunday(date.today().year) - timedelta(days=2)
        assert c[0].schedule.range_start == (good_friday.month, good_friday.day)


class TestDefaults:
    def test_imdb_movies_and_series(self):
        movies = build_default_collections("imdb", MediaType.MOVIE, {"name_popular": "Pop", "use_lowest": False})
        assert set(movies["collections"]) == {"IMDb Top 250 Movies", "Pop"}
        series = build_default_collections("imdb", MediaType.SERIES, {})
        charts = [v["imdb_chart"]["list_ids"][0] for v in series["collections"].values()]
        assert charts == ["toptv", "tvmeter"]

    def test_streaming_name_format_and_include(self):
        doc = build_default_collections(
            "streaming", MediaType.SERIES, {"include": ["netflix", "disney"], "name_format": "<<key_name>>"}
        )
        assert set(doc["collections"]) == {"Netflix", "Disney+"}
        assert doc["collections"]["Netflix"]["tmdb_discover"]["with_watch_providers"] == [8]

    def test_network_empty_include_builds_nothing(self):
        assert build_default_collections("network", MediaType.SERIES, {"include": None})["collections"] == {}

    def test_universe_exclude(self):
        doc = build_default_collections("universe", MediaType.MOVIE, {"exclude": ["trek", "star"]})
        assert set(doc["collections"]) == {"Marvel Cinematic Universe", "Wizarding World"}

    def test_unknown_default(self):
        assert build_default_collections("oscars", MediaType.MOVIE, {}) is None

    def test_default_entry_in_library_config(self, tmp_path: Path):
        (tmp_path / "config.yml").write_text(
            yaml.safe_dump(
                {
                    "libraries": {
                        "Serien": {
                            "collection_files": [
                                {"default": "streaming", "template_variables": {"include": ["netflix"]}}
                            ]
                        }
                    }
                }
            )
        )
        result = KometaParser(tmp_path).get_all_collections()
        assert [c.name for c in result["Serien"]] == ["Netflix Shows"]
        assert result["Serien"][0].tmdb_discover["with_watch_providers"] == [8]


class TestDynamic:
    def test_tmdb_collection_placeholder_and_instance(self, tmp_path: Path):
        parser = KometaParser(tmp_path)
        path = tmp_path / "c.yml"
        path.write_text(
            """
templates:
  collection:
    optional: [value]
    tmdb_collection_details: <<value>>
    sync_mode: sync
    minimum_items: 3
dynamic_collections:
  TMDb Collections:
    template: collection
    type: tmdb_collection
    remove_suffix: Collection
""",
            encoding="utf-8",
        )
        placeholder = parser.parse_collection_file(path)[0]
        assert placeholder.dynamic_type == "tmdb_collection"
        assert parser.dynamic_minimum(placeholder.dynamic_spec) == 3

        instance = parser.build_dynamic_instance(placeholder.dynamic_spec, 10, "Star Wars")
        assert instance.name == "Star Wars"
        assert instance.tmdb_collection == [10]


class TestRegressionsFromFirstRun:
    def test_template_without_schedule_runs_daily(self, tmp_path: Path):
        c = _parse(
            tmp_path,
            """
templates:
  Movies:
    optional: [trakt_list]
    trakt_list_details: <<trakt_list>>
collections:
  Ghibli:
    template: {name: Movies, trakt_list: "https://trakt.tv/users/a/lists/b?sort=rank,asc"}
""",
        )[0]
        assert c.schedule.schedule_type == ScheduleType.DAILY
        # `trakt_list_details` is the builder plus details
        assert c.trakt_lists == ["https://trakt.tv/users/a/lists/b?sort=rank,asc"]

    def test_imdb_alternative_lists(self):
        doc = build_default_collections(
            "imdb", MediaType.MOVIE,
            {"top_list": "https://trakt.tv/users/x/lists/top", "popular_list": "https://mdblist.com/lists/a/b"},
        )
        top, popular = doc["collections"].values()
        assert top["trakt_list"].startswith("https://trakt.tv") and "imdb_chart" not in top
        assert popular["mdblist_list"].startswith("https://mdblist.com")

    def test_tmdb_series_without_origin_country(self):
        from jfc.clients.tmdb import TMDbClient

        client = TMDbClient.__new__(TMDbClient)
        series = client._parse_series({"id": 1, "name": "X", "origin_country": [], "genre_ids": []})
        assert series.original_country is None
