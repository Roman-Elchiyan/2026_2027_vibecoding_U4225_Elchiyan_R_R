"""Run polling with `py bot.py`; offline diagnostics with `py bot.py --check`."""

from __future__ import annotations

import argparse
from functools import wraps
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import secrets
import sys
import time
import traceback

from telegram import BotCommand, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from admin import admin_callback, admin_command
from config import Config, ConfigError, load_config
from db import Database
from handlers import Controller
from sources import CatalogError, CsvSource

logger = logging.getLogger(__name__)
LOG_SALT = secrets.token_bytes(16)


class SafeFormatter(logging.Formatter):
    converter = time.gmtime

    def __init__(self, secret_values: tuple[str, ...] = ()):
        super().__init__("%(asctime)sZ %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S")
        self.secret_values = tuple(value for value in secret_values if value)

    def formatException(self, exc_info) -> str:
        # Exception messages and source lines can contain request text or credentials.
        frames = traceback.extract_tb(exc_info[2])
        return "Traceback:\n" + "\n".join(f"  {frame.filename}:{frame.lineno} in {frame.name}" for frame in frames) + "\n" + exc_info[0].__name__

    def format(self, record: logging.LogRecord) -> str:
        result = super().format(record)
        for secret in self.secret_values:
            result = result.replace(secret, "[REDACTED]")
        return re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}", "[REDACTED]", result)


def configure_logging(config: Config) -> None:
    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    secret_values = tuple(value for key, value in os.environ.items() if any(word in key.upper() for word in ("TOKEN", "API_KEY", "MASTER_KEY", "PASSWORD", "SECRET")))
    formatter = SafeFormatter(secret_values + (config.bot_token,))
    stdout = logging.StreamHandler(sys.stdout)
    file_handler = RotatingFileHandler(config.log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    for handler in (stdout, file_handler):
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[stdout, file_handler], force=True)
    # HTTP URLs contain the bot token. Library diagnostics go through our error handler.
    for name in ("httpx", "httpcore", "telegram"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def guarded(callback):
    @wraps(callback)
    async def handle(update, context):
        if update.effective_chat is None or update.effective_chat.type != "private":
            if update.callback_query:
                await update.callback_query.answer("Откройте личный чат с ботом.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("Для поиска и избранного откройте личный чат с ботом.")
            return
        started = time.perf_counter()
        user_id = update.effective_user.id if update.effective_user else 0
        actor = hashlib.sha256(LOG_SALT + str(user_id).encode()).hexdigest()[:12]
        try:
            await callback(update, context)
        except (ValueError, LookupError, CatalogError) as exc:
            logger.warning("event=action_rejected actor=%s error=%s", actor, type(exc).__name__)
            await update.effective_message.reply_text(str(exc))
        finally:
            logger.info("event=update_processed actor=%s handler=%s elapsed_ms=%.1f", actor, callback.__name__, (time.perf_counter() - started) * 1000)
    return handle


async def error_handler(update: object, context) -> None:
    error = context.error
    # An idempotent repeated favorite callback may leave its markup unchanged.
    if isinstance(error, BadRequest) and "Message is not modified" in str(error):
        return
    logger.error("event=handler_failed", exc_info=(type(error), error, error.__traceback__))
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Не удалось выполнить действие. Попробуйте ещё раз или /status. Сохранённые данные остаются в SQLite.")
        except Exception:
            logger.exception("event=error_notification_failed")


async def post_init(application: Application) -> None:
    commands = [
        ("start", "Главное меню"), ("help", "Помощь"), ("search", "Поиск материалов"),
        ("saved", "Избранное"), ("cancel", "Отменить ввод"), ("status", "Состояние бота"),
        ("ai", "ИИ-поиск (Lab 2)"), ("limits", "Лимиты (Lab 2)"),
        ("feedback", "Обратная связь (Lab 3)"), ("admin", "Диагностика администратора"),
    ]
    await application.bot.set_my_commands([BotCommand(name, description) for name, description in commands])
    logger.info("event=polling_ready stage=lab1")


def build_application(config: Config, controller: Controller) -> Application:
    application = Application.builder().token(config.bot_token).concurrent_updates(False).post_init(post_init).build()
    application.bot_data["controller"] = controller
    for command, callback in {
        "start": controller.start, "help": controller.help, "search": controller.search,
        "saved": controller.saved, "cancel": controller.cancel, "status": controller.status,
        "ai": controller.unavailable, "limits": controller.unavailable,
        "feedback": controller.unavailable, "admin": admin_command,
    }.items():
        application.add_handler(CommandHandler(command, guarded(callback)))
    application.add_handler(CallbackQueryHandler(guarded(admin_callback), pattern=r"^admin:"))
    application.add_handler(CallbackQueryHandler(guarded(controller.callback)))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, guarded(controller.text)))
    application.add_handler(MessageHandler(filters.ALL, guarded(controller.unknown)))
    application.add_error_handler(error_handler)
    return application


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="«Brilliant» detective — Lab 1")
    parser.add_argument("--check", action="store_true", help="Проверить локальную БД и CSV без Telegram и токена")
    args = parser.parse_args()
    try:
        config = load_config(require_token=not args.check)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        configure_logging(config)
        database = Database(config.db_path)
        database.initialize()
        source = CsvSource(config.csv_path)
        controller = Controller(config, database, source)
        if args.check:
            print(controller.status_text())
            source.load()
            return 0 if database.health() else 1
        source.load()  # Fail early on a missing catalog, before contacting Telegram.
        app = build_application(config, controller)
        logger.info("event=bot_starting stage=lab1 records=%d", source.row_count)
        app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)
        return 0
    except Exception:
        logger.exception("event=startup_failed")
        print("Запуск не удался. Проверьте CSV, SQLite, доступ к Telegram и runtime/logs/bot.log; секреты не выводятся.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
