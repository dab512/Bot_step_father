"""Async client for the Yandex Messenger Client API (api.messenger.yandex.ru).

This is the UNDOCUMENTED API used by the Yandex Messenger web client
and mobile apps. It uses a JSON-RPC-style protocol over HTTPS POST.

Unlike the official Bot API (botapi.messenger.yandex.net), this API:
  - Authenticates as a regular user (OAuth with yamb:all scope)
  - Can potentially communicate with users outside the organization
  - Has a different set of methods and data formats

Request format:
    POST https://api.messenger.yandex.ru/api/
    Authorization: OAuth <token>
    Content-Type: application/json
    x-version: 2

    {"method": "<method_name>", "params": {...}}

Response format:
    {"status": "ok", "data": {...}}
    {"status": "error", "data": {"code": "...", "text": "...", "source": "yamb"}}
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://api.messenger.yandex.ru"
_MAX_RETRIES = 3


class MessengerClientApiError(Exception):
    """Error from the Messenger Client API."""

    def __init__(self, code: str, text: str = "", source: str = "") -> None:
        self.code = code
        self.text = text
        self.source = source
        super().__init__(f"[{source}] {code}: {text}" if source else f"{code}: {text}")


class MessengerClientApi:
    """Async client for the undocumented Yandex Messenger Client API.

    This client communicates as a regular user (not a bot), using an
    OAuth token with the yamb:all scope.
    """

    def __init__(self, oauth_token: str, base_url: str = _DEFAULT_BASE) -> None:
        self.oauth_token = oauth_token
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"OAuth {oauth_token}",
                "Content-Type": "application/json",
                "x-version": "2",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Make a JSON-RPC-style API call."""
        body: dict[str, Any] = {"method": method}
        if params:
            body["params"] = params

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = await self._http.post("/api/", json=body)

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", attempt * 2))
                    logger.warning("Rate limited on %s, retry in %ds", method, retry_after)
                    await asyncio.sleep(retry_after)
                    continue

                if resp.status_code >= 500:
                    if attempt < _MAX_RETRIES:
                        await asyncio.sleep(attempt * 2)
                        continue
                    resp.raise_for_status()

                # Handle empty response bodies (some methods return 400/418 with no body)
                if not resp.content:
                    return {}

                result = resp.json()

                if result.get("status") == "error":
                    err_data = result.get("data", {})
                    raise MessengerClientApiError(
                        code=err_data.get("code", "unknown"),
                        text=err_data.get("text", ""),
                        source=err_data.get("source", ""),
                    )

                return result.get("data", result)

            except httpx.HTTPStatusError:
                raise
            except (httpx.HTTPError, MessengerClientApiError):
                if attempt == _MAX_RETRIES:
                    raise
                await asyncio.sleep(attempt * 2)

        return {}

    # ------------------------------------------------------------------
    # Bootstrap / Initialization
    # ------------------------------------------------------------------

    async def bootstrap(self) -> dict:
        """Get initial client data (organizations, settings, etc.)."""
        return await self._call("bootstrap")

    async def get_settings(self) -> dict:
        """Get user notification settings."""
        return await self._call("get_settings")

    async def get_organizations(self) -> dict:
        """List organizations the user belongs to."""
        return await self._call("get_organizations")

    # ------------------------------------------------------------------
    # Chats
    # ------------------------------------------------------------------

    async def get_chats(self, **params: Any) -> dict:
        """Get list of chats."""
        return await self._call("chats", params or None)

    async def create_chat(
        self,
        *,
        name: str | None = None,
        members: list[str] | None = None,
        description: str = "",
        **kwargs: Any,
    ) -> dict:
        """Create a new chat."""
        params: dict[str, Any] = {}
        if name:
            params["name"] = name
        if members:
            params["members"] = members
        if description:
            params["description"] = description
        params.update(kwargs)
        return await self._call("create_chat", params)

    async def get_groups(self) -> dict:
        """Get group chats list."""
        return await self._call("groups")

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        **kwargs: Any,
    ) -> dict:
        """Send a text message to a chat."""
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        params.update(kwargs)
        return await self._call("messages", params)

    async def get_messages(
        self,
        *,
        chat_id: str,
        **kwargs: Any,
    ) -> dict:
        """Get messages from a chat."""
        params: dict[str, Any] = {"chat_id": chat_id}
        params.update(kwargs)
        return await self._call("messages", params)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(self, query: str, **kwargs: Any) -> dict:
        """Search for users, chats, or messages."""
        params: dict[str, Any] = {"query": query}
        params.update(kwargs)
        return await self._call("search", params)

    # ------------------------------------------------------------------
    # Invites
    # ------------------------------------------------------------------

    async def invite(
        self,
        *,
        chat_id: str | None = None,
        invite: str | None = None,
        **kwargs: Any,
    ) -> dict:
        """Join a chat via invite link or add someone to a chat."""
        params: dict[str, Any] = {}
        if chat_id:
            params["chat_id"] = chat_id
        if invite:
            params["invite"] = invite
        params.update(kwargs)
        return await self._call("invite", params)

    # ------------------------------------------------------------------
    # Generic method call (for methods not yet wrapped)
    # ------------------------------------------------------------------

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call any API method by name. Use for undiscovered methods."""
        return await self._call(method, params)


class MessengerClientApiAdapter:
    """Adapts the Client API to present the same interface as YandexMessengerClient.

    This allows the TG2YM adapter to work with either Bot API or Client API
    depending on how the bot was provisioned.
    """

    def __init__(self, client: MessengerClientApi) -> None:
        self._client = client

    async def close(self) -> None:
        await self._client.close()

    async def send_text(
        self,
        *,
        text: str,
        chat_id: str | None = None,
        login: str | None = None,
        **kwargs: Any,
    ) -> dict:
        """Send text message via Client API."""
        target_chat = chat_id or login or ""
        result = await self._client.send_message(chat_id=target_chat, text=text)
        return {"ok": True, "message_id": result.get("message_id", 0)}

    async def get_updates(self, offset: int = 0, limit: int = 100) -> dict:
        """Get updates — mapped from Client API chats/messages calls."""
        # The Client API uses a different model for updates (likely WebSocket-based).
        # This is a compatibility shim that polls for new messages.
        # Full implementation requires WebSocket integration with uniproxy.messenger.yandex.ru
        return {"ok": True, "updates": []}

    async def set_webhook(self, webhook_url: str | None) -> dict:
        """Client API doesn't have webhooks — this is a no-op placeholder."""
        logger.info("Client API mode: webhook not natively supported, using polling")
        return {"ok": True}
