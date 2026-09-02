import os
import yaml
from typing import Literal, Optional, Dict
from pydantic import BaseModel, Field, model_validator, ValidationError

from utils.exceptions import ProviderAPIKeyNotFound, ConfigFileNotFoundError


PROVIDER_API_KEY_MAP: Dict[str, Optional[str]] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
    "google": "GOOGLE_API_KEY",
    "huggingface": "HUGGINGFACEHUB_API_TOKEN",
    "local": None
}

def resolve_api_key(provider: str, provided_key: Optional[str]) -> Optional[str]:
    if provided_key:
        return provided_key

    expected_env_var = PROVIDER_API_KEY_MAP.get(provider.lower())
    if not expected_env_var:
        return None

    api_key = os.getenv(expected_env_var)
    if not api_key:
        raise ProviderAPIKeyNotFound(provider=provider, env_var_name=expected_env_var)
    
    return api_key


class DatabaseConfig(BaseModel):
    url: str = Field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///instance/ecommerce.db"))
    chroma_persist_directory: str = Field(default_factory=lambda: os.getenv("CHROMA_DB_DIR", "./instance/chroma_db"))
    echo_queries: bool = False


class EmbeddingConfig(BaseModel):
    provider: Literal["openai", "huggingface", "google", "local"] = "openai"
    model_name: str = "text-embedding-3-small"
    api_key: Optional[str] = None

    @model_validator(mode='after')
    def validate_api_key(self):
        self.api_key = resolve_api_key(self.provider, self.api_key)
        return self


class LLMConfig(BaseModel):
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
    company_name: str = "OmniCart"
    tone: str = "Professional, helpful, and concise."
    system_prompt_template: str = (
        "You are an AI Sales and Customer Service agent for {company_name}. "
        "Your tone should be {tone}. "
        "You can answer questions based on the provided knowledge base, "
        "recommend products, and assist with orders."
    )


class AgentConfiguration(BaseModel):
    """Root configuration object for the application."""
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
        # We can either raise an error or return defaults. 
        # Returning defaults is usually safer for initial dev/CI pipelines.
        print(f"Warning: {file_path} not found. Using default configurations.")
        return AgentConfiguration()
    
    with open(file_path, "r") as f:
        yaml_data = yaml.safe_load(f) or {}
        
    return AgentConfiguration.model_validate(yaml_data)

# Global instantiated config loaded automatically when this module is imported
config = load_config("config.yaml")