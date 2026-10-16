"""Telegram navigation, persistent input steps, cards and owned result pages."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from html import escape
from time import perf_counter

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ai import AI_UNAVAILABLE
from budget import LIMITS_UNAVAILABLE
from config import Config
from db import Database
from search import Material, SearchFilters, SearchService, deduplicate
from sources import CatalogError, CsvSource, safe_url

logger = logging.getLogger(__name__)
SEARCH = "🔎 Поиск материалов"
SAVED = "⭐ Избранное"
AI = "🤖 ИИ-поиск"
LIMITS = "📊 Мои лимиты"
FEEDBACK = "📝 Обратная связь"
HELP = "❔ Помощь"
FEEDBACK_UNAVAILABLE = "Оценки и отзывы будут доступны в Lab 3."
MAIN_MENU = ReplyKeyboardMarkup([[SEARCH, SAVED], [AI, LIMITS], [FEEDBACK, HELP]], resize_keyboard=True)
HELP_TEXT = (
    "«Brilliant» detective — Lab 1\n"
    "Локальный каталог PLOS Water: поиск в заголовках и ключевых словах на языке исходных данных (английский). "
    "Все слова запроса должны встретиться в заголовке или ключевых словах. DOI ищется точно.\n\n"
    "/start — главное меню\n/search — запрос → фильтры → Искать\n/saved — избранное\n"
    "/cancel — отменить текущий ввод\n/status — проверка CSV и SQLite\n/help — помощь\n"
    "/admin — диагностика для администратора\n"
    "/ai, /limits — появятся в Lab 2\n/feedback — появится в Lab 3\n\n"
    "В выдаче до 5 карточек на страницу. Избранное, фильтры и кнопки выдач сохраняются после перезапуска. "
    "Пример темы: climate change. Аннотации, язык и подтверждённый открытый доступ в CSV отсутствуют."
)


def clipped(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit - 1] + "…"


def html_text(value: str, budget: int) -> str:
    pieces = []
    length = 0
    for character in value:
        encoded = escape(character)
        units = len(encoded.encode("utf-16-le")) // 2
        if length + units > budget - 1:
            pieces.append("…")
            break
        pieces.append(encoded)
        length += units
    return "".join(pieces)


def format_card(item: Material, number: int) -> str:
    # Bound escaped HTML too: a single '&' expands to five characters.
    lines = [f"<b>{number}. {html_text(item.title, 900)}</b>"]
    if item.authors:
        label = "Первый автор (CSV)" if item.metadata.get("first_author_name") else "Авторы"
        lines.append(f"{label}: {html_text(', '.join(item.authors), 280)}")
    lines.append(f"Год: {item.year if item.year is not None else 'нет данных'}")
    if item.material_type:
        lines.append(f"Тип: {html_text(item.material_type, 160)}")
    lines.append(f"Источник: {html_text(item.source, 180)}")
    if item.doi:
        lines.append(f"DOI: {html_text(item.doi, 400)}")
    url = safe_url(item.url)
    if url and len(escape(url, quote=True)) <= 700:
        lines.append(f'<a href="{escape(url, quote=True)}">Ссылка на публикацию</a>')
    keywords = item.metadata.get("keywords")
    if keywords:
        lines.append(f"Ключевые слова (CSV): {html_text(keywords, 480)}")
    lines.append("Аннотация и сведения об открытом доступе в CSV не указаны.")
    return "\n".join(lines)


def card_keyboard(item: Material, search_id: str, position: int, saved: bool) -> InlineKeyboardMarkup:
    action = "r" if saved else "a"
    text = "Убрать из избранного" if saved else "Сохранить"
    rows = [[InlineKeyboardButton(text, callback_data=f"f:{action}:{search_id}:{position}")]]
    url = safe_url(item.url) or safe_url("https://doi.org/" + item.doi if item.doi else None)
    if url and len(url) <= 1500:
        rows.append([InlineKeyboardButton("Открыть источник", url=url)])
    return InlineKeyboardMarkup(rows)


class Controller:
    def __init__(self, config: Config, db: Database, source: CsvSource):
        self.config = config
        self.db = db
        self.source = source
        self.search_service = SearchService(source)

    def register(self, update: Update) -> int:
        if update.effective_user is None:
            raise ValueError("Не удалось определить пользователя.")
        user_id = update.effective_user.id
        self.db.register(user_id)
        return user_id

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = self.register(update)
        filters, _ = self.db.get_settings(user_id)
        self.db.set_settings(user_id, filters)
        await update.effective_message.reply_text(
            "«Brilliant» detective\nИщу реальные публикации в локальном CSV PLOS Water. "
            "Выберите «Поиск материалов». /help — команды и возможности Lab 1.", reply_markup=MAIN_MENU)

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.register(update)
        await update.effective_message.reply_text(HELP_TEXT, reply_markup=MAIN_MENU)

    async def search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = self.register(update)
        filters, _ = self.db.get_settings(user_id)
        self.db.set_settings(user_id, filters, "topic")
        await update.effective_message.reply_text(
            "Введите тему, название, DOI или https://doi.org/… (до 500 символов). "
            "Например: climate change. Отмена: /cancel.", reply_markup=MAIN_MENU)

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = self.register(update)
        filters, _ = self.db.get_settings(user_id)
        self.db.set_settings(user_id, filters)
        await update.effective_message.reply_text("Ввод отменён. Сохранённые фильтры доступны в /search.", reply_markup=MAIN_MENU)

    async def unavailable(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.register(update)
        command = update.effective_message.text.split()[0].split("@")[0]
        message = AI_UNAVAILABLE if command in (AI.split()[0], "/ai") else LIMITS_UNAVAILABLE
        if command in (FEEDBACK.split()[0], "/feedback"):
            message = FEEDBACK_UNAVAILABLE
        await update.effective_message.reply_text(message, reply_markup=MAIN_MENU)

    def status_text(self) -> str:
        lines = ["Этап: Lab 1. Процесс бота запущен."]
        try:
            lines.append("SQLite: проверка пройдена." if self.db.health() else "SQLite: проверка не пройдена.")
        except (sqlite3.Error, OSError):
            logger.exception("event=sqlite_health_failed")
            lines.append("SQLite: не удалось открыть или проверить БД.")
        try:
            items = self.source.load()
            lines.append(f"CSV: существует и прочитан; строк {self.source.row_count}, материалов {len(deduplicate(items))}.")
        except CatalogError:
            logger.exception("event=csv_health_failed")
            lines.append("CSV: отсутствует, повреждён или не читается. Проверьте data/ploswater.csv.")
        lines.extend(["Внешние API и ИИ: не подключены.", "Telegram: отдельная проверка соединения getMe не выполнялась."])
        return "\n".join(lines)

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            self.register(update)
        except (sqlite3.Error, OSError):
            logger.exception("event=status_registration_failed")
        await update.effective_message.reply_text(self.status_text())

    async def saved(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = self.register(update)
        await self.render_page(update.effective_message, user_id, self.db.saved_search(user_id), 0)

    async def form(self, message, user_id: int) -> None:
        filters, _ = self.db.get_settings(user_id)
        years = f"{filters.year_from or '…'}–{filters.year_to or '…'}" if filters.year_from or filters.year_to else "любые"
        text = f"Запрос: {filters.topic or 'не задан'}\nГоды: {years}\nТип: {', '.join(filters.material_types) or 'любой'}\nИсточник: локальный CSV."
        rows = [
            [InlineKeyboardButton("Искать", callback_data="form:run")],
            [InlineKeyboardButton("Изменить запрос", callback_data="form:topic"), InlineKeyboardButton("Годы", callback_data="form:years")],
            [InlineKeyboardButton("Тип материала", callback_data="form:types"), InlineKeyboardButton("Сбросить фильтры", callback_data="form:reset")],
            [InlineKeyboardButton("Язык — пока недоступно", callback_data="future:language")],
            [InlineKeyboardButton("Открытый доступ — пока недоступно", callback_data="future:open")],
            [InlineKeyboardButton("Внешние источники — пока недоступно (Lab 2)", callback_data="future:sources")],
            [InlineKeyboardButton("ИИ и сортировка — пока недоступно (Lab 2)", callback_data="future:ai")],
            [InlineKeyboardButton("Отменить ввод", callback_data="form:cancel")],
        ]
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (update.effective_message.text or "").strip()
        actions = {SEARCH: self.search, SAVED: self.saved, HELP: self.help, AI: self.unavailable,
                   LIMITS: self.unavailable, FEEDBACK: self.unavailable}
        if text in actions:
            await actions[text](update, context)
            return
        user_id = self.register(update)
        filters, state = self.db.get_settings(user_id)
        if state == "topic":
            if not text or len(text) > 500:
                raise ValueError("Введите запрос длиной от 1 до 500 символов или /cancel.")
            filters.topic = text
        elif state == "years":
            if text.casefold() in ("нет", "любой", "сброс"):
                filters.year_from = filters.year_to = None
            else:
                match = re.fullmatch(r"(\d{4})(?:\s*[-–—]\s*(\d{4}))?", text)
                if not match:
                    raise ValueError("Введите YYYY или YYYY-YYYY, например 2022-2024; «нет» — снять фильтр.")
                filters.year_from = int(match[1])
                filters.year_to = int(match[2] or match[1])
                if filters.year_from > filters.year_to or filters.year_from < 1000:
                    raise ValueError("Некорректный диапазон: начальный год должен быть не больше конечного (YYYY-YYYY).")
        else:
            await update.effective_message.reply_text("Начните поиск командой /search или выберите пункт меню.", reply_markup=MAIN_MENU)
            return
        self.db.set_settings(user_id, filters, "form")
        await self.form(update.effective_message, user_id)

    async def render_page(self, message, user_id: int, search_id: str, page: int) -> None:
        record = self.db.get_search(user_id, search_id)
        items = self.db.get_page(user_id, search_id, page)
        total = record["total_count"]
        if not total:
            text = "Избранное пока пусто. Сохраните карточки из /search." if record["kind"] == "saved" else "Ничего не найдено. Измените запрос или фильтры: /search."
            await message.reply_text(text, reply_markup=MAIN_MENU)
            return
        pages = (total + 4) // 5
        title = "Избранное (снимок)" if record["kind"] == "saved" else "Результаты"
        await message.reply_text(f"{title}: {total}. Страница {page + 1}/{pages}.")
        for warning in json.loads(record["warnings_json"]):
            await message.reply_text(clipped(warning, 1000))
        for position, item in items:
            await message.reply_text(format_card(item, position + 1), parse_mode="HTML", disable_web_page_preview=True,
                reply_markup=card_keyboard(item, search_id, position, self.db.is_favorite(user_id, item.id)))
        buttons = []
        if page > 0:
            buttons.append(InlineKeyboardButton("Назад", callback_data=f"p:{search_id}:{page - 1}"))
        if page + 1 < pages:
            buttons.append(InlineKeyboardButton("Следующая страница", callback_data=f"p:{search_id}:{page + 1}"))
        if buttons:
            await message.reply_text("Страницы подборки:", reply_markup=InlineKeyboardMarkup([buttons]))

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user_id = self.register(update)
        data = query.data or ""
        await query.answer()
        if data.startswith("future:"):
            await query.message.reply_text("Этот параметр пока недоступен в Lab 1 и не применяется. Внешние источники и ИИ появятся в Lab 2.")
        elif data.startswith("form:"):
            action = data[5:]
            filters, state = self.db.get_settings(user_id)
            if action == "cancel":
                await self.cancel(update, context)
            elif action in ("topic", "years"):
                self.db.set_settings(user_id, filters, action)
                await query.message.reply_text("Введите запрос (до 500 символов). /cancel — отменить." if action == "topic" else "Введите год YYYY или диапазон YYYY-YYYY; «нет» — снять фильтр. /cancel — отменить.")
            elif action == "reset":
                self.db.set_settings(user_id, SearchFilters(topic=filters.topic), "form")
                await self.form(query.message, user_id)
            elif action == "types":
                items = self.source.load()
                if not self.source.supports_types:
                    await query.message.reply_text("Тип материала пока недоступен: в CSV нет соответствующего столбца.")
                    return
                types = sorted({item.material_type for item in items if item.material_type})
                rows = [[InlineKeyboardButton("Любой тип", callback_data="type:all")]]
                rows.extend([InlineKeyboardButton(value, callback_data=f"type:{index}")] for index, value in enumerate(types))
                await query.message.reply_text("Выберите тип из CSV:", reply_markup=InlineKeyboardMarkup(rows))
            elif action == "run":
                if state not in ("form", "idle"):
                    raise ValueError("Сначала завершите текущий ввод или выполните /cancel.")
                started = perf_counter()
                result = self.search_service.search(filters)
                self.db.save_search(user_id, filters, result)
                self.db.set_settings(user_id, filters)
                logger.info("event=search_completed search=%s count=%d elapsed_ms=%.1f", result.search_id, result.total_count, (perf_counter() - started) * 1000)
                await self.render_page(query.message, user_id, result.search_id, 0)
            else:
                raise ValueError("Неизвестная кнопка. Начните заново: /search.")
        elif data.startswith("type:"):
            filters, _ = self.db.get_settings(user_id)
            items = self.source.load()
            types = sorted({item.material_type for item in items if item.material_type})
            value = data[5:]
            if value == "all":
                filters.material_types = ()
            elif value.isdigit() and int(value) < len(types):
                filters.material_types = (types[int(value)],)
            else:
                raise ValueError("Тип больше недоступен. Выберите его заново.")
            self.db.set_settings(user_id, filters, "form")
            await self.form(query.message, user_id)
        elif re.fullmatch(r"p:[0-9a-f]{32}:\d{1,8}", data):
            _, search_id, page = data.split(":")
            await self.render_page(query.message, user_id, search_id, int(page))
        elif re.fullmatch(r"f:[ar]:[0-9a-f]{32}:\d{1,8}", data):
            _, action, search_id, position = data.split(":")
            item = self.db.set_favorite(user_id, search_id, int(position), action == "a")
            await query.edit_message_reply_markup(reply_markup=card_keyboard(item, search_id, int(position), self.db.is_favorite(user_id, item.id)))
        else:
            raise ValueError("Кнопка устарела или недоступна. Выполните /search или /saved.")

    async def unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.register(update)
        await update.effective_message.reply_text("Неизвестная команда или формат сообщения. /help — список команд.", reply_markup=MAIN_MENU)
