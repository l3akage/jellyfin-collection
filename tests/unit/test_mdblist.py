"""MDBList client parsing."""

import httpx
import pytest

from jfc.clients.mdblist import MDBListClient
from jfc.models.media import MediaType

PAYLOAD = [
    {"id": 278, "title": "The Shawshank Redemption", "mediatype": "movie", "imdb_id": "tt0111161", "tmdb_id": None, "release_year": 1994},
    {"id": 1396, "title": "Breaking Bad", "mediatype": "show", "imdb_id": "tt0903747", "tmdb_id": None, "tvdb_id": None, "release_year": 2008},
]


def test_json_path_variants():
    assert MDBListClient._json_path("https://mdblist.com/lists/snoak/imdb-top-250-movies") == "/lists/snoak/imdb-top-250-movies/json"
    assert MDBListClient._json_path("https://mdblist.com/lists/snoak/x/?page=1") == "/lists/snoak/x/json"
    assert MDBListClient._json_path("https://example.org/foo") is None


@pytest.mark.asyncio
async def test_get_list_uses_id_as_tmdb_id_and_filters_type(monkeypatch):
    client = MDBListClient(api_key="k")

    async def fake_get(path, params=None, **kwargs):
        assert path == "/lists/snoak/top/json" and params == {"apikey": "k"}
        return httpx.Response(200, json=PAYLOAD, request=httpx.Request("GET", "https://mdblist.com" + path))

    monkeypatch.setattr(client, "get", fake_get)
    movies = await client.get_list("https://mdblist.com/lists/snoak/top", MediaType.MOVIE)
    shows = await client.get_list("https://mdblist.com/lists/snoak/top", MediaType.SERIES)
    assert [(m.title, m.tmdb_id, m.imdb_id) for m in movies] == [("The Shawshank Redemption", 278, "tt0111161")]
    assert [(s.title, s.tmdb_id) for s in shows] == [("Breaking Bad", 1396)]
    assert shows[0].media_type == MediaType.SERIES
