"""Every administrative entry point checks server-side permissions."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import Config


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
        ]))


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application.bot_data["controller"]
    query = update.callback_query
    user_id = app.register(update)
    if not is_admin(user_id, app.config):
        await query.answer("Доступ запрещён.", show_alert=True)
        return
    await query.answer()
    if query.data == "admin:status":
        await query.message.reply_text(app.status_text())
    elif query.data == "admin:stats":
        counts = app.db.counts()
        await query.message.reply_text(f"SQLite: пользователей {counts['users']}, подборок {counts['searches']}, избранных материалов {counts['favorites']}.")
    else:
        await query.message.reply_text("Управление пользователями, пауза, лимиты и заявки будут доступны в Lab 3.")
