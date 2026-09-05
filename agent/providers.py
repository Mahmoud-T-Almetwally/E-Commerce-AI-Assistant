import os
from abc import ABC, abstractmethod

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.embeddings import Embeddings

from utils.exceptions import ConfigurationError
from utils.config import LLMConfig, EmbeddingConfig


class BaseProvider(ABC):
    """Abstract base class for all AI model providers."""
    
    @abstractmethod
    def get_llm(self, config: LLMConfig) -> BaseChatModel:
        """Instantiates and returns the LLM (Chat Model)."""
        pass

    @abstractmethod
    def get_embeddings(self, config: EmbeddingConfig) -> Embeddings:
        """Instantiates and returns the Embeddings model."""
        pass


class OpenAIProvider(BaseProvider):
    def get_llm(self, config: LLMConfig) -> BaseChatModel:
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as e:
            raise ConfigurationError("Missing package. Run: pip install langchain-openai") from e
            
        return ChatOpenAI(
            model=config.model_name,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            api_key=config.api_key,
            max_retries=3,
            timeout=60
        )

    def get_embeddings(self, config: EmbeddingConfig) -> Embeddings:
        try:
            from langchain_openai import OpenAIEmbeddings
        except ImportError as e:
            raise ConfigurationError("Missing package. Run: pip install langchain-openai") from e
            
        return OpenAIEmbeddings(
            model=config.model_name,
            api_key=config.api_key,
            max_retries=3,
            timeout=30
        )


class AnthropicProvider(BaseProvider):
    def get_llm(self, config: LLMConfig) -> BaseChatModel:
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as e:
            raise ConfigurationError("Missing package. Run: pip install langchain-anthropic") from e
            
        return ChatAnthropic(
            model_name=config.model_name,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            api_key=config.api_key,
            max_retries=3,
            timeout=60
        )

    def get_embeddings(self, config: EmbeddingConfig) -> Embeddings:
        raise NotImplementedError("Anthropic does not currently provide a public embeddings API.")


class GroqProvider(BaseProvider):
    def get_llm(self, config: LLMConfig) -> BaseChatModel:
        try:
            from langchain_groq import ChatGroq
        except ImportError as e:
            raise ConfigurationError("Missing package. Run: pip install langchain-groq") from e
            
        return ChatGroq(
            model_name=config.model_name,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            api_key=config.api_key,
            max_retries=3,
            timeout=60
        )

    def get_embeddings(self, config: EmbeddingConfig) -> Embeddings:
        raise NotImplementedError("Groq does not currently provide an embeddings API.")


class GoogleProvider(BaseProvider):
    def get_llm(self, config: LLMConfig) -> BaseChatModel:
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as e:
            raise ConfigurationError("Missing package. Run: pip install langchain-google-genai") from e
            
        return ChatGoogleGenerativeAI(
            model=config.model_name,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            api_key=config.api_key,
            max_retries=3,
            timeout=60
        )

    def get_embeddings(self, config: EmbeddingConfig) -> Embeddings:
        try:
            from langchain_google_genai import GoogleGenerativeAIEmbeddings
        except ImportError as e:
            raise ConfigurationError("Missing package. Run: pip install langchain-google-genai") from e
            
        return GoogleGenerativeAIEmbeddings(
            model=config.model_name,
            api_key=config.api_key
        )


class HuggingFaceProvider(BaseProvider):
    def get_llm(self, config: LLMConfig) -> BaseChatModel:
        try:
            from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint
        except ImportError as e:
            raise ConfigurationError("Missing package. Run: pip install langchain-huggingface") from e
            
        llm_backend = HuggingFaceEndpoint(
            repo_id=config.model_name,
            temperature=config.temperature,
            max_new_tokens=config.max_tokens,
            huggingfacehub_api_token=config.api_key,
            timeout=60
        )
        return ChatHuggingFace(llm=llm_backend)

    def get_embeddings(self, config: EmbeddingConfig) -> Embeddings:
        try:
            from langchain_huggingface import HuggingFaceEndpointEmbeddings
        except ImportError as e:
            raise ConfigurationError("Missing package. Run: pip install langchain-huggingface") from e
            
        return HuggingFaceEndpointEmbeddings(
            model=config.model_name, 
            huggingfacehub_api_token=config.api_key
        )


class LocalProvider(BaseProvider):
    def get_llm(self, config: LLMConfig) -> BaseChatModel:
        try:
            from langchain_ollama import ChatOllama
        except ImportError as e:
            raise ConfigurationError("Missing package. Run: pip install langchain-ollama") from e
            
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        return ChatOllama(
            model=config.model_name,
            temperature=config.temperature,
            base_url=base_url
        )

    def get_embeddings(self, config: EmbeddingConfig) -> Embeddings:
        try:
            from langchain_ollama import OllamaEmbeddings
        except ImportError as e:
            raise ConfigurationError("Missing package. Run: pip install langchain-ollama") from e
            
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        return OllamaEmbeddings(
            model=config.model_name,
            base_url=base_url
        )




class ModelFactory:
    """Singleton factory to retrieve configured LLMs and Embedding models."""
    
    _providers: dict[str, type[BaseProvider]] = {
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
        "groq": GroqProvider,
        "google": GoogleProvider,
        "huggingface": HuggingFaceProvider,
        "local": LocalProvider,
    }
    
    _instances: dict[str, BaseProvider] = {}

    @classmethod
    def _get_provider(cls, name: str) -> BaseProvider:
        provider_key = name.lower()
        if provider_key not in cls._providers:
            raise ValueError(
                f"Unsupported Provider '{provider_key}'. "
                f"Supported providers: {list(cls._providers.keys())}"
            )
            
        if provider_key not in cls._instances:
            cls._instances[provider_key] = cls._providers[provider_key]()
            
        return cls._instances[provider_key]

    @classmethod
    def get_llm(cls, config: LLMConfig) -> BaseChatModel:
        provider = cls._get_provider(config.provider)
        return provider.get_llm(config)

    @classmethod
    def get_embeddings(cls, config: EmbeddingConfig) -> Embeddings:
        provider = cls._get_provider(config.provider)
        return provider.get_embeddings(config)