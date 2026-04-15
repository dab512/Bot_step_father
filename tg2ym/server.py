"""FastAPI server exposing a Telegram-Bot-API-compatible HTTP interface.

Developers point their existing Telegram bot code at:
    https://<adapter_host>/bot<tg_compat_token>/sendMessage
    https://<adapter_host>/bot<tg_compat_token>/getUpdates
    ...

The adapter translates these calls into Yandex Messenger Bot API calls.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse
import io

from bot_stepfather.yandex_client import YandexMessengerClient
from config import settings
from database.models import RegisteredBot, get_session
from sqlalchemy import select
from tg2ym.models.telegram import (
    AnswerCallbackQueryRequest,
    DeleteMessageRequest,
    DeleteWebhookRequest,
    EditMessageTextRequest,
    GetUpdatesRequest,
    SendDocumentRequest,
    SendMessageRequest,
    SendPhotoRequest,
    SetWebhookRequest,
    TgInlineKeyboardMarkup,
    TgResponse,
    TgWebhookInfo,
)
from tg2ym.polling_manager import PollingManager
from tg2ym.translator import (
    resolve_tg_chat_id,
    tg_inline_keyboard_to_ym_suggest_buttons,
)
from tg2ym.webhook_manager import WebhookManager

logger = logging.getLogger(__name__)


def create_adapter_app(
    polling_manager: PollingManager,
    webhook_manager: WebhookManager,
) -> FastAPI:
    app = FastAPI(title="TG2YM Adapter", docs_url="/adapter/docs")

    # Store managers on app state
    app.state.polling = polling_manager
    app.state.webhooks = webhook_manager

    # ------------------------------------------------------------------
    # Token resolution helper
    # ------------------------------------------------------------------

    async def _resolve_bot(token: str) -> RegisteredBot:
        async with get_session() as session:
            stmt = select(RegisteredBot).where(
                RegisteredBot.tg_compat_token == token,
                RegisteredBot.is_active.is_(True),
            )
            result = await session.execute(stmt)
            bot = result.scalar_one_or_none()
        if not bot:
            raise HTTPException(status_code=401, detail="Invalid bot token")
        return bot

    def _get_client_and_map(
        token: str, bot: RegisteredBot
    ) -> tuple[YandexMessengerClient, dict[int, str]]:
        """Get the Yandex client and chat_id map for a bot."""
        # Try polling manager first
        client = polling_manager.get_client(token)
        chat_map = polling_manager.get_chat_id_map(token)
        if client:
            return client, chat_map
        # For webhook-mode bots, create a transient client
        chat_map = webhook_manager.get_chat_id_map(token)
        client = YandexMessengerClient(bot.ym_token, settings.yandex_api_base)
        return client, chat_map

    def _tg_ok(result: Any = True) -> JSONResponse:
        return JSONResponse({"ok": True, "result": result})

    def _tg_error(code: int, description: str) -> JSONResponse:
        return JSONResponse(
            {"ok": False, "error_code": code, "description": description},
            status_code=code,
        )

    # ------------------------------------------------------------------
    # getMe
    # ------------------------------------------------------------------

    @app.api_route("/bot{token}/getMe", methods=["GET", "POST"])
    async def get_me(token: str):
        bot = await _resolve_bot(token)
        return _tg_ok({
            "id": bot.id,
            "is_bot": True,
            "first_name": bot.name,
            "username": bot.ym_login or bot.name,
            "can_join_groups": True,
            "can_read_all_group_messages": True,
            "supports_inline_queries": False,
        })

    # ------------------------------------------------------------------
    # getUpdates (polling with long-poll simulation)
    # ------------------------------------------------------------------

    @app.api_route("/bot{token}/getUpdates", methods=["GET", "POST"])
    async def get_updates(token: str, request: Request):
        bot = await _resolve_bot(token)

        # Ensure bot is registered for polling
        await polling_manager.register_bot(token, bot.ym_token)

        # Parse parameters from query string or JSON body
        params: dict[str, Any] = dict(request.query_params)
        if request.method == "POST":
            try:
                body = await request.json()
                params.update(body)
            except Exception:
                pass

        offset = int(params.get("offset", 0)) if params.get("offset") else None
        limit = min(int(params.get("limit", 100)), 100)
        timeout = min(
            int(params.get("timeout", 0)),
            settings.tg_long_poll_max_timeout,
        )

        updates = await polling_manager.get_updates(
            token, offset=offset, limit=limit, timeout=timeout
        )
        return _tg_ok(updates)

    # ------------------------------------------------------------------
    # setWebhook
    # ------------------------------------------------------------------

    @app.api_route("/bot{token}/setWebhook", methods=["GET", "POST"])
    async def set_webhook(token: str, request: Request):
        bot = await _resolve_bot(token)

        params: dict[str, Any] = dict(request.query_params)
        if request.method == "POST":
            try:
                body = await request.json()
                params.update(body)
            except Exception:
                pass

        developer_url = params.get("url", "")
        secret_token = params.get("secret_token")

        if not developer_url:
            return _tg_error(400, "Bad Request: url is empty")

        # Stop polling for this bot if it was active
        await polling_manager.unregister_bot(token)

        # Register developer's webhook in our manager
        await webhook_manager.register_webhook(token, developer_url, secret_token)

        # Set Yandex webhook to point to OUR adapter's incoming endpoint
        adapter_webhook_url = (
            f"{settings.adapter_public_url}/adapter/ym-webhook/{token}"
        )
        client = YandexMessengerClient(bot.ym_token, settings.yandex_api_base)
        try:
            resp = await client.set_webhook(adapter_webhook_url)
            if not resp.get("ok"):
                return _tg_error(502, f"Failed to set Yandex webhook: {resp}")
        finally:
            await client.close()

        # Save webhook info in DB
        async with get_session() as session:
            stmt = select(RegisteredBot).where(RegisteredBot.id == bot.id)
            result = await session.execute(stmt)
            db_bot = result.scalar_one()
            db_bot.webhook_url = developer_url
            db_bot.webhook_secret = secret_token
            db_bot.ym_webhook_active = True
            await session.commit()

        return _tg_ok(True)

    # ------------------------------------------------------------------
    # deleteWebhook
    # ------------------------------------------------------------------

    @app.api_route("/bot{token}/deleteWebhook", methods=["GET", "POST"])
    async def delete_webhook(token: str, request: Request):
        bot = await _resolve_bot(token)

        # Remove Yandex webhook
        client = YandexMessengerClient(bot.ym_token, settings.yandex_api_base)
        try:
            await client.set_webhook(None)
        finally:
            await client.close()

        # Unregister from webhook manager
        await webhook_manager.unregister_webhook(token)

        # Update DB
        async with get_session() as session:
            stmt = select(RegisteredBot).where(RegisteredBot.id == bot.id)
            result = await session.execute(stmt)
            db_bot = result.scalar_one()
            db_bot.webhook_url = None
            db_bot.webhook_secret = None
            db_bot.ym_webhook_active = False
            await session.commit()

        return _tg_ok(True)

    # ------------------------------------------------------------------
    # getWebhookInfo
    # ------------------------------------------------------------------

    @app.api_route("/bot{token}/getWebhookInfo", methods=["GET", "POST"])
    async def get_webhook_info(token: str):
        bot = await _resolve_bot(token)
        target = webhook_manager.get_target(token)
        info = TgWebhookInfo(
            url=bot.webhook_url or "",
            pending_update_count=target.pending_update_count if target else 0,
            last_error_message=target.last_error_message if target else None,
            last_error_date=target.last_error_date if target else None,
        )
        return _tg_ok(info.model_dump(exclude_none=True))

    # ------------------------------------------------------------------
    # sendMessage
    # ------------------------------------------------------------------

    @app.api_route("/bot{token}/sendMessage", methods=["GET", "POST"])
    async def send_message(token: str, request: Request):
        bot = await _resolve_bot(token)
        client, chat_map = _get_client_and_map(token, bot)

        params: dict[str, Any] = dict(request.query_params)
        if request.method == "POST":
            try:
                body = await request.json()
                params.update(body)
            except Exception:
                pass

        chat_id_raw = params.get("chat_id")
        text = params.get("text", "")

        if not chat_id_raw:
            return _tg_error(400, "Bad Request: chat_id is required")
        if not text:
            return _tg_error(400, "Bad Request: text is required")

        ym_chat_id, ym_login = resolve_tg_chat_id(chat_id_raw, chat_map)
        if not ym_chat_id and not ym_login:
            return _tg_error(400, "Bad Request: unable to resolve chat_id")

        # Convert inline keyboard if present
        suggest_buttons = None
        reply_markup_raw = params.get("reply_markup")
        if reply_markup_raw:
            if isinstance(reply_markup_raw, str):
                import json
                reply_markup_raw = json.loads(reply_markup_raw)
            if "inline_keyboard" in reply_markup_raw:
                markup = TgInlineKeyboardMarkup(**reply_markup_raw)
                suggest_buttons = tg_inline_keyboard_to_ym_suggest_buttons(markup)

        reply_to = params.get("reply_to_message_id")
        if reply_to is not None:
            reply_to = int(reply_to)

        # Handle reply_parameters (newer Telegram API)
        reply_params = params.get("reply_parameters")
        if reply_params and isinstance(reply_params, dict):
            reply_to = reply_to or reply_params.get("message_id")

        resp = await client.send_text(
            text=text,
            chat_id=ym_chat_id,
            login=ym_login,
            reply_message_id=reply_to,
            disable_notification=bool(params.get("disable_notification")),
            disable_web_page_preview=bool(params.get("disable_web_page_preview")),
            thread_id=int(params["message_thread_id"]) if params.get("message_thread_id") else None,
            suggest_buttons=suggest_buttons,
        )

        if not resp.get("ok"):
            return _tg_error(502, resp.get("description", "Yandex API error"))

        # Build a Telegram-style Message response
        now = int(time.time())
        tg_msg = {
            "message_id": resp.get("message_id", 0),
            "from": {"id": bot.id, "is_bot": True, "first_name": bot.name},
            "chat": {"id": int(chat_id_raw) if str(chat_id_raw).lstrip("-").isdigit() else 0, "type": "private"},
            "date": now,
            "text": text,
        }
        return _tg_ok(tg_msg)

    # ------------------------------------------------------------------
    # sendPhoto
    # ------------------------------------------------------------------

    @app.post("/bot{token}/sendPhoto")
    async def send_photo(
        token: str,
        chat_id: str = Form(...),
        photo: UploadFile = File(...),
        caption: str | None = Form(None),
        disable_notification: bool = Form(False),
        reply_to_message_id: int | None = Form(None),
        message_thread_id: int | None = Form(None),
        reply_markup: str | None = Form(None),
    ):
        bot = await _resolve_bot(token)
        client, chat_map = _get_client_and_map(token, bot)

        ym_chat_id, ym_login = resolve_tg_chat_id(chat_id, chat_map)
        if not ym_chat_id and not ym_login:
            return _tg_error(400, "Bad Request: unable to resolve chat_id")

        image_data = await photo.read()
        content_type = photo.content_type or "image/jpeg"
        filename = photo.filename or "photo.jpg"

        suggest_buttons = None
        if reply_markup:
            import json
            markup_data = json.loads(reply_markup)
            if "inline_keyboard" in markup_data:
                markup = TgInlineKeyboardMarkup(**markup_data)
                suggest_buttons = tg_inline_keyboard_to_ym_suggest_buttons(markup)

        resp = await client.send_image(
            image=image_data,
            filename=filename,
            content_type=content_type,
            chat_id=ym_chat_id,
            login=ym_login,
            thread_id=message_thread_id,
            suggest_buttons=suggest_buttons,
        )

        if not resp.get("ok"):
            return _tg_error(502, resp.get("description", "Yandex API error"))

        now = int(time.time())
        tg_msg = {
            "message_id": resp.get("message_id", 0),
            "from": {"id": bot.id, "is_bot": True, "first_name": bot.name},
            "chat": {"id": int(chat_id) if chat_id.lstrip("-").isdigit() else 0, "type": "private"},
            "date": now,
            "photo": [{"file_id": "sent", "file_unique_id": "sent", "width": 0, "height": 0}],
            "caption": caption,
        }
        return _tg_ok(tg_msg)

    # ------------------------------------------------------------------
    # sendDocument
    # ------------------------------------------------------------------

    @app.post("/bot{token}/sendDocument")
    async def send_document(
        token: str,
        chat_id: str = Form(...),
        document: UploadFile = File(...),
        caption: str | None = Form(None),
        disable_notification: bool = Form(False),
        reply_to_message_id: int | None = Form(None),
        message_thread_id: int | None = Form(None),
    ):
        bot = await _resolve_bot(token)
        client, chat_map = _get_client_and_map(token, bot)

        ym_chat_id, ym_login = resolve_tg_chat_id(chat_id, chat_map)
        if not ym_chat_id and not ym_login:
            return _tg_error(400, "Bad Request: unable to resolve chat_id")

        file_data = await document.read()
        content_type = document.content_type or "application/octet-stream"
        filename = document.filename or "document.bin"

        resp = await client.send_file(
            document=file_data,
            filename=filename,
            content_type=content_type,
            chat_id=ym_chat_id,
            login=ym_login,
            thread_id=message_thread_id,
        )

        if not resp.get("ok"):
            return _tg_error(502, resp.get("description", "Yandex API error"))

        now = int(time.time())
        tg_msg = {
            "message_id": resp.get("message_id", 0),
            "from": {"id": bot.id, "is_bot": True, "first_name": bot.name},
            "chat": {"id": int(chat_id) if chat_id.lstrip("-").isdigit() else 0, "type": "private"},
            "date": now,
            "document": {"file_id": "sent", "file_unique_id": "sent", "file_name": filename},
            "caption": caption,
        }
        return _tg_ok(tg_msg)

    # ------------------------------------------------------------------
    # editMessageText
    # ------------------------------------------------------------------

    @app.api_route("/bot{token}/editMessageText", methods=["GET", "POST"])
    async def edit_message_text(token: str, request: Request):
        bot = await _resolve_bot(token)
        client, chat_map = _get_client_and_map(token, bot)

        params: dict[str, Any] = dict(request.query_params)
        if request.method == "POST":
            try:
                body = await request.json()
                params.update(body)
            except Exception:
                pass

        text = params.get("text", "")
        chat_id_raw = params.get("chat_id")
        message_id = params.get("message_id")

        if not text:
            return _tg_error(400, "Bad Request: text is required")

        # Yandex Messenger doesn't have editMessageText — delete + resend
        if chat_id_raw and message_id:
            ym_chat_id, ym_login = resolve_tg_chat_id(chat_id_raw, chat_map)

            # Delete old message
            try:
                await client.delete_message(
                    message_id=int(message_id),
                    chat_id=ym_chat_id,
                    login=ym_login,
                )
            except Exception:
                logger.warning("Could not delete message %s for edit", message_id)

            # Convert inline keyboard if present
            suggest_buttons = None
            reply_markup_raw = params.get("reply_markup")
            if reply_markup_raw:
                if isinstance(reply_markup_raw, str):
                    import json
                    reply_markup_raw = json.loads(reply_markup_raw)
                if "inline_keyboard" in reply_markup_raw:
                    markup = TgInlineKeyboardMarkup(**reply_markup_raw)
                    suggest_buttons = tg_inline_keyboard_to_ym_suggest_buttons(markup)

            # Send new message
            resp = await client.send_text(
                text=text,
                chat_id=ym_chat_id,
                login=ym_login,
                suggest_buttons=suggest_buttons,
            )

            if not resp.get("ok"):
                return _tg_error(502, resp.get("description", "Yandex API error"))

            now = int(time.time())
            tg_msg = {
                "message_id": resp.get("message_id", 0),
                "from": {"id": bot.id, "is_bot": True, "first_name": bot.name},
                "chat": {"id": int(chat_id_raw) if str(chat_id_raw).lstrip("-").isdigit() else 0, "type": "private"},
                "date": now,
                "text": text,
            }
            return _tg_ok(tg_msg)

        return _tg_error(400, "Bad Request: chat_id and message_id are required")

    # ------------------------------------------------------------------
    # deleteMessage
    # ------------------------------------------------------------------

    @app.api_route("/bot{token}/deleteMessage", methods=["GET", "POST"])
    async def delete_message(token: str, request: Request):
        bot = await _resolve_bot(token)
        client, chat_map = _get_client_and_map(token, bot)

        params: dict[str, Any] = dict(request.query_params)
        if request.method == "POST":
            try:
                body = await request.json()
                params.update(body)
            except Exception:
                pass

        chat_id_raw = params.get("chat_id")
        message_id = params.get("message_id")

        if not chat_id_raw or not message_id:
            return _tg_error(400, "Bad Request: chat_id and message_id required")

        ym_chat_id, ym_login = resolve_tg_chat_id(chat_id_raw, chat_map)

        resp = await client.delete_message(
            message_id=int(message_id),
            chat_id=ym_chat_id,
            login=ym_login,
        )

        if not resp.get("ok"):
            return _tg_error(502, resp.get("description", "Yandex API error"))

        return _tg_ok(True)

    # ------------------------------------------------------------------
    # answerCallbackQuery
    # ------------------------------------------------------------------

    @app.api_route("/bot{token}/answerCallbackQuery", methods=["GET", "POST"])
    async def answer_callback_query(token: str, request: Request):
        """Yandex Messenger doesn't have an equivalent — acknowledge silently."""
        await _resolve_bot(token)
        return _tg_ok(True)

    # ------------------------------------------------------------------
    # getFile / file download
    # ------------------------------------------------------------------

    @app.api_route("/bot{token}/getFile", methods=["GET", "POST"])
    async def get_file(token: str, request: Request):
        bot = await _resolve_bot(token)

        params: dict[str, Any] = dict(request.query_params)
        if request.method == "POST":
            try:
                body = await request.json()
                params.update(body)
            except Exception:
                pass

        file_id = params.get("file_id", "")
        if not file_id:
            return _tg_error(400, "Bad Request: file_id is required")

        # Return a file path that points to our download endpoint
        return _tg_ok({
            "file_id": file_id,
            "file_unique_id": file_id,
            "file_path": f"files/{token}/{file_id}",
        })

    @app.get("/file/bot{token}/{file_path:path}")
    async def download_file(token: str, file_path: str):
        bot = await _resolve_bot(token)
        client = YandexMessengerClient(bot.ym_token, settings.yandex_api_base)
        try:
            file_data = await client.get_file(file_path)
        finally:
            await client.close()
        return StreamingResponse(io.BytesIO(file_data), media_type="application/octet-stream")

    # ------------------------------------------------------------------
    # Incoming Yandex Messenger webhook (our adapter receives YM updates here)
    # ------------------------------------------------------------------

    @app.post("/adapter/ym-webhook/{tg_compat_token}")
    async def ym_webhook_receiver(tg_compat_token: str, request: Request):
        """Receives webhook updates FROM Yandex Messenger and forwards
        them to the developer's webhook URL in Telegram format."""
        body = await request.json()
        updates = body.get("updates", [body] if "update_id" in body else [])

        for upd in updates:
            await webhook_manager.forward_ym_update(tg_compat_token, upd)

        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------
    # Catch-all for unsupported methods (return graceful error)
    # ------------------------------------------------------------------

    @app.api_route("/bot{token}/{method}", methods=["GET", "POST"])
    async def unsupported_method(token: str, method: str):
        await _resolve_bot(token)
        return _tg_error(
            400,
            f"Method '{method}' is not yet supported by the TG2YM adapter. "
            f"Supported: getMe, getUpdates, setWebhook, deleteWebhook, "
            f"getWebhookInfo, sendMessage, sendPhoto, sendDocument, "
            f"editMessageText, deleteMessage, answerCallbackQuery, getFile",
        )

    return app
