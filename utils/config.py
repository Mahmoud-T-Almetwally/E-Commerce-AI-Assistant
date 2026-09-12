import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from utils.exceptions import ProviderAPIKeyNotFound, ConfigurationError

logger = logging.getLogger(__name__)

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PROVIDER_API_KEY_MAP: Dict[str, Optional[str]] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
    "google": "GOOGLE_API_KEY",
    "huggingface": "HUGGINGFACEHUB_API_TOKEN",
    "local": None,
}


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _require_api_key(provider: str, explicit_key: Optional[str]) -> Optional[str]:
    """
    Resolves the API key when needed, so the web app can
    boot even when no AI provider key is configured.
    Raises ProviderAPIKeyNotFound only when the key is actually needed.
    """
    if explicit_key:
        return explicit_key
    env_var = PROVIDER_API_KEY_MAP.get(provider.lower())
    if env_var is None:
        return None  # 'local' provider needs no key
    key = os.environ.get(env_var)
    if not key:
        raise ProviderAPIKeyNotFound(provider=provider, env_var_name=env_var)
    return key


class FlaskConfig(BaseModel):
    secret_key: str = Field(default_factory=lambda: os.environ.get(
        "FLASK_SECRET_KEY", "dev-secret-key-change-in-production"))
    host: str = "127.0.0.1"
    port: int = 5000
    debug: bool = Field(default_factory=lambda: _env_flag("FLASK_DEBUG"))

    # Session hardening
    session_cookie_httponly: bool = True
    session_cookie_samesite: str = "Lax"
    session_cookie_secure: bool = Field(default_factory=lambda: _env_flag("FLASK_SECURE_COOKIES"))
    permanent_session_lifetime_minutes: int = Field(default=720)


class RAGConfig(BaseModel):
    collection_name: str = "ecommerce_knowledge"
    max_file_size_mb: int = Field(default=10)
    max_content_chars: int = Field(default=200_000)
    supported_extensions: List[str] = Field(default=[".txt", ".pdf", ".docx"])
    chunk_size: int = 1000
    chunk_overlap: int = 200
    sync_on_startup: bool = True
    separators: List[str] = Field(default_factory=lambda: ["\n\n", "\n", ".", " ", ""])


class MetaConfig(BaseModel):
    enabled: bool = False
    verify_token: Optional[str] = Field(default_factory=lambda: os.environ.get("META_VERIFY_TOKEN"))
    page_access_token: Optional[str] = Field(default_factory=lambda: os.environ.get("META_PAGE_ACCESS_TOKEN"))
    app_secret: Optional[str] = Field(default_factory=lambda: os.environ.get("META_APP_SECRET"))


class DatabaseConfig(BaseModel):
    url: str = Field(default_factory=lambda: os.environ.get("DATABASE_URL") or f"sqlite:///{(PROJECT_ROOT / 'instance' / 'ecommerce.db').as_posix()}")
    chroma_persist_directory: str = Field(default_factory=lambda: os.environ.get("CHROMA_DB_DIR", str(PROJECT_ROOT / "instance" / "chroma_db")))
    checkpoint_path: str = Field(default_factory=lambda: os.environ.get("LANGGRAPH_CHECKPOINT_PATH", str(PROJECT_ROOT / "instance" / "checkpoints.sqlite")))
    echo_queries: bool = False
    connect_args: Dict[str, Any] = Field(default_factory=lambda: {"check_same_thread": False})


class EmbeddingConfig(BaseModel):
    provider: Literal["openai", "huggingface", "google", "local"] = "openai"
    model_name: str = "text-embedding-3-small"
    api_key: Optional[str] = None

    def get_api_key(self) -> Optional[str]:
        return _require_api_key(self.provider, self.api_key)


