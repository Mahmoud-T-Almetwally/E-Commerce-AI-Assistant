import pytest
import sys
from unittest.mock import patch

from agent.providers import ModelFactory
from utils.config import LLMConfig
from utils.exceptions import ConfigurationError, UnsupportedProviderError


class TestModelProviders:

    def test_unsupported_provider_raises(self):
        """Test asking for an unknown provider."""
        
        with pytest.raises(UnsupportedProviderError, match="Unsupported Provider"):
            config = LLMConfig(provider="magic_ai", model_name="v1", api_key="secret")
            ModelFactory.get_llm(config)

    def test_missing_package_raises_config_error(self):
        """Test that missing a provider's pip package raises a clean ConfigurationError."""
        config = LLMConfig(provider="openai", model_name="gpt-4o", api_key="secret")
        
        with patch.dict(sys.modules, {'langchain_openai': None}):
            with pytest.raises(ConfigurationError, match="Missing package. Run: pip install langchain-openai"):
                ModelFactory.get_llm(config)

    @patch("agent.providers.LocalProvider.get_llm")
    def test_factory_retrieves_correct_provider(self, mock_local_llm):
        """Test the factory initializes the right provider class."""
        config = LLMConfig(provider="local", model_name="llama3", api_key="")
        
        ModelFactory.get_llm(config)
        
        mock_local_llm.assert_called_once_with(config)