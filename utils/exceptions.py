class ProviderAPIKeyNotFound(Exception):
    """Raised when a required API key for a specified AI provider is missing."""
    def __init__(self, provider: str, env_var_name: str):
        self.provider = provider
        self.env_var_name = env_var_name
        super().__init__(
            f"API key for provider '{provider}' not found. "
            f"Please ensure the '{env_var_name}' environment variable is set."
        )