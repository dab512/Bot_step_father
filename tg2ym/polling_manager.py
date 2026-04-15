"""Polling manager: bridges Yandex short-polling to Telegram long-polling.

For each registered bot that uses polling mode (no webhook set):
  - A background task continuously short-polls Yandex getUpdates every ~1s
  - Incoming Yandex updates are translated to Telegram format and buffered
  - When a TG-compatible getUpdates request arrives with a timeout,
    we serve from the buffer or block until timeout (simulating long-polling)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

from bot_stepfather.yandex_client import YandexMessengerClient
from tg2ym.models.telegram import TgUpdate
from tg2ym.models.yandex import YmUpdate
from tg2ym.translator import ym_update_to_tg

logger = logging.getLogger(__name__)


class BotPollingState:
    """Per-bot polling state."""

    def __init__(self, ym_token: str, ym_base_url: str) -> None:
        self.client = YandexMessengerClient(ym_token, ym_base_url)
        self.ym_offset: int = 0
        self.buffer: list[TgUpdate] = []
        self.chat_id_map: dict[int, str] = {}
        self._new_update_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stop = False

    async def close(self) -> None:
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.client.close()


class PollingManager:
    """Manages polling loops for all bots in polling mode."""

    def __init__(self, ym_base_url: str, poll_interval: float = 1.0) -> None:
        self._bots: dict[str, BotPollingState] = {}  # keyed by tg_compat_token
        self._ym_base_url = ym_base_url
        self._poll_interval = poll_interval

    async def register_bot(self, tg_compat_token: str, ym_token: str) -> None:
        if tg_compat_token in self._bots:
            return
        state = BotPollingState(ym_token, self._ym_base_url)
        self._bots[tg_compat_token] = state
        state._task = asyncio.create_task(
            self._poll_loop(tg_compat_token, state)
        )
        logger.info("Started polling loop for bot token %s...", tg_compat_token[:12])

    async def unregister_bot(self, tg_compat_token: str) -> None:
        state = self._bots.pop(tg_compat_token, None)
        if state:
            await state.close()

    async def get_updates(
        self,
        tg_compat_token: str,
        offset: int | None = None,
        limit: int = 100,
        timeout: int = 0,
    ) -> list[dict]:
        """Serve Telegram-compatible getUpdates with optional long-polling."""
        state = self._bots.get(tg_compat_token)
        if not state:
            return []

        # Apply offset: drop all updates with update_id < offset
        if offset is not None:
            state.buffer = [u for u in state.buffer if u.update_id >= offset]

        # If buffer has data, return immediately
        if state.buffer:
            result = state.buffer[:limit]
            return [u.model_dump(by_alias=True, exclude_none=True) for u in result]

        # Long-polling: wait up to `timeout` seconds for new updates
        if timeout > 0:
            state._new_update_event.clear()
            try:
                await asyncio.wait_for(
                    state._new_update_event.wait(), timeout=timeout
                )
            except asyncio.TimeoutError:
                pass

        # Re-apply offset after waiting
        if offset is not None:
            state.buffer = [u for u in state.buffer if u.update_id >= offset]

        result = state.buffer[:limit]
        return [u.model_dump(by_alias=True, exclude_none=True) for u in result]

    def get_chat_id_map(self, tg_compat_token: str) -> dict[int, str]:
        state = self._bots.get(tg_compat_token)
        return state.chat_id_map if state else {}

    def get_client(self, tg_compat_token: str) -> YandexMessengerClient | None:
        state = self._bots.get(tg_compat_token)
        return state.client if state else None

    async def _poll_loop(self, token: str, state: BotPollingState) -> None:
        logger.info("Polling loop started for %s...", token[:12])
        while not state._stop:
            try:
                resp = await state.client.get_updates(
                    offset=state.ym_offset, limit=100
                )
                updates_raw = resp.get("updates", [])
                if updates_raw:
                    max_id = state.ym_offset
                    for raw in updates_raw:
                        ym_upd = YmUpdate(**raw)
                        tg_upd = ym_update_to_tg(ym_upd, state.chat_id_map)
                        state.buffer.append(tg_upd)
                        if ym_upd.update_id > max_id:
                            max_id = ym_upd.update_id
                    state.ym_offset = max_id + 1
                    state._new_update_event.set()

                    # Cap buffer at 10 000 updates
                    if len(state.buffer) > 10_000:
                        state.buffer = state.buffer[-5_000:]

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Polling error for bot %s...", token[:12])

            await asyncio.sleep(self._poll_interval)

    async def close_all(self) -> None:
        for token in list(self._bots):
            await self.unregister_bot(token)