class LLMConfig(BaseModel):
    provider: Literal["openai", "anthropic", "groq", "google", "huggingface", "local"] = "openai"
    model_name: str = "gpt-4o-mini"
    temperature: float = 0.2
    max_tokens: int = 2048
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    seed: Optional[int] = None
    max_retries: int = Field(default=3, ge=0, le=10)
    timeout_seconds: float = Field(default=60.0, gt=0)
    vision_capable: bool = Field(default=False)
    api_key: Optional[str] = None

    def get_api_key(self) -> Optional[str]:
        return _require_api_key(self.provider, self.api_key)


class AgentRuntimeConfig(BaseModel):
    """Tunables for the agent graph itself (see agent/graph.py, agent/tooling.py)."""
    guard_enabled: bool = True
    guard_include_context: bool = True
    max_tool_retries: int = Field(default=2, ge=0, le=5)
    tool_retry_backoff_seconds: float = Field(default=0.5, ge=0)
    status_events_enabled: bool = True
    sensitive_tool_names: List[str] = Field(default_factory=lambda: ["add_to_cart", "checkout"])
    confirmation_timeout_seconds: int = Field(default=600, gt=0)
    max_upload_mb: int = Field(default=5, gt=0)
    max_message_chars: int = Field(default=8_000, gt=0)
    allowed_upload_extensions: List[str] = Field(
        default_factory=lambda: [".png", ".jpg", ".jpeg", ".webp", ".gif"])


class SystemContext(BaseModel):
    company_name: str = "OmniCart"
    tone: str = "Professional, helpful, and concise."
    system_prompt_template: str = (
        "You are an AI Sales and Customer Service agent for {company_name}. "
        "Your tone should be {tone}. "
        "You can answer questions based on the provided knowledge base, "
        "recommend products, and assist with orders. Do not invent information."
    )


class AgentConfiguration(BaseModel):
    flask_config: FlaskConfig = Field(default_factory=FlaskConfig)
    meta_config: MetaConfig = Field(default_factory=MetaConfig)
    database_config: DatabaseConfig = Field(default_factory=DatabaseConfig)
    system_context: SystemContext = Field(default_factory=SystemContext)
    llm_config: LLMConfig = Field(default_factory=LLMConfig)
    embedding_config: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    rag_config: RAGConfig = Field(default_factory=RAGConfig)
    agent: AgentRuntimeConfig = Field(default_factory=AgentRuntimeConfig)


def load_config(file_path: Optional[str] = None) -> AgentConfiguration:
    """
    Missing file: warn and fall back to defaults + env vars.
    Malformed file: fail fast with a clear ConfigurationError.
    """
    path = Path(file_path) if file_path else PROJECT_ROOT / "config.yaml"

    if not path.exists():
        logger.warning("Config file '%s' not found — using defaults and environment variables.", path)
        return AgentConfiguration()

    try:
        yaml_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Configuration file '{path}' is malformed: {exc}") from exc

    return AgentConfiguration.model_validate(yaml_data)


def save_config(configuration: AgentConfiguration, file_path: Optional[str] = None) -> None:
    """
    Persists the configuration to YAML and hot-reloads the process-wide
    singleton so changes take effect on the next request without a restart.

    Secrets (API keys, Meta tokens) are never written to disk — they remain
    sourced exclusively from the environment.
    """
    path = Path(file_path) if file_path else PROJECT_ROOT / "config.yaml"

    data = configuration.model_dump(mode="json")
    for section, secret_fields in (
        ("llm_config", ("api_key",)),
        ("embedding_config", ("api_key",)),
        ("meta_config", ("page_access_token", "app_secret", "verify_token")),
        ("flask_config", ("secret_key",)),
        ("database_config", ("url",)),
    ):
        for field_name in secret_fields:
            (data.get(section) or {}).pop(field_name, None)

    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    logger.info("Configuration saved to '%s'.", path)

    # In-place hot-reload: modules that did `from utils.config import config`
    # keep referencing the same object, so rebind its fields rather than the name.
    for field_name in AgentConfiguration.model_fields:
        object.__setattr__(config, field_name, getattr(configuration, field_name))


config = load_config()
