"""Message handlers for Bot StepFather.

Implements a conversation-state-machine that guides users through
creating and managing child bots in Yandex Messenger.

Commands:
  /start     — Welcome message and menu
  /newbot    — Start the new bot registration flow
  /mybots   — List user's registered bots
  /token     — Show or regenerate the TG-compatible token for a bot
  /deletebot — Delete a registered bot
  /help      — Show help text

The actual creation of the bot in Yandex 360 admin panel must be done
manually by the user (Yandex provides no API for this). StepFather
guides them through the process and then registers the token.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select

from bot_stepfather.yandex_client import YandexMessengerClient
from config import settings
from database.models import ConversationState, RegisteredBot, get_session

logger = logging.getLogger(__name__)

HELP_TEXT = """Я — Bot StepFather, аналог BotFather для Яндекс Мессенджера.

Я помогу вам зарегистрировать ваших ботов и получить Telegram-совместимый токен для работы через TG2YM адаптер.

Команды:
/newbot — Зарегистрировать нового бота
/mybots — Список ваших ботов
/token — Показать TG-совместимый токен бота
/deletebot — Удалить регистрацию бота
/help — Эта справка

Как это работает:
1. Вы создаёте бота в админ-панели Яндекс 360 (admin.yandex.ru/bot-platform)
2. Копируете OAuth-токен бота
3. Регистрируете бота через меня командой /newbot
4. Получаете TG-совместимый токен и URL адаптера
5. В вашем Telegram-боте меняете endpoint и токен — готово!

Адрес адаптера: {adapter_url}/bot<TOKEN>/<METHOD>
"""

NEWBOT_STEP1 = """Отлично! Давайте зарегистрируем нового бота.

Сначала создайте бота в админ-панели Яндекс 360:
1. Откройте https://admin.yandex.ru/bot-platform
2. Нажмите «Создать бота»
3. Введите имя и загрузите аватар
4. Нажмите «Создать» — токен скопируется в буфер обмена

Теперь пришлите мне имя вашего нового бота:"""

NEWBOT_STEP2 = """Имя "{name}" принято.

Теперь пришлите мне OAuth-токен бота, который вы получили при создании в админ-панели Яндекс 360.

⚠️ Токен будет надёжно сохранён и не будет показываться третьим лицам."""

NEWBOT_DONE = """Бот "{name}" успешно зарегистрирован!

Ваш TG-совместимый токен:
{tg_token}

Базовый URL адаптера:
{adapter_url}/bot{tg_token}/

Замените в вашем Telegram-боте:
  api.telegram.org → {adapter_host}
  Токен → {tg_token}

Пример вызова sendMessage:
POST {adapter_url}/bot{tg_token}/sendMessage
Content-Type: application/json
{{"chat_id": "user@org.ru", "text": "Привет!"}}

