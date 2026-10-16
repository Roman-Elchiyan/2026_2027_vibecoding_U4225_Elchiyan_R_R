"""Run polling with `py bot.py`; offline diagnostics with `py bot.py --check`."""

from __future__ import annotations

import argparse
import asyncio
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
import weakref

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
        result = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", result)
        return re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}", "[REDACTED]", result)


def configure_logging(config: Config) -> None:
    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    secret_values = tuple(value for key, value in os.environ.items() if any(word in key.upper() for word in ("TOKEN", "API_KEY", "MASTER_KEY", "SALT_KEY", "PASSWORD", "SECRET", "WEBHOOK_PATH")))
    formatter = SafeFormatter(secret_values + (config.bot_token, getattr(config, 'webhook_path', ''), getattr(config, 'webhook_secret', '')))
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
            # Different users stay responsive while one user awaits E5; their own form steps stay ordered.
            locks = context.application.bot_data.setdefault("user_locks", weakref.WeakValueDictionary())
            lock = locks.setdefault(user_id, asyncio.Lock())
            async with lock:
                controller = context.application.bot_data.get("controller")
                database = getattr(controller, "db", None)
                persistent = hasattr(database, "claim_delivery") and update.update_id is not None
                if persistent and not database.claim_delivery(update.update_id):
                    if update.callback_query:
                        await update.callback_query.answer()
                    return
                try:
                    await callback(update, context)
                except (ValueError, LookupError, CatalogError):
                    if persistent: database.finish_delivery(update.update_id)
                    raise
                except BaseException:
                    if persistent: database.finish_delivery(update.update_id, False)
                    raise
                else:
                    if persistent: database.finish_delivery(update.update_id)
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
    controller = application.bot_data["controller"]
    controller.initialize_search()
    await asyncio.to_thread(controller.db.cleanup_history)
    if hasattr(controller.config, "run_mode"):
        from deployment import polling_ready, watch_tunnel
        application.bot_data['started_at'] = time.time()
        if controller.config.run_mode == "polling":
            await polling_ready(application.bot)
        else:
            application.bot_data['tunnel_monitor'] = asyncio.create_task(watch_tunnel(application, controller.config))
    commands = [
        ("start", "Главное меню"), ("help", "Помощь"), ("search", "Поиск материалов"),
        ("saved", "Избранное"), ("cancel", "Отменить ввод"), ("status", "Состояние бота"),
        ("ai", "Управление ИИ"), ("limits", "Бюджет ИИ"),
        ("feedback", "Оценка результата"), ("admin", "Администратор"),
    ]
    await application.bot.set_my_commands([BotCommand(name, description) for name, description in commands])
    logger.info("event=bot_ready stage=%s mode=%s", controller.config.stage, getattr(controller.config, 'run_mode', 'polling'))


async def post_shutdown(application: Application) -> None:
    monitor = application.bot_data.get('tunnel_monitor')
    if monitor:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
    controller = application.bot_data["controller"]
    if hasattr(controller, "close"):
        await controller.close()
    else:
        controller.search_service.close()


def build_application(config: Config, controller: Controller) -> Application:
    admin_cmd, admin_cb = admin_command, admin_callback
    if hasattr(controller, 'research'):
        from lab_admin import admin_command as admin_cmd, admin_callback as admin_cb
    application = Application.builder().token(config.bot_token).concurrent_updates(8).post_init(post_init).post_shutdown(post_shutdown).build()
    application.bot_data["controller"] = controller
    for command, callback in {
        "start": controller.start, "help": controller.help, "search": controller.search,
        "saved": controller.saved, "cancel": controller.cancel, "status": controller.status,
        "ai": getattr(controller, 'ai', controller.unavailable), "limits": getattr(controller, 'limits', controller.unavailable),
        "feedback": getattr(controller, 'feedback', controller.unavailable), "admin": admin_cmd,
    }.items():
        application.add_handler(CommandHandler(command, guarded(callback)))
    application.add_handler(CallbackQueryHandler(guarded(admin_cb), pattern=r"^admin:"))
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
    parser.add_argument("--stage", choices=("lab1", "lab2", "lab3"), default="lab3")
    parser.add_argument("--mode", choices=("polling", "webhook"))
    parser.add_argument("--external", action="store_true", help="Включить внешние научные API")
    args = parser.parse_args()
    try:
        if args.stage == 'lab1':
            config = load_config(require_token=not args.check)
        else:
            from lab_config import load_lab_config
            config = load_lab_config(require_token=not args.check, mode=args.mode, external=True if args.external else None)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        configure_logging(config)
        database_class = Database
        controller_class = Controller
        if args.stage != 'lab1':
            from lab_storage import LabDatabase
            from lab_handlers import LabController
            database_class, controller_class = LabDatabase, LabController
        database = database_class(config.db_path, config.history_max_per_user, config.history_days)
        database.initialize()
        source = CsvSource(config.csv_path)
        controller = controller_class(config, database, source)
        if args.check:
            print(controller.status_text())
            source.load()
            if hasattr(controller, 'close'): asyncio.run(controller.close())
            return 0 if database.health() else 1
        source.load()  # Fail early on a missing catalog, before contacting Telegram.
        from deployment import single_instance, tunnel_url
        from dataclasses import replace
        with single_instance():
            mode = getattr(config, 'run_mode', 'polling')
            if mode == 'webhook':
                try:
                    public_url = asyncio.run(tunnel_url(config))
                except ConfigError:
                    logger.warning("event=ngrok_unavailable fallback=polling")
                    mode = 'polling'
                    config = replace(config, run_mode='polling')
                    controller.config = config
            # asyncio.run() closes the previous event loop; PTB needs its own loop on Windows.
            asyncio.set_event_loop(asyncio.new_event_loop())
            app = build_application(config, controller)
            logger.info("event=bot_starting stage=%s records=%d mode=%s", config.stage, source.row_count, mode)
            if mode == 'webhook':
                try:
                    app.run_webhook(listen=config.webhook_host, port=config.webhook_port,
                        url_path=config.webhook_path, webhook_url=public_url+'/'+config.webhook_path,
                        secret_token=config.webhook_secret, allowed_updates=["message", "callback_query"],
                        drop_pending_updates=False)
                except Exception:
                    logger.error("event=webhook_start_failed fallback=polling")
                    app.bot_data['webhook_failed'] = True
                if app.bot_data.get('webhook_failed'):
                    config = replace(config, run_mode='polling')
                    controller = controller_class(config, database, source)
                    asyncio.set_event_loop(asyncio.new_event_loop())
                    app = build_application(config, controller)
                    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)
            else:
                app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)
        return 0
    except Exception:
        logger.exception("event=startup_failed")
        print("Запуск не удался. Проверьте CSV, SQLite, доступ к Telegram и runtime/logs/bot.log; секреты не выводятся.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
