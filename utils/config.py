import os
import yaml
from typing import Literal, Optional, Dict, Any
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

from utils.exceptions import ProviderAPIKeyNotFound, ConfigFileNotFoundError

# Explicitly load environment variables from a .env file into os.environ
load_dotenv()


PROVIDER_API_KEY_MAP: Dict[str, Optional[str]] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
    "google": "GOOGLE_API_KEY",
    "huggingface": "HUGGINGFACEHUB_API_TOKEN",
    "local": None
}

def resolve_api_key(provider: str, provided_key: Optional[str]) -> Optional[str]:
    """
    Resolves the API key for a given provider. Uses the provided key if available,
    otherwise falls back to retrieving it from loaded environment variables.
    """
    if provided_key:
        return provided_key

    expected_env_var = PROVIDER_API_KEY_MAP.get(provider.lower())
    if not expected_env_var:
        return None

    api_key = os.environ.get(expected_env_var)
    if not api_key:
        raise ProviderAPIKeyNotFound(provider=provider, env_var_name=expected_env_var)
    
    return api_key


class FlaskConfig(BaseModel):
    """Configuration for the Flask backend web server and admin dashboard."""
    secret_key: str = Field(default_factory=lambda: os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-in-production"))
    host: str = "127.0.0.1"
    port: int = 5000
    debug: bool = True


class MetaConfig(BaseModel):
    """Configuration for the Meta/Facebook Messenger Bonus integration."""
    enabled: bool = False
    verify_token: Optional[str] = Field(default_factory=lambda: os.environ.get("META_VERIFY_TOKEN"))
    page_access_token: Optional[str] = Field(default_factory=lambda: os.environ.get("META_PAGE_ACCESS_TOKEN"))
    app_secret: Optional[str] = Field(default_factory=lambda: os.environ.get("META_APP_SECRET"))


class DatabaseConfig(BaseModel):
    """Configuration for the primary SQL database and Vector/RAG database."""
    url: str = Field(default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite:///instance/ecommerce.db"))
    chroma_persist_directory: str = Field(default_factory=lambda: os.environ.get("CHROMA_DB_DIR", "./instance/chroma_db"))
    echo_queries: bool = False
    connect_args: Dict[str, Any] = Field(default_factory=lambda: {"check_same_thread": False})


class EmbeddingConfig(BaseModel):
    """Configuration for the Vector embedding model."""
    provider: Literal["openai", "huggingface", "google", "local"] = "openai"
    model_name: str = "text-embedding-3-small"
    api_key: Optional[str] = None

    @model_validator(mode='after')
    def validate_api_key(self):
        self.api_key = resolve_api_key(self.provider, self.api_key)
        return self


class LLMConfig(BaseModel):
    """Configuration for the LangGraph Chat/Agent Model."""
    provider: Literal["openai", "anthropic", "groq", "google", "huggingface", "local"] = "openai"
    model_name: str = "gpt-4o-mini"
    temperature: float = 0.2
    max_tokens: int = 2048
    api_key: Optional[str] = None

    @model_validator(mode='after')
    def validate_api_key(self):
        self.api_key = resolve_api_key(self.provider, self.api_key)
        return self


class SystemContext(BaseModel):
    """Configuration for the AI Agent's persona and context constraints."""
    company_name: str = "OmniCart"
    tone: str = "Professional, helpful, and concise."
    system_prompt_template: str = (
        "You are an AI Sales and Customer Service agent for {company_name}. "
        "Your tone should be {tone}. "
        "You can answer questions based on the provided knowledge base, "
        "recommend products, and assist with orders. Do not invent information."
    )


class AgentConfiguration(BaseModel):
    """Root configuration object for the application."""
    flask_config: FlaskConfig = Field(default_factory=FlaskConfig)
    meta_config: MetaConfig = Field(default_factory=MetaConfig)
    database_config: DatabaseConfig = Field(default_factory=DatabaseConfig)
    system_context: SystemContext = Field(default_factory=SystemContext)
    llm_config: LLMConfig = Field(default_factory=LLMConfig)
    embedding_config: EmbeddingConfig = Field(default_factory=EmbeddingConfig)


def load_config(file_path: str = "config.yaml") -> AgentConfiguration:
    """
    Loads configuration from a YAML file. 
    Falls back to default values (and environment variables) if the file doesn't exist.
    """
    if not os.path.exists(file_path):
        raise ConfigFileNotFoundError(file_path=file_path)
    
    with open(file_path, "r") as f:
        yaml_data = yaml.safe_load(f) or {}
        
    return AgentConfiguration.model_validate(yaml_data)

# Global instantiated config loaded automatically when this module is imported
config = load_config("config.yaml")