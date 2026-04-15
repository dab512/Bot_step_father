"""Entry point for Bot StepFather + TG2YM Adapter.

Starts two services:
  1. Bot StepFather — a Yandex Messenger bot managing child bots (polling or webhook)
  2. TG2YM Adapter — a FastAPI server exposing Telegram-compatible API

Usage:
    python main.py                       # Start both services
    python main.py --adapter-only        # Start only the adapter
    python main.py --stepfather-only     # Start only Bot StepFather
    python main.py --stepfather-mode webhook  # StepFather in webhook mode
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bot_stepfather.bot import BotStepFather
from config import settings
from database.models import init_db
from tg2ym.polling_manager import PollingManager
from tg2ym.server import create_adapter_app
from tg2ym.webhook_manager import WebhookManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot_step_father")


def build_combined_app(
    stepfather: BotStepFather,
    polling_manager: PollingManager,
    webhook_manager: WebhookManager,
) -> FastAPI:
    """Build a single FastAPI app combining the adapter and StepFather webhook."""
    adapter_app = create_adapter_app(polling_manager, webhook_manager)

    # Add StepFather webhook endpoint
    @adapter_app.post("/stepfather/webhook")
    async def stepfather_webhook(request: Request):
        body = await request.json()
        updates = body.get("updates", [body] if "update_id" in body else [])
        for upd in updates:
            await stepfather.process_webhook_update(upd)
        return JSONResponse({"ok": True})

    # Health check
    @adapter_app.get("/health")
    async def health():
        return {"status": "ok", "service": "bot-step-father"}

    # Root info
    @adapter_app.get("/")
    async def root():
        return {
            "service": "Bot StepFather + TG2YM Adapter",
            "adapter_docs": "/adapter/docs",
            "usage": "POST /bot<token>/<method> — Telegram-compatible Bot API",
            "health": "/health",
        }

    return adapter_app


async def run_services(
    run_adapter: bool = True,
    run_stepfather: bool = True,
    stepfather_mode: str = "polling",
) -> None:
    # Initialize database
    await init_db(settings.database_url)
    logger.info("Database initialized")

    # Create managers
    polling_manager = PollingManager(
        settings.yandex_api_base, settings.ym_poll_interval
    )
    webhook_manager = WebhookManager()
    stepfather = BotStepFather()

    # Build combined FastAPI app
    app = build_combined_app(stepfather, polling_manager, webhook_manager)

    # Start Bot StepFather
    if run_stepfather:
        await stepfather.start(mode=stepfather_mode)

    # Start FastAPI server
    if run_adapter:
        config = uvicorn.Config(
            app,
            host=settings.adapter_host,
            port=settings.adapter_port,
            log_level="info",
        )
        server = uvicorn.Server(config)
        try:
            await server.serve()
        finally:
            await stepfather.stop()
            await polling_manager.close_all()
            await webhook_manager.close()
    else:
        # Just run StepFather in polling mode indefinitely
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await stepfather.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot StepFather + TG2YM Adapter")
    parser.add_argument("--adapter-only", action="store_true", help="Run only the TG2YM adapter")
    parser.add_argument("--stepfather-only", action="store_true", help="Run only Bot StepFather")
    parser.add_argument(
        "--stepfather-mode",
        choices=["polling", "webhook"],
        default="polling",
        help="StepFather update mode (default: polling)",
    )
    args = parser.parse_args()

    run_adapter = not args.stepfather_only
    run_stepfather = not args.adapter_only

    try:
        asyncio.run(
            run_services(
                run_adapter=run_adapter,
                run_stepfather=run_stepfather,
                stepfather_mode=args.stepfather_mode,
            )
        )
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    main()
