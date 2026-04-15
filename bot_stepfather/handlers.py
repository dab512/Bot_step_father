"""Message handlers for Bot StepFather.

Implements a conversation-state-machine that guides users through
creating and managing child bots in Yandex Messenger.

Commands:
  /start      — Welcome message and menu
  /newbot     — Register an existing bot (manual: provide Bot API token)
  /createbot  — Fully automated bot creation (creates user via api360,
                obtains OAuth token, registers in adapter)
  /mybots     — List user's registered bots
  /token      — Show the TG-compatible token for a bot
  /deletebot  — Delete a registered bot
  /help       — Show help text

Two API modes:
  - bot_api:    Classic Bot API (botapi.messenger.yandex.net) — internal org only
  - client_api: Client API (api.messenger.yandex.ru) — user-as-bot, can reach
                external users via messenger client protocol
"""

from __future__ import annotations

import json
import logging
import secrets
import string

from sqlalchemy import select

from bot_stepfather.yandex_client import YandexMessengerClient
from config import settings
from database.models import ConversationState, RegisteredBot, get_session

logger = logging.getLogger(__name__)


def _generate_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


HELP_TEXT = """Я — Bot StepFather, аналог BotFather для Яндекс Мессенджера.

Я помогу создать ботов и получить Telegram-совместимый токен для работы через TG2YM адаптер.

Команды:
/createbot — Создать бота автоматически (рекомендуется)
/newbot — Зарегистрировать существующего бота (ручной режим)
/mybots — Список ваших ботов
/token — Показать TG-совместимый токен бота
/deletebot — Удалить бота
/help — Эта справка

Режимы работы:
  bot_api — Классический Bot API (только внутри организации)
  client_api — Клиентское API (пользователь-бот, доступен извне)

/createbot создаёт пользователя в организации через Яндекс 360 API,
получает OAuth-токен с правом yamb:all и регистрирует бота в адаптере.
Вы получаете TG-совместимый токен и URL — замените их в вашем Telegram-боте.

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

CREATEBOT_STEP1 = """Создаём нового бота автоматически!

Я создам пользователя-бота в вашей организации через Яндекс 360 API,
получу OAuth-токен и зарегистрирую бота в TG2YM адаптере.

Пришлите мне имя нового бота (будет использовано как display name):"""

CREATEBOT_MODE = """Имя «{name}» принято. Логин бота в организации: {login}

Выберите режим работы бота:

1 — bot_api (Bot API, только внутри организации)
2 — client_api (Клиентское API, может общаться с внешними пользователями)

Отправьте 1 или 2:"""

CREATEBOT_DONE = """Бот «{name}» успешно создан!

Режим: {api_mode}
Логин в организации: {ym_login}

Ваш TG-совместимый токен:
{tg_token}

Базовый URL адаптера:
{adapter_url}/bot{tg_token}/

Замените в вашем Telegram-боте:
  api.telegram.org → {adapter_host}
  Токен → {tg_token}

Поддерживаемые методы: getUpdates, setWebhook, deleteWebhook, sendMessage, sendPhoto, sendDocument, editMessageText, deleteMessage, answerCallbackQuery, getMe, getFile"""

CREATEBOT_ERROR_NO_CONFIG = """Автоматическое создание ботов не настроено.

Администратор должен задать переменные окружения:
  BSF_YANDEX360_OAUTH_TOKEN — токен с правами ya360_admin:directory_write
  BSF_YANDEX360_ORG_ID — ID организации
  BSF_OAUTH_CLIENT_ID — ID OAuth-приложения с правом yamb:all

