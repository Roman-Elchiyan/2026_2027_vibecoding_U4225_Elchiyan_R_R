"""Local configuration; only Telegram credentials are required for polling."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


class ConfigError(ValueError):
    pass


def project_path(value: str) -> Path:
    path = Path(value)
    resolved = (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    if not resolved.is_relative_to(ROOT):
        raise ConfigError("Пути данных и runtime должны находиться внутри репозитория.")
    return resolved


@dataclass(frozen=True)
class Config:
    bot_token: str = field(default="", repr=False)
    admin_telegram_ids: frozenset[int] = frozenset()
    db_path: Path = ROOT / "runtime/bot.db"
    csv_path: Path = ROOT / "data/ploswater.csv"
    log_path: Path = ROOT / "runtime/logs/bot.log"
    stage: str = "Lab 1"
    external_sources_enabled: bool = False
    ai_enabled: bool = False
    page_size: int = 5


def load_config(require_token: bool = True) -> Config:
    load_dotenv(ROOT / ".env", override=False)
    token = os.getenv("BOT_TOKEN", "").strip()
    if require_token and (not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token)):
        raise ConfigError("Задайте BOT_TOKEN из BotFather в .env. Значение токена не выводится.")
    try:
        admins = frozenset(int(part.strip()) for part in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if part.strip())
        if any(user <= 0 for user in admins):
            raise ValueError
    except ValueError:
        raise ConfigError("ADMIN_TELEGRAM_IDS: нужны положительные числовые ID через запятую.") from None
    if os.getenv("BOT_STAGE", "lab1").casefold() != "lab1":
        raise ConfigError("В этой версии поддерживается только BOT_STAGE=lab1.")
    for flag in ("ENABLE_EXTERNAL_SOURCES", "ENABLE_AI"):
        if os.getenv(flag, "false").casefold() not in ("false", "0", "no", ""):
            raise ConfigError(f"{flag} должен быть false: функция пока недоступна в Lab 1.")
    return Config(
        bot_token=token,
        admin_telegram_ids=admins,
        db_path=project_path(os.getenv("SQLITE_PATH", "runtime/bot.db")),
        csv_path=project_path(os.getenv("CSV_PATH", "data/ploswater.csv")),
        log_path=project_path(os.getenv("LOG_PATH", "runtime/logs/bot.log")),
    )
