"""Tests for long-running daemon auth handling (Jellyfin header, Trakt token, startup retry)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jfc.clients.jellyfin import JellyfinClient
from jfc.services.runner import Runner
from jfc.services.startup import StartupService


class _StopRun(Exception):
    """Raised by the fake startup to stop run() right after Trakt init."""


class _FakeTraktAuth:
    def __init__(self, token: str | None):
        self.token = token

    async def get_valid_token(self) -> str | None:
        return self.token


def _make_runner(trakt_auth: _FakeTraktAuth) -> Runner:
    runner = Runner.__new__(Runner)
    runner.trakt = None
    runner.trakt_auth = trakt_auth
    runner.builder = SimpleNamespace(trakt=None, matcher=None)
    runner.settings = SimpleNamespace(
        trakt=SimpleNamespace(client_id="id", client_secret="secret"),
        openai=SimpleNamespace(force_regenerate=False),
    )
    runner._startup_done = False
    runner.startup = SimpleNamespace(run_startup=AsyncMock(side_effect=_StopRun))
    return runner


async def _run_until_startup(runner: Runner) -> None:
    with pytest.raises(_StopRun):
        await runner.run()


def test_jellyfin_uses_mediabrowser_authorization_header() -> None:
    """Jellyfin 12+ rejects X-Emby-Token; the Authorization header must be used."""
    client = JellyfinClient(url="http://jellyfin:8096", api_key="abc123")

    assert client.headers["Authorization"] == 'MediaBrowser Token="abc123"'
    assert "X-Emby-Token" not in client.headers


@pytest.mark.asyncio
async def test_trakt_client_rebuilt_when_token_refreshed() -> None:
    """A refreshed Trakt token must replace the client held by the daemon."""
    auth = _FakeTraktAuth("token-a")
    runner = _make_runner(auth)

    await _run_until_startup(runner)
    first = runner.trakt
    assert first.access_token == "token-a"

    # Same token: client is reused
    await _run_until_startup(runner)
    assert runner.trakt is first

    # Token refreshed: client is rebuilt and builder updated
    auth.token = "token-b"
    await _run_until_startup(runner)
    assert runner.trakt is not first
    assert runner.trakt.access_token == "token-b"
    assert runner.builder.trakt is runner.trakt


@pytest.mark.asyncio
async def test_trakt_client_dropped_when_token_lost() -> None:
    """If the token can no longer be refreshed, the stale client is dropped."""
    auth = _FakeTraktAuth("token-a")
    runner = _make_runner(auth)
    await _run_until_startup(runner)

    auth.token = None
    await _run_until_startup(runner)

    assert runner.trakt is None
    assert runner.builder.trakt is None


@pytest.mark.asyncio
async def test_startup_failure_is_retried_on_next_run() -> None:
    """A failed startup must not be marked as done."""
    runner = _make_runner(_FakeTraktAuth(None))
    runner.startup = SimpleNamespace(run_startup=AsyncMock(return_value=False))

    with pytest.raises(RuntimeError):
        await runner.run()

    assert runner._startup_done is False


@pytest.mark.asyncio
async def test_startup_retries_until_services_available(monkeypatch) -> None:
    """Startup should retry connection checks when required services are down."""
    sleep = AsyncMock()
    monkeypatch.setattr("jfc.services.startup.asyncio.sleep", sleep)

    service = StartupService.__new__(StartupService)
    service.radarr = None
    service.sonarr = None
    service.print_banner = lambda: None
    service.check_connections = AsyncMock(
        side_effect=[
            {"Jellyfin": False, "TMDb": True},
            {"Jellyfin": True, "TMDb": True},
        ]
    )

    assert await service.run_startup() is True
    assert service.check_connections.await_count == 2
    sleep.assert_awaited_once_with(StartupService.STARTUP_RETRY_DELAY)


@pytest.mark.asyncio
async def test_startup_gives_up_after_max_attempts(monkeypatch) -> None:
    """Startup should fail after exhausting all attempts."""
    monkeypatch.setattr("jfc.services.startup.asyncio.sleep", AsyncMock())

    service = StartupService.__new__(StartupService)
    service.print_banner = lambda: None
    service.check_connections = AsyncMock(return_value={"Jellyfin": False, "TMDb": True})

    assert await service.run_startup() is False
    assert service.check_connections.await_count == StartupService.STARTUP_MAX_ATTEMPTS


class TestTraktPkce:
    def test_pkce_pair_is_valid_s256(self):
        import base64
        import hashlib

        from jfc.services.trakt_auth import generate_pkce

        verifier, challenge = generate_pkce()
        assert 43 <= len(verifier) <= 128
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        assert challenge == expected
        assert "=" not in challenge

    def test_authorize_url(self, tmp_path):
        from urllib.parse import parse_qs, urlparse

        from jfc.services.trakt_auth import TraktAuth

        auth = TraktAuth("cid", "", tmp_path, redirect_uri="https://example.org/cb")
        url, verifier = auth.build_pkce_authorization()
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://auth.trakt.tv/oauth/authorize"
        assert query["client_id"] == ["cid"] and query["code_challenge_method"] == ["S256"]
        assert query["redirect_uri"] == ["https://example.org/cb"] and query["response_type"] == ["code"]
        assert verifier and verifier not in url

    def test_extract_code(self):
        from jfc.services.trakt_auth import extract_authorization_code

        assert extract_authorization_code("abc123") == "abc123"
        assert extract_authorization_code("https://example.org/cb?code=xyz&state=1") == "xyz"
        assert extract_authorization_code("https://example.org/cb?error=denied") is None
        assert extract_authorization_code("  ") is None

    def test_secret_only_sent_when_set(self, tmp_path):
        from jfc.services.trakt_auth import TraktAuth

        assert TraktAuth("cid", "", tmp_path)._client_credentials() == {"client_id": "cid"}
        assert not TraktAuth("cid", "", tmp_path).has_client_secret
        assert TraktAuth("cid", "s", tmp_path)._client_credentials() == {"client_id": "cid", "client_secret": "s"}

    @pytest.mark.asyncio
    async def test_exchange_and_refresh_requests_have_no_secret(self, tmp_path, monkeypatch):
        import httpx

        from jfc.services.trakt_auth import TraktAuth

        sent = []

        async def fake_post(self, url, json=None, **kwargs):
            sent.append(json)
            return httpx.Response(
                200,
                json={"access_token": "a", "refresh_token": "r", "expires_in": 7200},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        auth = TraktAuth("cid", "", tmp_path)
        tokens = await auth.pkce_exchange("code1", "verifier1")
        assert tokens and tokens.access_token == "a"
        await auth.refresh_tokens("r")

        exchange, refresh = sent
        assert exchange["grant_type"] == "authorization_code" and exchange["code_verifier"] == "verifier1"
        assert refresh["grant_type"] == "refresh_token" and refresh["redirect_uri"]
        assert all("client_secret" not in body for body in sent)