Пока используйте /newbot для ручной регистрации."""

MYBOTS_EMPTY = "У вас пока нет зарегистрированных ботов. Используйте /createbot или /newbot."


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
        elif cmd == "/createbot":
            await _set_state(from_login, "createbot_name", {})
            await _reply(client, chat_id, from_login, CREATEBOT_STEP1)
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
    elif state == "createbot_name":
        await _handle_createbot_name(client, chat_id, from_login, text)
    elif state == "createbot_mode":
        await _handle_createbot_mode(client, chat_id, from_login, text, ctx)
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
# /createbot flow (fully automated via api360 + OAuth)
# ------------------------------------------------------------------

async def _handle_createbot_name(
    client: YandexMessengerClient,
    chat_id: str | None,
    login: str,
    name: str,
) -> None:
    name = name.strip()
    if len(name) < 2 or len(name) > 200:
        await _reply(client, chat_id, login, "Имя бота должно быть от 2 до 200 символов. Попробуйте ещё раз:")
        return

    if not settings.yandex360_oauth_token or not settings.yandex360_org_id:
        await _reply(client, chat_id, login, CREATEBOT_ERROR_NO_CONFIG)
        await _reset_state(login)
        return

    # Generate a login for the bot user
    safe_name = "".join(c if c.isalnum() else "-" for c in name.lower())[:30]
    bot_login = f"bot-{safe_name}-{secrets.token_hex(3)}"

    ctx = {"name": name, "login": bot_login}
    await _set_state(login, "createbot_mode", ctx)
    await _reply(client, chat_id, login, CREATEBOT_MODE.format(name=name, login=bot_login))


async def _handle_createbot_mode(
    client: YandexMessengerClient,
    chat_id: str | None,
    login: str,
    text: str,
    ctx: dict,
) -> None:
    text = text.strip()
    if text == "1":
        api_mode = "bot_api"
    elif text == "2":
        api_mode = "client_api"
    else:
        await _reply(client, chat_id, login, "Отправьте 1 (bot_api) или 2 (client_api):")
        return

    name = ctx["name"]
    bot_login = ctx["login"]

    await _reply(client, chat_id, login, f"Создаю бота «{name}»... Это может занять несколько секунд.")

    try:
        result = await _provision_bot(name, bot_login, api_mode, login)
    except Exception as exc:
        logger.exception("Failed to provision bot %s", name)
        await _reply(
            client, chat_id, login,
            f"Ошибка при создании бота: {exc}\n\nПопробуйте ещё раз командой /createbot"
        )
        await _reset_state(login)
        return

    await _reset_state(login)
    await _reply(
        client, chat_id, login,
        CREATEBOT_DONE.format(
            name=name,
            api_mode=api_mode,
            ym_login=result["ym_login"],
            tg_token=result["tg_token"],
            adapter_url=settings.adapter_public_url,
            adapter_host=settings.adapter_public_url.replace("https://", "").replace("http://", ""),
        ),
    )


async def _provision_bot(
    name: str,
    bot_login: str,
    api_mode: str,
    owner_login: str,
) -> dict:
    """Create a user in the org, obtain OAuth token, register in DB.

    Steps:
      1. Create user via Yandex 360 API (api360.yandex.net)
      2. If client_api mode: obtain OAuth token via password grant (yamb:all)
      3. Register in database with TG-compatible token
    """
    from bot_stepfather.yandex360_client import Yandex360Client

    password = _generate_password()

    # Step 1: Create user account in the organization
    y360 = Yandex360Client(settings.yandex360_oauth_token, settings.yandex360_org_id)
    try:
        user_resp = await y360.create_user(
            nickname=bot_login,
            password=password,
            name={"first": name, "last": "Bot"},
            about=f"Bot managed by Bot StepFather. Owner: {owner_login}",
            position="Bot",
        )
    finally:
        await y360.close()

    user_id = str(user_resp.get("id", ""))
    full_login = user_resp.get("email", f"{bot_login}@{settings.yandex360_org_id}")
    logger.info("Created user %s (ID: %s) for bot %s", full_login, user_id, name)

    # Step 2: Obtain OAuth token for the bot user
    ym_token = ""
    refresh_token = ""

    if api_mode == "client_api" and settings.oauth_client_id:
        from bot_stepfather.oauth_helper import YandexOAuth
        oauth = YandexOAuth(settings.oauth_client_id, settings.oauth_client_secret)
        try:
            token_resp = await oauth.token_by_password(
                username=full_login,
                password=password,
                scope="yamb:all",
            )
            ym_token = token_resp.access_token
            refresh_token = token_resp.refresh_token
            logger.info("Obtained yamb:all OAuth token for %s", full_login)
        except Exception as exc:
            logger.warning("OAuth token acquisition failed for %s: %s", full_login, exc)
            # Fall back — token can be obtained later
            ym_token = ""
        finally:
            await oauth.close()

    # Step 3: Register in database
    async with get_session() as session:
        new_bot = RegisteredBot(
            name=name,
            ym_token=ym_token,
            tg_compat_token="",
            owner_login=owner_login,
            api_mode=api_mode,
            ym_user_login=full_login,
            ym_user_password=password,
            ym_refresh_token=refresh_token,
            ym_login=full_login,
            ym_display_name=name,
        )
        session.add(new_bot)
        await session.flush()

        tg_token = RegisteredBot.generate_tg_token(new_bot.id)
        new_bot.tg_compat_token = tg_token
        await session.commit()

    return {
        "tg_token": tg_token,
        "ym_login": full_login,
        "user_id": user_id,
    }


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
        mode = b.api_mode or "bot_api"
        lines.append(f"  {b.id}. {b.name} [{mode}] ({webhook_status})")
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
