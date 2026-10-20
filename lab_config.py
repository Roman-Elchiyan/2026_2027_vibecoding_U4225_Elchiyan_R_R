"""Lab 2/3 options layered over the unchanged Lab 1 configuration contract."""
from dataclasses import dataclass, field, replace
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv, dotenv_values
from config import Config, ConfigError, ROOT, load_config, project_path


@dataclass(frozen=True)
class LabConfig(Config):
    stage: str = "Lab 3"
    external_sources_enabled: bool = True
    external_semantic_enabled: bool = True
    external_semantic_timeout: float = 12.0
    ai_enabled: bool = False
    openalex_key: str = field(default="", repr=False)
    contact_email: str = ""
    core_api_key: str = field(default="", repr=False)
    zenodo_api_token: str = field(default="", repr=False)
    ncbi_api_key: str = field(default="", repr=False)
    un_comtrade_api_key_1: str = field(default="", repr=False)
    un_comtrade_api_key_2: str = field(default="", repr=False)
    un_comtrade_key_slot: int = 1
    unpaywall_email: str = ""
    api_page_size: int = 30
    api_max_pages: int = 2
    api_request_budget: int = 48
    access_lookup_limit: int = 5
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
    query_variants: int = 3
    api_candidates: int = 30
    search_candidates: int = 180
    search_timeout: float = 35.0
    enrich_dois: int = 4
    reranker_enabled: bool = True
    reranker_model_dir: Path = ROOT / 'runtime/models/mmarco-mMiniLMv2-L12-H384-v1'
    reranker_revision: str = '2a0ed9295b28874866ea121f341ed150267f86a3'
    reranker_device: str = 'cpu'
    reranker_candidates: int = 60
    reranker_batch_size: int = 4
    reranker_max_length: int = 384
    reranker_timeout: float = 12.0
    reranker_query_language: str = 'original'
    relevance_calibration: Path = ROOT / 'runtime/relevance-calibration.json'


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
                        ("stage", "external_sources_enabled", "ai_enabled", "external_semantic_enabled")},
        external_sources_enabled=external if external is not None else flag("ENABLE_EXTERNAL_SOURCES", True),
        external_semantic_enabled=flag('ENABLE_EXTERNAL_SEMANTIC_RANKING', True),
        external_semantic_timeout=number('EXTERNAL_SEMANTIC_TIMEOUT_SECONDS',12.0,0.1,60),
        ai_enabled=flag("ENABLE_AI", False),
        openalex_key=os.getenv("OPENALEX_API_KEY", ""), contact_email=os.getenv("CROSSREF_CONTACT_EMAIL", ""),
        core_api_key=os.getenv('CORE_API_KEY', ''),
        zenodo_api_token=os.getenv('ZENODO_API_TOKEN', ''),
        ncbi_api_key=os.getenv('NCBI_API_KEY', ''),
        un_comtrade_api_key_1=os.getenv('UN_COMTRADE_API_KEY_1', ''),
        un_comtrade_api_key_2=os.getenv('UN_COMTRADE_API_KEY_2', ''),
        un_comtrade_key_slot=number('UN_COMTRADE_KEY_SLOT', 1, 1, 2),
        unpaywall_email=os.getenv('UNPAYWALL_EMAIL', os.getenv('CROSSREF_CONTACT_EMAIL', '')),
        api_page_size=number('API_PAGE_SIZE', 30, 1, 100),
        api_max_pages=number('API_MAX_PAGES', 2, 1, 5),
        api_request_budget=number('API_REQUEST_BUDGET', 48, 1, 200),
        access_lookup_limit=number('ACCESS_LOOKUP_LIMIT', 5, 0, 20),
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
        query_variants=number('SEARCH_QUERY_VARIANTS', 3, 1, 4),
        api_candidates=number('SEARCH_API_CANDIDATES', 30, 5, 100),
        search_candidates=number('SEARCH_CANDIDATES', 180, 20, 400),
        search_timeout=number('SEARCH_TIMEOUT_SECONDS', 35.0, 1, 120),
        enrich_dois=number('SEARCH_ENRICH_DOIS', 4, 0, 10),
        reranker_enabled=flag('ENABLE_CROSS_ENCODER', True),
        reranker_model_dir=project_path(os.getenv('CROSS_ENCODER_MODEL_DIR', 'runtime/models/mmarco-mMiniLMv2-L12-H384-v1')),
        reranker_revision=os.getenv('CROSS_ENCODER_REVISION', LabConfig.reranker_revision),
        reranker_device=os.getenv('CROSS_ENCODER_DEVICE', 'cpu'),
        reranker_candidates=number('CROSS_ENCODER_CANDIDATES', 60, 5, 100),
        reranker_batch_size=number('CROSS_ENCODER_BATCH_SIZE', 4, 1, 8),
        reranker_max_length=number('CROSS_ENCODER_MAX_LENGTH', 384, 64, 512),
        reranker_timeout=number('CROSS_ENCODER_TIMEOUT_SECONDS', 12.0, 0.1, 120),
        reranker_query_language=os.getenv('CROSS_ENCODER_QUERY_LANGUAGE','original'),
        relevance_calibration=project_path(os.getenv('RELEVANCE_CALIBRATION', 'runtime/relevance-calibration.json')),
        run_mode=mode or os.getenv("BOT_RUN_MODE", "polling"),
        webhook_host=os.getenv("WEBHOOK_LISTEN_HOST", "127.0.0.1"),
        webhook_port=number("WEBHOOK_PORT", 8080, 1024, 65535),
        webhook_path=os.getenv("WEBHOOK_PATH", ""), webhook_secret=os.getenv("WEBHOOK_SECRET_TOKEN", ""),
        ngrok_url=os.getenv("NGROK_PUBLIC_URL", "").rstrip("/"))
    if config.run_mode not in ("polling", "webhook"):
        raise ConfigError("BOT_RUN_MODE: polling или webhook.")
    if config.reranker_device not in ('cpu', 'auto', 'cuda'):
        raise ConfigError('CROSS_ENCODER_DEVICE: cpu, auto или cuda.')
    if config.reranker_query_language not in ('original','translated'):
        raise ConfigError('CROSS_ENCODER_QUERY_LANGUAGE: original или translated.')
    if urlparse(config.litellm_url).hostname not in ("127.0.0.1", "localhost"):
        raise ConfigError("LiteLLM должен быть доступен только локально.")
    if config.primary_model != "research-basic" or config.fallback_model != "research-complex":
        raise ConfigError("Допустимы только алиасы research-basic / research-complex.")
    return config
