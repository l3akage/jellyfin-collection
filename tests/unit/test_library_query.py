"""Tests for the Kometa-style library filter engine."""

from datetime import date, datetime, timedelta, timezone

from jfc.clients.jellyfin import apply_detail_fields, classify_resolution, normalize_language, parse_edition
from jfc.models.media import LibraryItem, MediaType
from jfc.services.library_query import LibraryQuery


def item(**kw) -> LibraryItem:
    base = dict(
        jellyfin_id="1", title="Movie", media_type=MediaType.MOVIE, library_id="lib", library_name="Filme"
    )
    base.update(kw)
    return LibraryItem(**base)


NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def test_added_within_days_and_label_not():
    recent = item(date_added=NOW - timedelta(days=3), tags=["Foo"])
    old = item(date_added=NOW - timedelta(days=30))
    soon = item(date_added=NOW - timedelta(days=1), tags=["Coming Soon"])
    q = LibraryQuery()
    filters = {"added": 7, "label.not": "Coming Soon, RequestNeeded"}
    assert q.matches(recent, filters, now=NOW)
    assert not q.matches(old, filters, now=NOW)
    assert not q.matches(soon, filters, now=NOW)


def test_resolution_and_dolby_vision():
    q = LibraryQuery()
    uhd_dv = item(resolution="4k", dolby_vision=True)
    uhd = item(resolution="4k")
    hd = item(resolution="1080", dolby_vision=True)
    filters = {"has_dolby_vision": True, "resolution": "4k"}
    assert q.matches(uhd_dv, filters)
    assert not q.matches(uhd, filters)
    assert not q.matches(hd, filters)
    assert q.matches(uhd, {"resolution": "2160"})


def test_edition_any_case_insensitive():
    q = LibraryQuery()
    search = {"any": {"edition": ["Imax", "Extended Imax", "Imax-E"]}}
    assert q.matches(item(edition="IMAX"), search)
    assert q.matches(item(edition="Imax-E"), search)
    assert not q.matches(item(edition="Director's Cut"), search)
    assert not q.matches(item(), search)


def test_audio_language_combinations():
    q = LibraryQuery()
    de_en = item(audio_languages=["de", "en"])
    de = item(audio_languages=["de"])
    en = item(audio_languages=["en"])
    ger_only = {"audio_language": "de", "audio_language.not": "en"}
    assert q.matches(de, ger_only) and not q.matches(de_en, ger_only)
    assert q.matches(de_en, {"audio_language": ["de", "en"]})
    assert q.matches(en, {"audio_language": "en", "audio_language.not": "de"})


def test_genre_year_and_numeric_modifiers():
    q = LibraryQuery()
    movie = item(genres=["Komödie", "Drama"], year=2018, community_rating=7.4)
    assert q.matches(movie, {"all": {"genre": "Komödie", "year.gte": 2012}})
    assert not q.matches(movie, {"all": {"year.lte": 2010}})
    assert q.matches(movie, {"audience_rating.gte": 7.0})
    assert not q.matches(movie, {"audience_rating.gte": 8.0})


def test_groups_not_and_any():
    q = LibraryQuery()
    movie = item(genres=["Horror"], year=2000)
    assert q.matches(movie, {"any": [{"genre": "Comedy"}, {"year": 2000}]})
    assert not q.matches(movie, {"not": {"genre": "Horror"}})
    assert q.matches(movie, {"all": [{"genre": "Horror"}, {"year.lt": 2010}]})


def test_tmdb_status_lookup():
    q = LibraryQuery()
    q.set_tmdb_statuses({1: "Returning Series", 2: "Ended", 3: "Canceled"})
    assert q.matches(item(tmdb_id=1), {"tmdb_status": "returning"})
    assert q.matches(item(tmdb_id=2), {"tmdb_status": "ended"})
    assert q.matches(item(tmdb_id=3), {"tmdb_status": "canceled"})
    assert not q.matches(item(tmdb_id=2), {"tmdb_status": "returning"})
    assert LibraryQuery.uses_attribute({"all": [{"tmdb_status": "ended"}]}, "tmdb_status")


def test_unknown_attribute_is_ignored():
    assert LibraryQuery().matches(item(), {"some_plex_only_thing": "x"})


def test_sort_and_limit_keys_do_not_filter():
    q = LibraryQuery()
    assert q.matches(item(year=2000), {"all": {"year.gte": 1}, "limit": 5, "sort_by": "title.asc"})
    ordered = LibraryQuery.sort([item(title="b"), item(title="a")], "title.asc")
    assert [i.title for i in ordered] == ["a", "b"]


class TestJellyfinHelpers:
    def test_classify_resolution(self):
        assert classify_resolution(3840, 2160) == "4k"
        assert classify_resolution(3840, 1600) == "4k"
        assert classify_resolution(1920, 1080) == "1080"
        assert classify_resolution(1920, 800) == "1080"
        assert classify_resolution(1280, 720) == "720"
        assert classify_resolution(720, 480) == "480"

    def test_normalize_language(self):
        assert normalize_language("ger") == "de"
        assert normalize_language("deu") == "de"
        assert normalize_language("eng") == "en"
        assert normalize_language(None) is None

    def test_parse_edition(self):
        assert parse_edition("/m/Film (2018) {edition-IMAX}.mkv") == "IMAX"
        assert parse_edition("/m/Film (2018) {edition-Director's Cut}/Film.mkv") == "Director's Cut"
        assert parse_edition("/m/Film (2018).mkv") is None

    def test_apply_detail_fields(self):
        target = item(path="/m/Film {edition-Extended Edition}.mkv")
        apply_detail_fields(
            target,
            {
                "DateCreated": "2026-10-01T10:00:00.0000000Z",
                "PremiereDate": "2018-07-25T00:00:00.0000000Z",
                "Tags": ["Coming Soon"],
                "Studios": [{"Name": "Paramount"}],
                "CommunityRating": 7.4,
                "RunTimeTicks": 8_880_000_000_0,
                "MediaStreams": [
                    {"Type": "Video", "Width": 3840, "Height": 2160, "VideoRange": "HDR", "VideoRangeType": "DOVIWithHDR10"},
                    {"Type": "Audio", "Language": "ger"},
                    {"Type": "Audio", "Language": "eng"},
                    {"Type": "Subtitle", "Language": "ger"},
                ],
            },
        )
        assert target.resolution == "4k" and target.dolby_vision and target.hdr
        assert target.audio_languages == ["de", "en"] and target.subtitle_languages == ["de"]
        assert target.edition == "Extended Edition"
        assert target.tags == ["Coming Soon"] and target.studios == ["Paramount"]
        assert target.premiere_date == date(2018, 7, 25)
        assert target.runtime_minutes == 148
