"""Webhook manager: bridges Yandex Messenger webhooks to Telegram-style webhooks.

Flow:
  1. Bot developer calls setWebhook(url=...) via TG-compatible API
  2. We configure Yandex Messenger webhook to point to OUR adapter endpoint
  3. When Yandex sends updates to our endpoint, we translate them to TG format
  4. We forward the translated update to the developer's webhook URL
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from tg2ym.models.yandex import YmUpdate
from tg2ym.translator import ym_update_to_tg

logger = logging.getLogger(__name__)


class WebhookTarget:
    """State for a single bot's webhook forwarding."""

    def __init__(
        self,
        tg_compat_token: str,
        developer_url: str,
        secret_token: str | None = None,
    ) -> None:
        self.tg_compat_token = tg_compat_token
        self.developer_url = developer_url
        self.secret_token = secret_token
        self.chat_id_map: dict[int, str] = {}
        self.pending_update_count: int = 0
        self.last_error_message: str | None = None
        self.last_error_date: int | None = None


class WebhookManager:
    """Manages webhook forwarding for all bots in webhook mode."""

    def __init__(self) -> None:
        self._targets: dict[str, WebhookTarget] = {}  # keyed by tg_compat_token
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))

    async def register_webhook(
        self,
        tg_compat_token: str,
        developer_url: str,
        secret_token: str | None = None,
    ) -> None:
        self._targets[tg_compat_token] = WebhookTarget(
            tg_compat_token, developer_url, secret_token
        )
        logger.info(
            "Registered webhook for bot %s... → %s",
            tg_compat_token[:12], developer_url,
        )

    async def unregister_webhook(self, tg_compat_token: str) -> None:
        self._targets.pop(tg_compat_token, None)
        logger.info("Unregistered webhook for bot %s...", tg_compat_token[:12])

    def get_target(self, tg_compat_token: str) -> WebhookTarget | None:
        return self._targets.get(tg_compat_token)

    def get_chat_id_map(self, tg_compat_token: str) -> dict[int, str]:
        target = self._targets.get(tg_compat_token)
        return target.chat_id_map if target else {}

    async def forward_ym_update(
        self, tg_compat_token: str, ym_update_raw: dict[str, Any]
    ) -> bool:
        """Translate a Yandex update and forward to the developer's webhook.

        Returns True if delivery succeeded.
        """
        target = self._targets.get(tg_compat_token)
        if not target:
            logger.warning("No webhook target for bot %s...", tg_compat_token[:12])
            return False

        try:
            ym_upd = YmUpdate(**ym_update_raw)
            tg_upd = ym_update_to_tg(ym_upd, target.chat_id_map)
            payload = tg_upd.model_dump(by_alias=True, exclude_none=True)

            headers: dict[str, str] = {"Content-Type": "application/json"}
            if target.secret_token:
                headers["X-Telegram-Bot-Api-Secret-Token"] = target.secret_token

            resp = await self._http.post(
                target.developer_url, json=payload, headers=headers
            )

            if resp.status_code >= 500:
                import time
                target.last_error_message = f"HTTP {resp.status_code}"
                target.last_error_date = int(time.time())
                target.pending_update_count += 1
                logger.warning(
                    "Webhook delivery failed for %s...: HTTP %d",
                    tg_compat_token[:12], resp.status_code,
                )
                return False

            target.pending_update_count = 0
            return True

        except Exception as exc:
            import time
            target.last_error_message = str(exc)
            target.last_error_date = int(time.time())
            target.pending_update_count += 1
            logger.exception("Webhook delivery error for %s...", tg_compat_token[:12])
            return False

    async def close(self) -> None:
        await self._http.aclose()
