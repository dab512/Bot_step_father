"""SQLAlchemy models for the bot registry."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RegisteredBot(Base):
    """A child bot created/registered through Bot StepFather."""

    __tablename__ = "registered_bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Human-readable name chosen by the creator
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # The real Yandex Messenger OAuth token for this bot
    ym_token: Mapped[str] = mapped_column(Text, nullable=False)

    # A generated Telegram-compatible token that external users use
    # Format: <numeric_id>:<random_hash>  (mimics Telegram token format)
    tg_compat_token: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)

    # Owner login in Yandex Messenger (who registered the bot)
    owner_login: Mapped[str] = mapped_column(String(200), nullable=False)

    # Webhook URL set by the bot developer (Telegram-style setWebhook)
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Secret token for webhook verification (X-Telegram-Bot-Api-Secret-Token)
    webhook_secret: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Whether the Yandex-side webhook has been configured to point to our adapter
    ym_webhook_active: Mapped[bool] = mapped_column(Boolean, default=False)

    # Bot's display name in Yandex Messenger (fetched after registration)
    ym_display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ym_login: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # --- API mode ---
    # "bot_api" = classic Bot API (botapi.messenger.yandex.net)
    # "client_api" = Client API as user-bot (api.messenger.yandex.ru)
    api_mode: Mapped[str] = mapped_column(String(20), default="bot_api")

    # For client_api mode: service account / user credentials
    ym_user_login: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ym_user_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    ym_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    yc_service_account_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @staticmethod
    def generate_tg_token(bot_id: int) -> str:
        """Generate a Telegram-compatible token: <id>:<random_hash>."""
        random_part = secrets.token_urlsafe(24)
        return f"{bot_id}:{random_part}"


class ConversationState(Base):
    """Tracks the BotStepFather conversation state with each user."""

    __tablename__ = "conversation_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_login: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(50), default="idle")
    # JSON-encoded context data for multi-step flows
    context_data: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# ------------------------------------------------------------------
# Engine / session helpers
# ------------------------------------------------------------------

_engine = None
_session_factory = None


async def init_db(database_url: str) -> None:
    global _engine, _session_factory
    _engine = create_async_engine(database_url, echo=False)
    _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def get_session() -> AsyncSession:
    if _session_factory is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _session_factory()
