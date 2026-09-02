import os
from typing import Literal, Optional, Dict
from pydantic import BaseModel, Field, model_validator

from utils.exceptions import ProviderAPIKeyNotFound


# Mapping of provider names to their required environment variable keys
PROVIDER_API_KEY_MAP: Dict[str, Optional[str]] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
    "huggingface": "HUGGINGFACE_API_KEY",
    "local": None  # Local providers don't need an API key
}

def resolve_api_key(provider: str, provided_key: Optional[str]) -> Optional[str]:
    """
    Checks the provider mapping and retrieves the API key from the environment.
    Raises ProviderAPIKeyNotFound if the required key is missing.
    """
    if provided_key:
        return provided_key

    expected_env_var = PROVIDER_API_KEY_MAP.get(provider.lower())
    
    if not expected_env_var:
        return None

    api_key = os.getenv(expected_env_var)
    if not api_key:
        raise ProviderAPIKeyNotFound(provider=provider, env_var_name=expected_env_var)
    
    return api_key


class EmbeddingConfig(BaseModel):
    provider: Literal["groq", "openai", "huggingface", "local"] = "groq"
    model_name: str = "text-embedding-3-small"
    api_key: Optional[str] = None

    @model_validator(mode='after')
    def validate_api_key(self):
        self.api_key = resolve_api_key(self.provider, self.api_key)
        return self


class LLMConfig(BaseModel):
    provider: Literal["groq", "openai", "anthropic", "local"] = "groq"
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
    """Root configuration object for the AI Agent."""
    system_context: SystemContext = Field(default_factory=SystemContext)
    llm_config: LLMConfig = Field(default_factory=LLMConfig)
    embedding_config: EmbeddingConfig = Field(default_factory=EmbeddingConfig)


config = AgentConfiguration()