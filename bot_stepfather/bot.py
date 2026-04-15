"""Bot StepFather — main bot runner.

Runs in either polling or webhook mode, processing incoming messages
from the Yandex Messenger Bot API and dispatching to handlers.
"""

from __future__ import annotations

import asyncio
import logging

from bot_stepfather.handlers import handle_message
from bot_stepfather.yandex_client import YandexMessengerClient
from config import settings
from tg2ym.models.yandex import YmUpdate

logger = logging.getLogger(__name__)


class BotStepFather:
    """The main BotStepFather bot instance."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or settings.stepfather_token
        if not self.token:
            logger.warning(
                "No StepFather token configured (BSF_STEPFATHER_TOKEN). "
                "Bot StepFather will not start."
            )
        self.client: YandexMessengerClient | None = None
        self._offset = 0
        self._stop = False
        self._task: asyncio.Task | None = None

    async def start(self, mode: str = "polling") -> None:
        if not self.token:
            return

        self.client = YandexMessengerClient(self.token, settings.yandex_api_base)

        if mode == "polling":
            # Disable any webhook so polling works
            await self.client.set_webhook(None)
            self._task = asyncio.create_task(self._polling_loop())
            logger.info("Bot StepFather started in polling mode")
        else:
            # Webhook mode — set webhook URL
            webhook_url = f"{settings.adapter_public_url}/stepfather/webhook"
            await self.client.set_webhook(webhook_url)
            logger.info("Bot StepFather started in webhook mode → %s", webhook_url)

    async def stop(self) -> None:
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.client:
            await self.client.close()

    async def _polling_loop(self) -> None:
        assert self.client is not None
        while not self._stop:
            try:
                resp = await self.client.get_updates(
                    offset=self._offset, limit=100
                )
                updates = resp.get("updates", [])
                for raw in updates:
                    upd = YmUpdate(**raw)
                    await self._process_update(upd)
                    if upd.update_id >= self._offset:
                        self._offset = upd.update_id + 1
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("StepFather polling error")

            await asyncio.sleep(settings.ym_poll_interval)

    async def process_webhook_update(self, raw: dict) -> None:
        """Called from the webhook endpoint."""
        upd = YmUpdate(**raw)
        await self._process_update(upd)

    async def _process_update(self, upd: YmUpdate) -> None:
        assert self.client is not None

        # Skip updates from bots (including self)
        if upd.from_ and upd.from_.robot:
            return

        text = upd.text or ""
        from_login = ""
        if upd.from_:
            from_login = upd.from_.login or upd.from_.id or ""

        chat_id = upd.chat.id if upd.chat else None

        if not from_login:
            return

        # Handle server_action callbacks (from suggest_buttons)
        if upd.bot_request and upd.bot_request.server_action:
            sa = upd.bot_request.server_action
            text = sa.payload.get("data", sa.name)

        if not text:
            return

        try:
            await handle_message(self.client, from_login, chat_id, text)
        except Exception:
            logger.exception("Error handling message from %s", from_login)
            try:
                await self.client.send_text(
                    text="Произошла ошибка при обработке запроса. Попробуйте ещё раз.",
                    chat_id=chat_id,
                    login=from_login,
                )
            except Exception:
                pass
