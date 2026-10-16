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
    semantic_enabled: bool = True
    # Reserved for Lab 2; no external adapters are connected here.
    external_semantic_enabled: bool = False
    semantic_model: str = "intfloat/multilingual-e5-small"
    semantic_revision: str = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
    semantic_index_dir: Path = ROOT / "runtime/semantic"
    semantic_timeout: float = 3.0
    semantic_build_timeout: float = 180.0
    semantic_batch_size: int = 4
    semantic_cpu_threads: int = 4
    semantic_min_score: float = 0.83
    semantic_score_margin: float = 0.018
    semantic_candidates: int = 20
    fuzzy_min_score: float = 88.0
    rrf_k: int = 60
    fts_weight: float = 1.0
    fuzzy_weight: float = 0.5
    semantic_weight: float = 1.0
    history_max_per_user: int = 50
    history_days: int = 30


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
    def boolean(name: str, default: bool) -> bool:
        value = os.getenv(name, str(default)).casefold()
        if value not in ("true", "false", "1", "0", "yes", "no"):
            raise ConfigError(f"{name}: ожидается true или false.")
        return value in ("true", "1", "yes")

    def number(name: str, default, minimum, maximum):
        try:
            value = type(default)(os.getenv(name, str(default)))
            if not minimum <= value <= maximum:
                raise ValueError
            return value
        except ValueError:
            raise ConfigError(f"{name}: ожидается число от {minimum} до {maximum}.") from None

    index_dir = project_path(os.getenv("SEMANTIC_INDEX_DIR", "runtime/semantic"))
    if not index_dir.is_relative_to(ROOT / "runtime"):
        raise ConfigError("SEMANTIC_INDEX_DIR должен находиться внутри runtime/.")

    return Config(
        bot_token=token,
        admin_telegram_ids=admins,
        db_path=project_path(os.getenv("SQLITE_PATH", "runtime/bot.db")),
        csv_path=project_path(os.getenv("CSV_PATH", "data/ploswater.csv")),
        log_path=project_path(os.getenv("LOG_PATH", "runtime/logs/bot.log")),
        semantic_enabled=boolean("ENABLE_SEMANTIC_SEARCH", True),
        external_semantic_enabled=boolean("ENABLE_EXTERNAL_SEMANTIC_RANKING", False),
        semantic_model=os.getenv("SEMANTIC_MODEL", Config.semantic_model),
        semantic_revision=os.getenv("SEMANTIC_REVISION", Config.semantic_revision),
        semantic_index_dir=index_dir,
        semantic_timeout=number("SEMANTIC_TIMEOUT_SECONDS", 3.0, 0.01, 60),
        semantic_build_timeout=number("SEMANTIC_BUILD_TIMEOUT_SECONDS", 180.0, 1, 3600),
        semantic_batch_size=number("SEMANTIC_BATCH_SIZE", 4, 1, 16),
        semantic_cpu_threads=number("SEMANTIC_CPU_THREADS", 4, 1, 32),
        semantic_min_score=number("SEMANTIC_MIN_SCORE", 0.83, 0, 1),
        semantic_score_margin=number("SEMANTIC_SCORE_MARGIN", 0.018, 0, 1),
        semantic_candidates=number("SEMANTIC_CANDIDATES", 20, 1, 100),
        fuzzy_min_score=number("FUZZY_MIN_SCORE", 88.0, 50, 100),
        rrf_k=number("RRF_K", 60, 1, 1000),
        fts_weight=number("FTS_WEIGHT", 1.0, 0, 10),
        fuzzy_weight=number("FUZZY_WEIGHT", 0.5, 0, 10),
        semantic_weight=number("SEMANTIC_WEIGHT", 1.0, 0, 10),
        history_max_per_user=number("HISTORY_MAX_PER_USER", 50, 1, 10000),
        history_days=number("HISTORY_DAYS", 30, 1, 3650),
    )
