"""Lab 2/3 options layered over the unchanged Lab 1 configuration contract."""
from dataclasses import dataclass, field, replace
import os
import re
from urllib.parse import urlparse

from dotenv import load_dotenv, dotenv_values
from config import Config, ConfigError, ROOT, load_config


@dataclass(frozen=True)
class LabConfig(Config):
    stage: str = "Lab 3"
    external_sources_enabled: bool = True
    ai_enabled: bool = False
    openalex_key: str = field(default="", repr=False)
    contact_email: str = ""
    litellm_url: str = "http://127.0.0.1:4000"
    master_key: str = field(default="", repr=False)
    primary_model: str = "research-basic"
    fallback_model: str = "research-complex"
    sol_fallback: bool = False
    default_budget: float = 0.50
    admin_budget: float = 0.50
    max_output_tokens: int = 350
    llm_timeout: float = 20
    api_timeout: float = 12
    translation_enabled: bool = True
    translation_timeout: float = 12
    run_mode: str = "polling"
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8080
    webhook_path: str = field(default="", repr=False)
    webhook_secret: str = field(default="", repr=False)
    ngrok_url: str = ""


def load_lab_config(require_token=True, mode=None, external=None):
    load_dotenv(ROOT / ".env", override=False)
    for key,value in dotenv_values(ROOT / "runtime/recovered-template.env").items():
        if key in ("OPENALEX_API_KEY","CROSSREF_CONTACT_EMAIL") and value:
            os.environ.setdefault(key,value)
    load_dotenv(ROOT / "runtime/services.env", override=False)
    # Keep load_config() and the Lab 1 validation/tests backwards compatible.
    names = ("BOT_STAGE", "ENABLE_EXTERNAL_SOURCES", "ENABLE_AI")
    original = {name: os.environ.get(name) for name in names}
    try:
        os.environ.update(BOT_STAGE="lab1", ENABLE_EXTERNAL_SOURCES="false", ENABLE_AI="false")
        base = load_config(require_token)
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def flag(name, default):
        value = os.getenv(name, str(default)).lower()
        if value not in ("true", "false", "1", "0", "yes", "no"):
            raise ConfigError(f"{name}: ожидается true/false.")
        return value in ("true", "1", "yes")

    def number(name, default, lo, hi):
        try:
            value = type(default)(os.getenv(name, str(default)))
            if not lo <= value <= hi:
                raise ValueError
            return value
        except ValueError:
            raise ConfigError(f"{name}: ожидается число от {lo} до {hi}.") from None

    config = LabConfig(**{k: v for k, v in vars(base).items() if k not in
                        ("stage", "external_sources_enabled", "ai_enabled")},
        external_sources_enabled=external if external is not None else flag("ENABLE_EXTERNAL_SOURCES", True),
        ai_enabled=flag("ENABLE_AI", False),
        openalex_key=os.getenv("OPENALEX_API_KEY", ""), contact_email=os.getenv("CROSSREF_CONTACT_EMAIL", ""),
        litellm_url=os.getenv("LITELLM_URL", "http://127.0.0.1:4000").rstrip("/"),
        master_key=os.getenv("LITELLM_MASTER_KEY", ""),
        primary_model=os.getenv("LLM_PRIMARY_MODEL", "research-basic"),
        fallback_model=os.getenv("LLM_FALLBACK_MODEL", "research-complex"),
        sol_fallback=flag("ENABLE_SOL_FALLBACK", False),
        default_budget=number("DEFAULT_USER_BUDGET_USD", 0.50, 0.01, 10),
        admin_budget=number("ADMIN_ANALYSIS_BUDGET_USD", 0.50, 0.01, 10),
        max_output_tokens=number("MAX_AI_OUTPUT_TOKENS", 350, 32, 1000),
        llm_timeout=number("LLM_TIMEOUT_SECONDS", 20.0, 1, 120),
        api_timeout=number("API_TIMEOUT_SECONDS", 12.0, 0.1, 60),
        translation_enabled=flag("ENABLE_TRANSLATION", True),
        translation_timeout=number("TRANSLATION_TIMEOUT_SECONDS", 12.0, 0.1, 120),
        run_mode=mode or os.getenv("BOT_RUN_MODE", "polling"),
        webhook_host=os.getenv("WEBHOOK_LISTEN_HOST", "127.0.0.1"),
        webhook_port=number("WEBHOOK_PORT", 8080, 1024, 65535),
        webhook_path=os.getenv("WEBHOOK_PATH", ""), webhook_secret=os.getenv("WEBHOOK_SECRET_TOKEN", ""),
        ngrok_url=os.getenv("NGROK_PUBLIC_URL", "").rstrip("/"))
    if config.run_mode not in ("polling", "webhook"):
        raise ConfigError("BOT_RUN_MODE: polling или webhook.")
    if urlparse(config.litellm_url).hostname not in ("127.0.0.1", "localhost"):
        raise ConfigError("LiteLLM должен быть доступен только локально.")
    if config.primary_model != "research-basic" or config.fallback_model != "research-complex":
        raise ConfigError("Допустимы только алиасы research-basic / research-complex.")
    return config
