"""Pydantic models mirroring the Telegram Bot API data structures.

Only the subset needed for the TG2YM adapter is modelled here.
Fields are kept Optional where the Telegram spec says so.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ------------------------------------------------------------------
# Core types
# ------------------------------------------------------------------


class TgUser(BaseModel):
    id: int
    is_bot: bool = False
    first_name: str = ""
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None


class TgChat(BaseModel):
    id: int
    type: str  # "private", "group", "supergroup", "channel"
    title: str | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class TgMessageEntity(BaseModel):
    type: str
    offset: int
    length: int
    url: str | None = None
    user: TgUser | None = None
    language: str | None = None


class TgPhotoSize(BaseModel):
    file_id: str
    file_unique_id: str
    width: int
    height: int
    file_size: int | None = None


class TgDocument(BaseModel):
    file_id: str
    file_unique_id: str
    file_name: str | None = None
    mime_type: str | None = None
    file_size: int | None = None


class TgFile(BaseModel):
    file_id: str
    file_unique_id: str
    file_size: int | None = None
    file_path: str | None = None


# ------------------------------------------------------------------
# Inline Keyboard
# ------------------------------------------------------------------


class TgInlineKeyboardButton(BaseModel):
    text: str
    url: str | None = None
    callback_data: str | None = None


class TgInlineKeyboardMarkup(BaseModel):
    inline_keyboard: list[list[TgInlineKeyboardButton]]


class TgReplyKeyboardMarkup(BaseModel):
    keyboard: list[list[dict[str, Any]]]
    resize_keyboard: bool | None = None
    one_time_keyboard: bool | None = None
    selective: bool | None = None


class TgReplyKeyboardRemove(BaseModel):
    remove_keyboard: bool = True
    selective: bool | None = None


class TgForceReply(BaseModel):
    force_reply: bool = True
    selective: bool | None = None


# ------------------------------------------------------------------
# Message
# ------------------------------------------------------------------


class TgMessage(BaseModel):
    message_id: int
    message_thread_id: int | None = None
    from_user: TgUser | None = Field(None, alias="from")
    date: int
    chat: TgChat
    text: str | None = None
    entities: list[TgMessageEntity] | None = None
    reply_to_message: TgMessage | None = None
    photo: list[TgPhotoSize] | None = None
    document: TgDocument | None = None
    caption: str | None = None
    caption_entities: list[TgMessageEntity] | None = None
    reply_markup: TgInlineKeyboardMarkup | None = None

    model_config = {"populate_by_name": True}


# ------------------------------------------------------------------
# Callback Query
# ------------------------------------------------------------------


class TgCallbackQuery(BaseModel):
    id: str
    from_user: TgUser = Field(..., alias="from")
    chat_instance: str = ""
    message: TgMessage | None = None
    data: str | None = None

    model_config = {"populate_by_name": True}


# ------------------------------------------------------------------
# Update
# ------------------------------------------------------------------


class TgUpdate(BaseModel):
    update_id: int
    message: TgMessage | None = None
    edited_message: TgMessage | None = None
    channel_post: TgMessage | None = None
    callback_query: TgCallbackQuery | None = None


# ------------------------------------------------------------------
# Webhook Info
# ------------------------------------------------------------------


class TgWebhookInfo(BaseModel):
    url: str = ""
    has_custom_certificate: bool = False
    pending_update_count: int = 0
    last_error_date: int | None = None
    last_error_message: str | None = None
    max_connections: int | None = None
    allowed_updates: list[str] | None = None


# ------------------------------------------------------------------
# API Response wrapper
# ------------------------------------------------------------------


class TgResponse(BaseModel):
    ok: bool
    result: Any = None
    description: str | None = None
    error_code: int | None = None


# ------------------------------------------------------------------
# Request bodies for key methods
# ------------------------------------------------------------------


class SendMessageRequest(BaseModel):
    chat_id: int | str
    text: str
    parse_mode: str | None = None
    entities: list[TgMessageEntity] | None = None
    disable_web_page_preview: bool | None = None
    disable_notification: bool | None = None
    reply_to_message_id: int | None = None
    reply_parameters: dict | None = None
    reply_markup: (
        TgInlineKeyboardMarkup
        | TgReplyKeyboardMarkup
        | TgReplyKeyboardRemove
        | TgForceReply
        | None
    ) = None
    message_thread_id: int | None = None


class SendPhotoRequest(BaseModel):
    chat_id: int | str
    caption: str | None = None
    parse_mode: str | None = None
    disable_notification: bool | None = None
    reply_to_message_id: int | None = None
    reply_markup: TgInlineKeyboardMarkup | None = None
    message_thread_id: int | None = None


class SendDocumentRequest(BaseModel):
    chat_id: int | str
    caption: str | None = None
    parse_mode: str | None = None
    disable_notification: bool | None = None
    reply_to_message_id: int | None = None
    reply_markup: TgInlineKeyboardMarkup | None = None
    message_thread_id: int | None = None


class EditMessageTextRequest(BaseModel):
    chat_id: int | str | None = None
    message_id: int | None = None
    text: str = ""
    parse_mode: str | None = None
    entities: list[TgMessageEntity] | None = None
    disable_web_page_preview: bool | None = None
    reply_markup: TgInlineKeyboardMarkup | None = None


class DeleteMessageRequest(BaseModel):
    chat_id: int | str
    message_id: int


class AnswerCallbackQueryRequest(BaseModel):
    callback_query_id: str
    text: str | None = None
    show_alert: bool = False
    cache_time: int = 0


class SetWebhookRequest(BaseModel):
    url: str
    certificate: Any = None
    max_connections: int | None = None
    allowed_updates: list[str] | None = None
    drop_pending_updates: bool | None = None
    secret_token: str | None = None


class DeleteWebhookRequest(BaseModel):
    drop_pending_updates: bool | None = None


class GetUpdatesRequest(BaseModel):
    offset: int | None = None
    limit: int | None = None
    timeout: int | None = None
    allowed_updates: list[str] | None = None
