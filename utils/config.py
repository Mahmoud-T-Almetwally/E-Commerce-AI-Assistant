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
    separators: List[str] = Field(default_factory=lambda: ["\n\n", "\n", ".", " ", ""])


class MetaConfig(BaseModel):
    enabled: bool = False
    verify_token: Optional[str] = Field(default_factory=lambda: os.environ.get("META_VERIFY_TOKEN"))
    page_access_token: Optional[str] = Field(default_factory=lambda: os.environ.get("META_PAGE_ACCESS_TOKEN"))
    app_secret: Optional[str] = Field(default_factory=lambda: os.environ.get("META_APP_SECRET"))


class DatabaseConfig(BaseModel):
    url: str = Field(default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite:///instance/ecommerce.db"))
    chroma_persist_directory: str = Field(default_factory=lambda: os.environ.get("CHROMA_DB_DIR", "./instance/chroma_db"))
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
    api_key: Optional[str] = None

    def get_api_key(self) -> Optional[str]:
        return _require_api_key(self.provider, self.api_key)


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


def load_config(file_path: Optional[str] = None) -> AgentConfiguration:
    """
    Missing file: warn and fall back to defaults + env vars (matches the
    original docstring, which the old code contradicted).
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


config = load_config()