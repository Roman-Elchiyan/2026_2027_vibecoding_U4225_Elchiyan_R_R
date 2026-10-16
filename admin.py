"""Every administrative entry point checks server-side permissions."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import Config
import asyncio


def semantic_buttons(app):
    return [
        [InlineKeyboardButton("Включить локальную E5", callback_data="admin:local:on"),
         InlineKeyboardButton("Отключить локальную E5", callback_data="admin:local:off")],
        [InlineKeyboardButton("E5 внешних API: вкл. (Lab 2)", callback_data="admin:external:on"),
         InlineKeyboardButton("E5 внешних API: выкл. (Lab 2)", callback_data="admin:external:off")],
    ]


def is_admin(user_id: int, config: Config) -> bool:
    return user_id in config.admin_telegram_ids


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application.bot_data["controller"]
    user_id = app.register(update)
    if not is_admin(user_id, app.config):
        await update.effective_message.reply_text("Доступ к /admin разрешён только администратору.")
        return
    await update.effective_message.reply_text("Диагностика Lab 1. Управление пользователями, пауза, лимиты и заявки будут доступны в Lab 3.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Проверить состояние", callback_data="admin:status")],
            [InlineKeyboardButton("Статистика SQLite", callback_data="admin:stats")],
            [InlineKeyboardButton("Управление — пока недоступно (Lab 3)", callback_data="admin:future")],
        ] + semantic_buttons(app)))


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application.bot_data["controller"]
    query = update.callback_query
    user_id = app.register(update)
    if not is_admin(user_id, app.config):
        await query.answer("Доступ запрещён.", show_alert=True)
        return
    await query.answer()
    if query.data in ("admin:local:on", "admin:local:off", "admin:external:on", "admin:external:off"):
        # Loading the model is deferred to the worker, including when switching on here.
        if app.search_service.semantic is None:
            from semantic_search import SemanticSearch
            app.search_service.semantic = SemanticSearch(app.source, app.config, app.db)
        manager = app.search_service.semantic
        _, scope, setting = query.data.split(":")
        manager.set_enabled(setting == "on", external=scope == "external")
        text = (f"E5 локального каталога: {manager.state()}.\n"
                f"Семантическое ранжирование внешних API (Lab 2): {'включено' if manager.external_enabled else 'отключено'}. "
                "Внешние API пока не подключены. Настройка сохранена.")
        await app.present(query.message, text, InlineKeyboardMarkup(semantic_buttons(app)), edit=True)
    elif query.data == "admin:status":
        await query.message.reply_text(await asyncio.to_thread(app.status_text))
    elif query.data == "admin:stats":
        counts = app.db.counts()
        await query.message.reply_text(f"SQLite: пользователей {counts['users']}, подборок {counts['searches']}, избранных материалов {counts['favorites']}.")
    else:
        await query.message.reply_text("Управление пользователями, пауза, лимиты и заявки будут доступны в Lab 3.")
