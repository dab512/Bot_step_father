"""Bidirectional translator between Telegram Bot API and Yandex Messenger Bot API.

Handles:
  - Converting Yandex Messenger updates → Telegram-format updates
  - Converting Telegram-style API requests → Yandex Messenger API calls
  - Mapping chat IDs, user IDs, message IDs, and keyboard structures
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from tg2ym.models.telegram import (
    TgCallbackQuery,
    TgChat,
    TgDocument,
    TgInlineKeyboardButton,
    TgInlineKeyboardMarkup,
    TgMessage,
    TgPhotoSize,
    TgUpdate,
    TgUser,
)
from tg2ym.models.yandex import YmSuggestButton, YmSuggestButtons, YmUpdate

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# ID mapping helpers
# ------------------------------------------------------------------

def _ym_chat_id_to_tg_int(ym_chat_id: str) -> int:
    """Convert a Yandex chat ID string to a deterministic negative integer.

    Yandex uses format like "0/0/<uuid>".  We hash it to a stable int
    that won't collide in practice and is always negative (like TG group IDs).
    """
    digest = hashlib.sha256(ym_chat_id.encode()).digest()
    # Take first 6 bytes → up to 2^48 values, negate for group-like ID
    num = int.from_bytes(digest[:6], "big")
    return -num


def _tg_int_to_ym_chat_id(tg_id: int, chat_map: dict[int, str]) -> str | None:
    """Look up the original Yandex chat ID from our mapping."""
    return chat_map.get(tg_id)


def _ym_login_to_tg_user_id(login: str) -> int:
    """Convert a Yandex login to a deterministic positive integer user ID."""
    digest = hashlib.sha256(login.encode()).digest()
    return int.from_bytes(digest[:6], "big")


# ------------------------------------------------------------------
# Yandex Update → Telegram Update
# ------------------------------------------------------------------

def ym_update_to_tg(ym: YmUpdate, chat_id_map: dict[int, str]) -> TgUpdate:
    """Translate a single Yandex Messenger update into a Telegram-format Update."""

    tg_chat_id = 0
    ym_chat_id_str = ""
    chat_type = "private"

    if ym.chat:
        ym_chat_id_str = ym.chat.id
        chat_type = ym.chat.type
        if chat_type == "private":
            # For private chats use the sender login hash as positive ID
            if ym.from_ and ym.from_.login:
                tg_chat_id = _ym_login_to_tg_user_id(ym.from_.login)
            else:
                tg_chat_id = _ym_chat_id_to_tg_int(ym_chat_id_str)
        else:
            tg_chat_id = _ym_chat_id_to_tg_int(ym_chat_id_str)

        # Remember mapping for reverse lookups
        chat_id_map[tg_chat_id] = ym_chat_id_str

    # Build TgUser from Yandex sender
    tg_from = None
    if ym.from_:
        login = ym.from_.login or ym.from_.id or "unknown"
        tg_from = TgUser(
            id=_ym_login_to_tg_user_id(login),
            is_bot=ym.from_.robot,
            first_name=ym.from_.display_name or login,
            username=login.split("@")[0] if "@" in login else login,
        )

    tg_chat = TgChat(
        id=tg_chat_id,
        type=_map_chat_type(chat_type),
        first_name=tg_from.first_name if tg_from and chat_type == "private" else None,
        title=None if chat_type == "private" else f"Chat {ym_chat_id_str[:20]}",
    )

    # Check if this is a callback (server_action from suggest_buttons)
    if ym.bot_request and ym.bot_request.server_action and ym.bot_request.server_action.name:
        callback_data = json.dumps(ym.bot_request.server_action.payload) if ym.bot_request.server_action.payload else ym.bot_request.server_action.name
        # Truncate to 64 bytes (Telegram limit)
        if len(callback_data.encode()) > 64:
            callback_data = callback_data[:64]

        cb_message = TgMessage(
            message_id=ym.message_id,
            date=ym.timestamp,
            chat=tg_chat,
            **{"from": tg_from},
        )
        callback_query = TgCallbackQuery(
            id=str(ym.update_id),
            chat_instance=str(tg_chat_id),
            message=cb_message,
            data=callback_data,
            **{"from": tg_from or TgUser(id=0, first_name="unknown")},
        )
        return TgUpdate(update_id=ym.update_id, callback_query=callback_query)

    # Build photo list if images present
    photos: list[TgPhotoSize] | None = None
    if ym.images:
        photos = []
        flat_images = _flatten_images(ym.images)
        for img in flat_images:
            if isinstance(img, dict):
                photos.append(
                    TgPhotoSize(
                        file_id=img.get("file_id", ""),
                        file_unique_id=img.get("file_id", ""),
                        width=img.get("width", 0),
                        height=img.get("height", 0),
                        file_size=img.get("size"),
                    )
                )

    # Build document if file present
    document: TgDocument | None = None
    if ym.file and ym.file.id:
        document = TgDocument(
            file_id=ym.file.id,
            file_unique_id=ym.file.id,
            file_name=ym.file.name or None,
            file_size=ym.file.size or None,
        )

    tg_message = TgMessage(
        message_id=ym.message_id,
        date=ym.timestamp,
        chat=tg_chat,
        text=ym.text if not photos and not document else None,
        caption=ym.text if (photos or document) else None,
        photo=photos,
        document=document,
        **{"from": tg_from},
    )

    return TgUpdate(update_id=ym.update_id, message=tg_message)


def _flatten_images(images: list[Any]) -> list[dict]:
    """Yandex may return images as Image[] or Image[][] — flatten to a flat list."""
    result = []
    for item in images:
        if isinstance(item, list):
            result.extend(item)
        elif isinstance(item, dict):
            result.append(item)
        else:
            # Pydantic model
            result.append(item.model_dump() if hasattr(item, "model_dump") else dict(item))
    return result


def _map_chat_type(ym_type: str) -> str:
    return {"private": "private", "group": "group", "channel": "channel"}.get(
        ym_type, "group"
    )


# ------------------------------------------------------------------
# Telegram InlineKeyboard → Yandex SuggestButtons
# ------------------------------------------------------------------

def tg_inline_keyboard_to_ym_suggest_buttons(
    markup: TgInlineKeyboardMarkup,
) -> dict[str, Any]:
    """Convert Telegram InlineKeyboardMarkup to Yandex suggest_buttons format."""
    buttons: list[dict] = []
    for row in markup.inline_keyboard:
        for btn in row:
            directives: list[dict] = []
            if btn.url:
                directives.append({"type": "open_uri", "uri": btn.url})
            elif btn.callback_data:
                directives.append({
                    "type": "server_action",
                    "name": "callback",
                    "payload": {"data": btn.callback_data},
                })
            else:
                # Fallback: send_message with button text
                directives.append({"type": "send_message", "text": btn.text})

            buttons.append({
                "title": btn.text,
                "directives": directives,
            })

    return {"buttons": buttons}


def ym_suggest_buttons_to_tg_inline_keyboard(
    suggest: dict[str, Any],
) -> TgInlineKeyboardMarkup:
    """Convert Yandex suggest_buttons to Telegram InlineKeyboardMarkup."""
    buttons_data = suggest.get("buttons", [])
    row: list[TgInlineKeyboardButton] = []
    for btn_data in buttons_data:
        title = btn_data.get("title", "")
        directives = btn_data.get("directives", [])

        url = None
        callback_data = None
        for d in directives:
            if d.get("type") == "open_uri":
                url = d.get("uri")
            elif d.get("type") == "server_action":
                payload = d.get("payload", {})
                callback_data = payload.get("data", d.get("name", ""))
            elif d.get("type") == "send_message":
                callback_data = d.get("text", title)

        row.append(TgInlineKeyboardButton(
            text=title, url=url, callback_data=callback_data
        ))

    # Return as a single row (Yandex doesn't have a row concept in suggest_buttons)
    return TgInlineKeyboardMarkup(inline_keyboard=[row] if row else [])


# ------------------------------------------------------------------
# Telegram chat_id → Yandex addressing
# ------------------------------------------------------------------

def resolve_tg_chat_id(
    tg_chat_id: int | str, chat_id_map: dict[int, str]
) -> tuple[str | None, str | None]:
    """Given a Telegram-style chat_id return (ym_chat_id, ym_login).

    Returns one of:
      - (chat_id, None) for group/channel chats
      - (None, login) for private chats addressed by login
      - (chat_id, None) if the chat_id is a string that looks like a Yandex chat ID
    """
    if isinstance(tg_chat_id, str):
        # Could be a Yandex chat ID passed directly, or a @username
        if "/" in tg_chat_id:
            return (tg_chat_id, None)
        if "@" in tg_chat_id:
            return (None, tg_chat_id)
        # Try to interpret as numeric
        try:
            tg_chat_id = int(tg_chat_id)
        except ValueError:
            return (None, tg_chat_id)

    # Look up in our mapping
    ym_id = chat_id_map.get(tg_chat_id)
    if ym_id:
        return (ym_id, None)

    # If positive and not found, might be a user ID — can't resolve without extra info
    return (None, None)
