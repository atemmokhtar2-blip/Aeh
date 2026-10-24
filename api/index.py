import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

import main as bot

log = logging.getLogger("aeh.webhook")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv("TELEGRAM_WEBHOOK_PATH", "").strip().strip("/")
if not WEBHOOK_SECRET:
    log.warning("TELEGRAM_WEBHOOK_SECRET is not configured; webhook requests will be rejected.")
if not WEBHOOK_PATH:
    WEBHOOK_PATH = "telegram"

telegram_app = None

@asynccontextmanager
async def lifespan(_: FastAPI):
    global telegram_app
    if not bot.TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required.")
    if not bot.INITIAL_ADMINS:
        raise RuntimeError("ADMIN_IDS must contain at least one Telegram user ID.")
    if bot.OWNER is None or bot.OWNER not in bot.INITIAL_ADMINS:
        raise RuntimeError("OWNER_ID must be set and included in ADMIN_IDS.")
    bot.init_db()
    telegram_app = Application.builder().token(bot.TOKEN).build()
    for command, fn in [("start", bot.start), ("admin", bot.admin_cmd), ("cancel", bot.cancel),
                        ("setprogram", bot.setprogram), ("addadmin", bot.addadmin),
                        ("deladmin", bot.deladmin), ("user", bot.usercmd)]:
        telegram_app.add_handler(CommandHandler(command, fn))
    telegram_app.add_handler(CallbackQueryHandler(bot.callbacks))
    telegram_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, bot.messages))
    telegram_app.add_error_handler(bot.error_handler)
    await telegram_app.initialize()
    await telegram_app.start()
    yield
    await telegram_app.stop()
    await telegram_app.shutdown()

app = FastAPI(title="Aeh Telegram Webhook", lifespan=lifespan)

@app.get("/")
async def health():
    return {"ok": True, "service": "aeh-telegram-webhook"}

@app.post("/" + WEBHOOK_PATH)
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if not WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Webhook secret is not configured")
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret")
    if telegram_app is None:
        raise HTTPException(status_code=503, detail="Telegram application is not initialized")
    try:
        payload = await request.json()
        update = Update.de_json(payload, telegram_app.bot)
        await telegram_app.process_update(update)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid Telegram update")
    except Exception:
        log.exception("Failed to process Telegram webhook update")
        raise HTTPException(status_code=500, detail="Update processing failed")
    return {"ok": True}
