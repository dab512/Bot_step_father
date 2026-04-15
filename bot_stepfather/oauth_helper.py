"""OAuth helper for acquiring Yandex OAuth tokens.

Supports multiple grant types for different scenarios:
  - password: For programmatically created user-bots (username + password → token)
  - device_code: For interactive flows where a user authorizes via browser
  - authorization_code: Standard web OAuth flow
  - refresh_token: Token renewal

The key scope for Messenger Client API access is `yamb:all`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth.yandex.ru/token"
DEVICE_CODE_URL = "https://oauth.yandex.ru/device/code"
AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"

MESSENGER_SCOPE = "yamb:all"


@dataclass
class OAuthToken:
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 0
    refresh_token: str = ""
    scope: str = ""
    obtained_at: float = 0.0

    @property
    def is_expired(self) -> bool:
        if self.expires_in <= 0:
            return False  # no expiration info — assume valid
        return time.time() > (self.obtained_at + self.expires_in - 60)


@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_url: str
    interval: int = 5
    expires_in: int = 300


class OAuthError(Exception):
    def __init__(self, error: str, description: str = "") -> None:
        self.error = error
        self.description = description
        super().__init__(f"{error}: {description}" if description else error)


class YandexOAuth:
    """Async helper for Yandex OAuth token acquisition."""

    def __init__(self, client_id: str, client_secret: str = "") -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    async def close(self) -> None:
        await self._http.aclose()

    async def _token_request(self, **params: Any) -> OAuthToken:
        """Send a token request and parse the response."""
        data = {"client_id": self.client_id, **params}
        if self.client_secret:
            data["client_secret"] = self.client_secret

        resp = await self._http.post(
            TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        body = resp.json()

        if "error" in body:
            raise OAuthError(
                error=body["error"],
                description=body.get("error_description", ""),
            )

        return OAuthToken(
            access_token=body["access_token"],
            token_type=body.get("token_type", "bearer"),
            expires_in=body.get("expires_in", 0),
            refresh_token=body.get("refresh_token", ""),
            scope=body.get("scope", ""),
            obtained_at=time.time(),
        )

    # ------------------------------------------------------------------
    # Password Grant (for programmatic user-bots)
    # ------------------------------------------------------------------

    async def token_by_password(
        self,
        username: str,
        password: str,
        scope: str = MESSENGER_SCOPE,
    ) -> OAuthToken:
        """Obtain an OAuth token using Resource Owner Password Credentials.

        This is the simplest flow for user accounts created via api360:
        we know the username and password, so we can directly exchange
        them for an OAuth token.

        Args:
            username: Full email address (e.g., bot1@yourdomain.ru)
            password: The password set when creating the user via api360
            scope: OAuth scope (default: yamb:all for Messenger)
        """
        logger.info("Requesting OAuth token via password grant for %s", username)
        return await self._token_request(
            grant_type="password",
            username=username,
            password=password,
            scope=scope,
        )

    # ------------------------------------------------------------------
    # Device Code Flow (for interactive authorization)
    # ------------------------------------------------------------------

    async def start_device_flow(
        self, scope: str = MESSENGER_SCOPE
    ) -> DeviceCode:
        """Start a device code flow. Returns codes for user to authorize."""
        resp = await self._http.post(
            DEVICE_CODE_URL,
            data={
                "client_id": self.client_id,
                "scope": scope,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        body = resp.json()

        if "error" in body:
            raise OAuthError(body["error"], body.get("error_description", ""))

        return DeviceCode(
            device_code=body["device_code"],
            user_code=body["user_code"],
            verification_url=body.get("verification_url", "https://ya.ru/device"),
            interval=body.get("interval", 5),
            expires_in=body.get("expires_in", 300),
        )

    async def poll_device_token(self, device_code: DeviceCode) -> OAuthToken:
        """Poll until the user completes authorization or timeout."""
        deadline = time.time() + device_code.expires_in

        while time.time() < deadline:
            try:
                return await self._token_request(
                    grant_type="device_code",
                    code=device_code.device_code,
                )
            except OAuthError as e:
                if e.error == "authorization_pending":
                    await asyncio.sleep(device_code.interval)
                    continue
                elif e.error == "slow_down":
                    await asyncio.sleep(device_code.interval + 5)
                    continue
                raise

        raise OAuthError("timeout", "Device code authorization timed out")

    # ------------------------------------------------------------------
    # Refresh Token
    # ------------------------------------------------------------------

    async def refresh(self, refresh_token: str) -> OAuthToken:
        """Refresh an expired token."""
        return await self._token_request(
            grant_type="refresh_token",
            refresh_token=refresh_token,
        )

    # ------------------------------------------------------------------
    # Authorization Code Flow
    # ------------------------------------------------------------------

    def get_authorize_url(
        self,
        redirect_uri: str,
        scope: str = MESSENGER_SCOPE,
        state: str = "",
    ) -> str:
        """Generate the authorization URL for web-based OAuth flow."""
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "scope": scope,
            "redirect_uri": redirect_uri,
        }
        if state:
            params["state"] = state
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{AUTHORIZE_URL}?{query}"

    async def token_by_code(
        self, code: str, redirect_uri: str
    ) -> OAuthToken:
        """Exchange an authorization code for an OAuth token."""
        return await self._token_request(
            grant_type="authorization_code",
            code=code,
            redirect_uri=redirect_uri,
        )
