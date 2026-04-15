"""Async client for Yandex Messenger Bot API.

Base URL: https://botapi.messenger.yandex.net/bot/v1/
Auth: Authorization: OAuth <token>
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://botapi.messenger.yandex.net/bot/v1"
_MAX_RETRIES = 3
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class YandexMessengerClient:
    """Low-level async wrapper around the Yandex Messenger Bot API."""

    def __init__(self, token: str, base_url: str = _DEFAULT_BASE) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"OAuth {token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Low-level request with retry
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        data: dict | None = None,
        files: Any = None,
    ) -> dict:
        url = f"/{path.lstrip('/')}"
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                if files is not None:
                    # multipart upload — drop JSON content-type
                    headers = {"Authorization": f"OAuth {self.token}"}
                    resp = await self._http.request(
                        method, url, data=data, files=files, headers=headers
                    )
                else:
                    resp = await self._http.request(
                        method, url, json=json, params=params
                    )

                if resp.status_code in _RETRY_STATUSES:
                    retry_after = int(resp.headers.get("Retry-After", attempt * 2))
                    logger.warning(
                        "Yandex API %s %s returned %d, retry in %ds (attempt %d/%d)",
                        method, url, resp.status_code, retry_after, attempt, _MAX_RETRIES,
                    )
                    await asyncio.sleep(retry_after)
                    continue

                resp.raise_for_status()
                return resp.json()

            except httpx.HTTPStatusError:
                raise
            except httpx.HTTPError as exc:
                if attempt == _MAX_RETRIES:
                    raise
                wait = attempt * 2
                logger.warning("HTTP error %s, retry in %ds: %s", type(exc).__name__, wait, exc)
                await asyncio.sleep(wait)

        return {"ok": False, "description": "max retries exceeded"}

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def send_text(
        self,
        *,
        text: str,
        chat_id: str | None = None,
        login: str | None = None,
        reply_message_id: int | None = None,
        disable_notification: bool = False,
        disable_web_page_preview: bool = False,
        thread_id: int | None = None,
        suggest_buttons: dict | None = None,
    ) -> dict:
        body: dict[str, Any] = {"text": text}
        if chat_id:
            body["chat_id"] = chat_id
        if login:
            body["login"] = login
        if reply_message_id is not None:
            body["reply_message_id"] = reply_message_id
        if disable_notification:
            body["disable_notification"] = True
        if disable_web_page_preview:
            body["disable_web_page_preview"] = True
        if thread_id is not None:
            body["thread_id"] = thread_id
        if suggest_buttons is not None:
            body["suggest_buttons"] = suggest_buttons
        return await self._request("POST", "/messages/sendText/", json=body)

    async def send_image(
        self,
        *,
        image: bytes,
        filename: str = "image.jpg",
        content_type: str = "image/jpeg",
        chat_id: str | None = None,
        login: str | None = None,
        thread_id: int | None = None,
        suggest_buttons: dict | None = None,
    ) -> dict:
        data: dict[str, Any] = {}
        if chat_id:
            data["chat_id"] = chat_id
        if login:
            data["login"] = login
        if thread_id is not None:
            data["thread_id"] = str(thread_id)
        if suggest_buttons is not None:
            import json as _json
            data["suggest_buttons"] = _json.dumps(suggest_buttons)
        files = {"image": (filename, image, content_type)}
        return await self._request("POST", "/messages/sendImage/", data=data, files=files)

    async def send_file(
        self,
        *,
        document: bytes,
        filename: str = "file.bin",
        content_type: str = "application/octet-stream",
        chat_id: str | None = None,
        login: str | None = None,
        thread_id: int | None = None,
    ) -> dict:
        data: dict[str, Any] = {}
        if chat_id:
            data["chat_id"] = chat_id
        if login:
            data["login"] = login
        if thread_id is not None:
            data["thread_id"] = str(thread_id)
        files = {"document": (filename, document, content_type)}
        return await self._request("POST", "/messages/sendFile/", data=data, files=files)

    async def delete_message(
        self,
        *,
        message_id: int,
        chat_id: str | None = None,
        login: str | None = None,
        thread_id: int | None = None,
    ) -> dict:
        body: dict[str, Any] = {"message_id": message_id}
        if chat_id:
            body["chat_id"] = chat_id
        if login:
            body["login"] = login
        if thread_id is not None:
            body["thread_id"] = thread_id
        return await self._request("POST", "/messages/delete/", json=body)

    async def get_file(self, file_id: str) -> bytes:
        resp = await self._http.get(
            "/messages/getFile/",
            params={"file_id": file_id},
        )
        resp.raise_for_status()
        return resp.content

    # ------------------------------------------------------------------
    # Updates (polling)
    # ------------------------------------------------------------------

    async def get_updates(self, offset: int = 0, limit: int = 100) -> dict:
        return await self._request(
            "GET",
            "/messages/getUpdates/",
            params={"offset": offset, "limit": limit},
        )

    # ------------------------------------------------------------------
    # Webhook
    # ------------------------------------------------------------------

    async def set_webhook(self, webhook_url: str | None) -> dict:
        return await self._request(
            "POST", "/self/update/", json={"webhook_url": webhook_url}
        )

    # ------------------------------------------------------------------
    # Chats
    # ------------------------------------------------------------------

    async def create_chat(
        self,
        *,
        name: str,
        description: str = "",
        admins: list[dict] | None = None,
        members: list[dict] | None = None,
        channel: bool = False,
    ) -> dict:
        body: dict[str, Any] = {"name": name, "description": description}
        if admins:
            body["admins"] = admins
        if members:
            body["members"] = members
        if channel:
            body["channel"] = True
        return await self._request("POST", "/chats/create/", json=body)

    async def update_members(
        self,
        chat_id: str,
        *,
        members: list[dict] | None = None,
        admins: list[dict] | None = None,
        remove: list[dict] | None = None,
    ) -> dict:
        body: dict[str, Any] = {"chat_id": chat_id}
        if members:
            body["members"] = members
        if admins:
            body["admins"] = admins
        if remove:
            body["remove"] = remove
        return await self._request("POST", "/chats/updateMembers/", json=body)

    # ------------------------------------------------------------------
    # Polls
    # ------------------------------------------------------------------

    async def create_poll(
        self,
        *,
        title: str,
        answers: list[str],
        chat_id: str | None = None,
        login: str | None = None,
        max_choices: int = 1,
        is_anonymous: bool = False,
    ) -> dict:
        body: dict[str, Any] = {"title": title, "answers": answers}
        if chat_id:
            body["chat_id"] = chat_id
        if login:
            body["login"] = login
        body["max_choices"] = max_choices
        body["is_anonymous"] = is_anonymous
        return await self._request("POST", "/messages/createPoll/", json=body)

    async def get_poll_results(
        self, *, chat_id: str, message_id: int
    ) -> dict:
        return await self._request(
            "GET",
            "/polls/getResults/",
            params={"chat_id": chat_id, "message_id": message_id},
        )

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def get_user_link(self, login: str) -> dict:
        return await self._request(
            "GET", "/users/getUserLink", params={"login": login}
        )
