"""Trakt OAuth authentication service."""

import asyncio
import base64
import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from loguru import logger
from pydantic import BaseModel


class TraktTokens(BaseModel):
    """Trakt OAuth tokens."""

    access_token: str
    refresh_token: str
    expires_at: datetime
    created_at: datetime
    token_type: str = "Bearer"

    def is_expired(self) -> bool:
        """Check if access token is expired (with 1 hour buffer)."""
        return datetime.now() >= (self.expires_at - timedelta(hours=1))

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "token_type": self.token_type,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TraktTokens":
        """Create from dict."""
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=datetime.fromisoformat(data["expires_at"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            token_type=data.get("token_type", "Bearer"),
        )


# Trakt developer apps only accept localhost redirect URIs; register this one (or set
# TRAKT_REDIRECT_URI to the one you registered).
DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"


def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for a PKCE sign-in (S256)."""
    verifier = secrets.token_urlsafe(64)  # 86 chars from [A-Za-z0-9_-]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def extract_authorization_code(user_input: str) -> Optional[str]:
    """The authorization code from a pasted code or from the full redirect URL (`?code=...`)."""
    text = user_input.strip()
    if not text:
        return None
    if "://" in text or "?" in text:
        query = parse_qs(urlparse(text).query)
        codes = query.get("code")
        return codes[0] if codes else None
    return text


def is_local_redirect(redirect_uri: str) -> bool:
    """Whether the redirect URI points to this machine (JFC can listen for the callback)."""
    parsed = urlparse(redirect_uri)
    return parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1") and bool(parsed.port)


async def wait_for_local_callback(
    redirect_uri: str,
    expected_state: Optional[str] = None,
    timeout: float = 300.0,
) -> Optional[str]:
    """
    Listen on a localhost redirect URI until the browser delivers `?code=...`.

    Returns the authorization code, or None on timeout / error / state mismatch.
    Raises OSError if the port cannot be bound.
    """
    parsed = urlparse(redirect_uri)
    result: asyncio.Future = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        body = "You can close this window and return to the terminal."
        status = "200 OK"
        try:
            request_line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            parts = request_line.split()
            target = urlparse(parts[1]) if len(parts) >= 2 else None
            if not target or target.path != (parsed.path or "/"):
                status, body = "404 Not Found", "Not found"
            else:
                query = parse_qs(target.query)
                code = (query.get("code") or [None])[0]
                state = (query.get("state") or [None])[0]
                if query.get("error"):
                    status, body = "400 Bad Request", f"Trakt returned an error: {query['error'][0]}"
                    if not result.done():
                        result.set_result(None)
                elif code and (expected_state is None or state == expected_state):
                    if not result.done():
                        result.set_result(code)
                else:
                    status, body = "400 Bad Request", "Missing code or state mismatch"
        except Exception:
            status, body = "400 Bad Request", "Bad request"
        finally:
            payload = body.encode()
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload
            )
            try:
                await writer.drain()
            finally:
                writer.close()

    server = await asyncio.start_server(handle, host=parsed.hostname, port=parsed.port)
    try:
        return await asyncio.wait_for(result, timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        server.close()
        await server.wait_closed()


class TraktAuth:
    """Trakt OAuth authentication handler.

    Newer Trakt apps get no client secret and sign users in with PKCE; an empty
    `client_secret` therefore means "PKCE app": the secret is omitted from all requests.
    """

    OAUTH_URL = "https://api.trakt.tv/oauth"
    AUTHORIZE_URL = "https://auth.trakt.tv/oauth/authorize"
    TOKEN_FILE = "trakt_tokens.json"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        data_dir: Path,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
    ):
        """
        Initialize Trakt auth handler.

        Args:
            client_id: Trakt application client ID
            client_secret: Trakt application client secret (empty for PKCE apps)
            data_dir: Directory to store token file
            redirect_uri: Redirect URI registered for the app (PKCE sign-in / token refresh)
        """
        self.client_id = client_id
        self.client_secret = client_secret or ""
        self.redirect_uri = redirect_uri or DEFAULT_REDIRECT_URI
        self.data_dir = data_dir
        self.pkce_state: Optional[str] = None
        self.token_path = data_dir / self.TOKEN_FILE
        self._tokens: Optional[TraktTokens] = None

    @property
    def has_client_secret(self) -> bool:
        return bool(self.client_secret)

    def _client_credentials(self) -> dict:
        """client_id plus client_secret when the app has one."""
        credentials = {"client_id": self.client_id}
        if self.client_secret:
            credentials["client_secret"] = self.client_secret
        return credentials

    def build_pkce_authorization(self) -> tuple[str, str]:
        """Return (authorize_url, code_verifier). Keep the verifier for `pkce_exchange`."""
        verifier, challenge = generate_pkce()
        self.pkce_state = secrets.token_urlsafe(16)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": self.pkce_state,
        }
        return f"{self.AUTHORIZE_URL}?{urlencode(params)}", verifier

    async def pkce_exchange(self, code: str, code_verifier: str) -> Optional[TraktTokens]:
        """Exchange the authorization code for tokens (no client secret)."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.OAUTH_URL}/token",
                json={
                    **self._client_credentials(),
                    "code": code,
                    "code_verifier": code_verifier,
                    "redirect_uri": self.redirect_uri,
                    "grant_type": "authorization_code",
                },
            )

        if response.status_code != 200:
            logger.error(f"Trakt token exchange failed ({response.status_code}): {response.text}")
            return None

        tokens = self._tokens_from_response(response.json())
        self.save_tokens(tokens)
        logger.info("Trakt authentication successful!")
        return tokens

    @staticmethod
    def _tokens_from_response(token_data: dict) -> TraktTokens:
        return TraktTokens(
            access_token=token_data["access_token"],
            refresh_token=token_data["refresh_token"],
            expires_at=datetime.now() + timedelta(seconds=token_data["expires_in"]),
            created_at=datetime.now(),
        )

    def load_tokens(self) -> Optional[TraktTokens]:
        """Load tokens from file."""
        if not self.token_path.exists():
            logger.debug("No Trakt token file found")
            return None

        try:
            with open(self.token_path, encoding="utf-8") as f:
                data = json.load(f)
            self._tokens = TraktTokens.from_dict(data)
            logger.debug(f"Loaded Trakt tokens (expires: {self._tokens.expires_at})")
            return self._tokens
        except Exception as e:
            logger.warning(f"Failed to load Trakt tokens: {e}")
            return None

    def save_tokens(self, tokens: TraktTokens) -> None:
        """Save tokens to file."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with open(self.token_path, "w", encoding="utf-8") as f:
            json.dump(tokens.to_dict(), f, indent=2)
        self._tokens = tokens
        logger.info(f"Saved Trakt tokens (expires: {tokens.expires_at})")

    def delete_tokens(self) -> None:
        """Delete token file."""
        if self.token_path.exists():
            self.token_path.unlink()
            self._tokens = None
            logger.info("Deleted Trakt tokens")

    async def get_valid_token(self) -> Optional[str]:
        """Get a valid access token, refreshing if needed."""
        tokens = self._tokens or self.load_tokens()

        if not tokens:
            return None

        if tokens.is_expired():
            logger.info("Trakt access token expired, refreshing...")
            tokens = await self.refresh_tokens(tokens.refresh_token)
            if not tokens:
                return None

        return tokens.access_token

    async def device_code_flow(
        self,
        on_code_received: callable = None,
    ) -> Optional[TraktTokens]:
        """
        Perform OAuth Device Code flow.

        Args:
            on_code_received: Callback when user code is received.
                              Receives (user_code, verification_url, expires_in)

        Returns:
            TraktTokens if successful, None otherwise
        """
        async with httpx.AsyncClient() as client:
            # Step 1: Get device code
            logger.info("Requesting Trakt device code...")
            response = await client.post(
                f"{self.OAUTH_URL}/device/code",
                json={"client_id": self.client_id},
            )

            if response.status_code != 200:
                logger.error(f"Failed to get device code: {response.text}")
                if not self.has_client_secret:
                    logger.error("Apps without a client secret must sign in with PKCE (jfc trakt-auth)")
                return None

            data = response.json()
            device_code = data["device_code"]
            user_code = data["user_code"]
            verification_url = data["verification_url"]
            expires_in = data["expires_in"]
            interval = data["interval"]

            # Notify caller about the code
            if on_code_received:
                on_code_received(user_code, verification_url, expires_in)
            else:
                logger.info(f"Go to: {verification_url}")
                logger.info(f"Enter code: {user_code}")

            # Step 2: Poll for authorization
            start_time = time.time()
            while (time.time() - start_time) < expires_in:
                await asyncio.sleep(interval)

                token_response = await client.post(
                    f"{self.OAUTH_URL}/device/token",
                    json={"code": device_code, **self._client_credentials()},
                )

                if token_response.status_code == 200:
                    # Success!
                    token_data = token_response.json()
                    tokens = TraktTokens(
                        access_token=token_data["access_token"],
                        refresh_token=token_data["refresh_token"],
                        expires_at=datetime.now() + timedelta(seconds=token_data["expires_in"]),
                        created_at=datetime.now(),
                    )
                    self.save_tokens(tokens)
                    logger.info("Trakt authentication successful!")
                    return tokens

                elif token_response.status_code == 400:
                    # Handle empty response body
                    try:
                        error_data = token_response.json()
                        error = error_data.get("error", "unknown")
                    except Exception:
                        # Empty body - likely authorization_pending
                        error = "authorization_pending"

                    if error == "authorization_pending":
                        # Still waiting for user
                        continue
                    elif error == "slow_down":
                        interval += 1
                        continue
                    else:
                        logger.error(f"Authorization failed: {error}")
                        return None

                elif token_response.status_code == 410:
                    logger.error("Device code expired")
                    return None

            logger.error("Authorization timed out")
            return None

    async def refresh_tokens(self, refresh_token: str) -> Optional[TraktTokens]:
        """
        Refresh access token using refresh token.

        Args:
            refresh_token: The refresh token

        Returns:
            New TraktTokens if successful, None otherwise
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.OAUTH_URL}/token",
                json={
                    "refresh_token": refresh_token,
                    **self._client_credentials(),
                    "redirect_uri": self.redirect_uri,
                    "grant_type": "refresh_token",
                },
            )

            if response.status_code != 200:
                logger.error(f"Failed to refresh Trakt token: {response.text}")
                # Delete invalid tokens
                self.delete_tokens()
                return None

            token_data = response.json()
            tokens = TraktTokens(
                access_token=token_data["access_token"],
                refresh_token=token_data["refresh_token"],
                expires_at=datetime.now() + timedelta(seconds=token_data["expires_in"]),
                created_at=datetime.now(),
            )
            self.save_tokens(tokens)
            logger.info("Trakt tokens refreshed successfully")
            return tokens

    async def revoke_token(self) -> bool:
        """Revoke current access token."""
        tokens = self._tokens or self.load_tokens()
        if not tokens:
            return True

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.OAUTH_URL}/revoke",
                json={"token": tokens.access_token, **self._client_credentials()},
            )

            if response.status_code == 200:
                self.delete_tokens()
                logger.info("Trakt token revoked")
                return True
            else:
                logger.warning(f"Failed to revoke token: {response.text}")
                return False