Поддерживаемые методы: getUpdates, setWebhook, deleteWebhook, sendMessage, sendPhoto, sendDocument, editMessageText, deleteMessage, answerCallbackQuery, getMe, getFile"""

MYBOTS_EMPTY = "У вас пока нет зарегистрированных ботов. Используйте /newbot для регистрации."


async def handle_message(
    client: YandexMessengerClient,
    from_login: str,
    chat_id: str | None,
    text: str,
) -> None:
    """Main message handler dispatching to appropriate flow."""
    text = text.strip()

    # Load conversation state
    state, ctx = await _get_state(from_login)

    # Check for commands (override any state)
    if text.startswith("/"):
        cmd = text.split()[0].lower()
        if cmd == "/start" or cmd == "/help":
            await _reset_state(from_login)
            await _reply(client, chat_id, from_login, HELP_TEXT.format(
                adapter_url=settings.adapter_public_url
            ))
            return
        elif cmd == "/newbot":
            await _set_state(from_login, "newbot_name", {})
            await _reply(client, chat_id, from_login, NEWBOT_STEP1)
            return
        elif cmd == "/mybots":
            await _handle_mybots(client, chat_id, from_login)
            return
        elif cmd == "/token":
            await _handle_token(client, chat_id, from_login, text)
            return
        elif cmd == "/deletebot":
            await _handle_deletebot_start(client, chat_id, from_login)
            return

    # Handle state-machine flow
    if state == "newbot_name":
        await _handle_newbot_name(client, chat_id, from_login, text)
    elif state == "newbot_token":
        await _handle_newbot_token(client, chat_id, from_login, text, ctx)
    elif state == "deletebot_confirm":
        await _handle_deletebot_confirm(client, chat_id, from_login, text, ctx)
    else:
        await _reply(
            client, chat_id, from_login,
            "Не понимаю. Отправьте /help для списка команд."
        )


# ------------------------------------------------------------------
# State management
# ------------------------------------------------------------------

async def _get_state(login: str) -> tuple[str, dict]:
    async with get_session() as session:
        stmt = select(ConversationState).where(ConversationState.user_login == login)
        result = await session.execute(stmt)
        cs = result.scalar_one_or_none()
        if cs:
            return cs.state, json.loads(cs.context_data)
        return "idle", {}


async def _set_state(login: str, state: str, ctx: dict) -> None:
    async with get_session() as session:
        stmt = select(ConversationState).where(ConversationState.user_login == login)
        result = await session.execute(stmt)
        cs = result.scalar_one_or_none()
        if cs:
            cs.state = state
            cs.context_data = json.dumps(ctx)
        else:
            cs = ConversationState(
                user_login=login, state=state, context_data=json.dumps(ctx)
            )
            session.add(cs)
        await session.commit()


async def _reset_state(login: str) -> None:
    await _set_state(login, "idle", {})


# ------------------------------------------------------------------
# Reply helper
# ------------------------------------------------------------------

async def _reply(
    client: YandexMessengerClient,
    chat_id: str | None,
    login: str,
    text: str,
) -> None:
    await client.send_text(text=text, chat_id=chat_id, login=login)


# ------------------------------------------------------------------
# /newbot flow
# ------------------------------------------------------------------

async def _handle_newbot_name(
    client: YandexMessengerClient,
    chat_id: str | None,
    login: str,
    name: str,
) -> None:
    name = name.strip()
    if len(name) < 2 or len(name) > 200:
        await _reply(client, chat_id, login, "Имя бота должно быть от 2 до 200 символов. Попробуйте ещё раз:")
        return

    await _set_state(login, "newbot_token", {"name": name})
    await _reply(client, chat_id, login, NEWBOT_STEP2.format(name=name))


async def _handle_newbot_token(
    client: YandexMessengerClient,
    chat_id: str | None,
    login: str,
    token: str,
    ctx: dict,
) -> None:
    token = token.strip()
    name = ctx.get("name", "Unknown Bot")

    if len(token) < 10:
        await _reply(
            client, chat_id, login,
            "Токен выглядит слишком коротким. Пришлите полный OAuth-токен бота из Яндекс 360 админ-панели:"
        )
        return

    # Validate token by calling Yandex API
    test_client = YandexMessengerClient(token, settings.yandex_api_base)
    try:
        resp = await test_client.set_webhook(None)  # just to check auth
        if not resp.get("ok") and resp.get("code") == "forbidden":
            await _reply(
                client, chat_id, login,
                "Токен невалиден или у бота нет прав. Проверьте токен и попробуйте ещё раз:"
            )
            return
        ym_display_name = resp.get("display_name")
        ym_login = resp.get("login")
    except Exception as exc:
        logger.warning("Token validation failed: %s", exc)
        await _reply(
            client, chat_id, login,
            "Не удалось проверить токен. Проверьте токен и попробуйте ещё раз:"
        )
        return
    finally:
        await test_client.close()

    # Register the bot in DB
    async with get_session() as session:
        new_bot = RegisteredBot(
            name=name,
            ym_token=token,
            tg_compat_token="",  # will set after we know the ID
            owner_login=login,
            ym_display_name=ym_display_name,
            ym_login=ym_login,
        )
        session.add(new_bot)
        await session.flush()  # get the auto-generated ID

        tg_token = RegisteredBot.generate_tg_token(new_bot.id)
        new_bot.tg_compat_token = tg_token
        await session.commit()

    await _reset_state(login)
    await _reply(
        client, chat_id, login,
        NEWBOT_DONE.format(
            name=name,
            tg_token=tg_token,
            adapter_url=settings.adapter_public_url,
            adapter_host=settings.adapter_public_url.replace("https://", "").replace("http://", ""),
        ),
    )


# ------------------------------------------------------------------
# /mybots
# ------------------------------------------------------------------

async def _handle_mybots(
    client: YandexMessengerClient,
    chat_id: str | None,
    login: str,
) -> None:
    async with get_session() as session:
        stmt = (
            select(RegisteredBot)
            .where(RegisteredBot.owner_login == login, RegisteredBot.is_active.is_(True))
            .order_by(RegisteredBot.created_at)
        )
        result = await session.execute(stmt)
        bots = result.scalars().all()

    if not bots:
        await _reply(client, chat_id, login, MYBOTS_EMPTY)
        return

    lines = ["Ваши зарегистрированные боты:\n"]
    for b in bots:
        webhook_status = "webhook" if b.ym_webhook_active else "polling"
        lines.append(f"  {b.id}. {b.name} ({webhook_status})")
    lines.append(f"\nВсего ботов: {len(bots)}")
    lines.append("Используйте /token <id> для просмотра токена бота.")
    await _reply(client, chat_id, login, "\n".join(lines))


# ------------------------------------------------------------------
# /token <id>
# ------------------------------------------------------------------

async def _handle_token(
    client: YandexMessengerClient,
    chat_id: str | None,
    login: str,
    text: str,
) -> None:
    parts = text.split()
    if len(parts) < 2:
        await _reply(
            client, chat_id, login,
            "Укажите ID бота: /token <id>\nИспользуйте /mybots чтобы увидеть список."
        )
        return

    try:
        bot_id = int(parts[1])
    except ValueError:
        await _reply(client, chat_id, login, "ID бота должен быть числом.")
        return

    async with get_session() as session:
        stmt = select(RegisteredBot).where(
            RegisteredBot.id == bot_id,
            RegisteredBot.owner_login == login,
            RegisteredBot.is_active.is_(True),
        )
        result = await session.execute(stmt)
        bot = result.scalar_one_or_none()

    if not bot:
        await _reply(client, chat_id, login, f"Бот с ID {bot_id} не найден или не принадлежит вам.")
        return

    await _reply(
        client, chat_id, login,
        f"Бот: {bot.name}\n"
        f"TG-совместимый токен:\n{bot.tg_compat_token}\n\n"
        f"URL адаптера:\n{settings.adapter_public_url}/bot{bot.tg_compat_token}/\n\n"
        f"Webhook: {'активен → ' + (bot.webhook_url or '') if bot.ym_webhook_active else 'не установлен (polling)'}"
    )


# ------------------------------------------------------------------
# /deletebot
# ------------------------------------------------------------------

async def _handle_deletebot_start(
    client: YandexMessengerClient,
    chat_id: str | None,
    login: str,
) -> None:
    async with get_session() as session:
        stmt = (
            select(RegisteredBot)
            .where(RegisteredBot.owner_login == login, RegisteredBot.is_active.is_(True))
        )
        result = await session.execute(stmt)
        bots = result.scalars().all()

    if not bots:
        await _reply(client, chat_id, login, MYBOTS_EMPTY)
        return

    lines = ["Какого бота удалить? Отправьте ID:\n"]
    for b in bots:
        lines.append(f"  {b.id}. {b.name}")
    await _set_state(login, "deletebot_confirm", {})
    await _reply(client, chat_id, login, "\n".join(lines))


async def _handle_deletebot_confirm(
    client: YandexMessengerClient,
    chat_id: str | None,
    login: str,
    text: str,
    ctx: dict,
) -> None:
    try:
        bot_id = int(text.strip())
    except ValueError:
        await _reply(client, chat_id, login, "Отправьте числовой ID бота или /start для отмены.")
        return

    async with get_session() as session:
        stmt = select(RegisteredBot).where(
            RegisteredBot.id == bot_id,
            RegisteredBot.owner_login == login,
            RegisteredBot.is_active.is_(True),
        )
        result = await session.execute(stmt)
        bot = result.scalar_one_or_none()

        if not bot:
            await _reply(client, chat_id, login, f"Бот с ID {bot_id} не найден.")
            await _reset_state(login)
            return

        bot.is_active = False
        await session.commit()
        name = bot.name

    await _reset_state(login)
    await _reply(
        client, chat_id, login,
        f"Бот «{name}» (ID {bot_id}) удалён из реестра.\n"
        f"Токен деактивирован. Бот в Яндекс 360 остаётся — удалите его вручную через админ-панель."
    )
