"""Async client for the Yandex 360 Admin API (api360.yandex.net).

Used by Bot StepFather to manage organization users, including
creating user accounts that will serve as bot accounts.

API docs: https://yandex.ru/dev/api360/doc/ru/
Auth: OAuth token with ya360_admin:directory_write scope
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://api360.yandex.net"
_MAX_RETRIES = 3


class Yandex360Client:
    """Async wrapper around the Yandex 360 Admin API."""

    def __init__(self, oauth_token: str, org_id: str) -> None:
        self.org_id = org_id
        self._http = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={
                "Authorization": f"OAuth {oauth_token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(
        self, method: str, path: str, *, json: dict | None = None, params: dict | None = None
    ) -> dict:
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = await self._http.request(method, path, json=json, params=params)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", attempt * 2))
                    logger.warning("Rate limited, retry in %ds", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError:
                raise
            except httpx.HTTPError as exc:
                if attempt == _MAX_RETRIES:
                    raise
                await asyncio.sleep(attempt * 2)
                logger.warning("Retry %d: %s", attempt, exc)
        return {}

    # ------------------------------------------------------------------
    # Organization info
    # ------------------------------------------------------------------

    async def get_organization(self) -> dict:
        """Get organization info."""
        return await self._request(
            "GET", f"/directory/v1/org/{self.org_id}"
        )

    # ------------------------------------------------------------------
    # User Management (for bot account creation)
    # ------------------------------------------------------------------

    async def create_user(
        self,
        *,
        nickname: str,
        password: str,
        department_id: int = 1,
        name: dict[str, str] | None = None,
        about: str = "",
        position: str = "",
    ) -> dict:
        """Create a new user in the organization.

        This can be used to create user accounts that will serve as
        bot identities. The admin can then register these users as bots
        through the bot-platform admin panel.

        Args:
            nickname: Login prefix (user@org.domain)
            password: Initial password
            department_id: Department ID (1 = root department)
            name: {"first": "...", "last": "...", "middle": "..."}
            about: User description
            position: Job title/position
        """
        body: dict[str, Any] = {
            "nickname": nickname,
            "password": password,
            "departmentId": department_id,
        }
        if name:
            body["name"] = name
        if about:
            body["about"] = about
        if position:
            body["position"] = position

        return await self._request(
            "POST",
            f"/directory/v1/org/{self.org_id}/users",
            json=body,
        )

    async def get_user(self, user_id: str) -> dict:
        """Get user by ID."""
        return await self._request(
            "GET", f"/directory/v1/org/{self.org_id}/users/{user_id}"
        )

    async def update_user(self, user_id: str, **fields: Any) -> dict:
        """Update user fields."""
        return await self._request(
            "PATCH",
            f"/directory/v1/org/{self.org_id}/users/{user_id}",
            json=fields,
        )

    async def list_users(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> dict:
        """List users in the organization.

        Response includes `isRobot` field for each user indicating
        whether the user is a bot account.
        """
        return await self._request(
            "GET",
            f"/directory/v1/org/{self.org_id}/users",
            params={"page": page, "perPage": per_page},
        )

    async def list_bot_users(self) -> list[dict]:
        """List all users marked as robots (isRobot=true)."""
        bots = []
        page = 1
        while True:
            resp = await self.list_users(page=page, per_page=100)
            users = resp.get("users", [])
            if not users:
                break
            for u in users:
                if u.get("isRobot"):
                    bots.append(u)
            total = resp.get("total", 0)
            if page * 100 >= total:
                break
            page += 1
        return bots

    async def delete_user(self, user_id: str) -> dict:
        """Delete a user from the organization."""
        return await self._request(
            "DELETE", f"/directory/v1/org/{self.org_id}/users/{user_id}"
        )

    # ------------------------------------------------------------------
    # Departments
    # ------------------------------------------------------------------

    async def list_departments(self, page: int = 1, per_page: int = 100) -> dict:
        """List departments."""
        return await self._request(
            "GET",
            f"/directory/v1/org/{self.org_id}/departments",
            params={"page": page, "perPage": per_page},
        )

    async def create_department(
        self, name: str, parent_id: int = 1, description: str = ""
    ) -> dict:
        """Create a department (e.g., a 'Bots' department)."""
        return await self._request(
            "POST",
            f"/directory/v1/org/{self.org_id}/departments",
            json={
                "name": name,
                "parentId": parent_id,
                "description": description,
            },
        )

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    async def list_groups(self, page: int = 1, per_page: int = 100) -> dict:
        """List groups."""
        return await self._request(
            "GET",
            f"/directory/v1/org/{self.org_id}/groups",
            params={"page": page, "perPage": per_page},
        )

    async def add_user_to_group(self, group_id: int, user_id: str) -> dict:
        """Add a user to a group."""
        return await self._request(
            "POST",
            f"/directory/v1/org/{self.org_id}/groups/{group_id}/members",
            json={"id": user_id, "type": "user"},
        )
