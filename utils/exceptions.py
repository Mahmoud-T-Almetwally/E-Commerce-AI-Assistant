class ConfigurationError(Exception):
    """Base exception for all configuration-related errors."""
    pass


class ProviderAPIKeyNotFound(ConfigurationError):
    """Raised when a required API key for a specified AI provider is missing."""
    def __init__(self, provider: str, env_var_name: str):
        self.provider = provider
        self.env_var_name = env_var_name
        super().__init__(
            f"API key for provider '{provider}' not found. "
            f"Please ensure the '{env_var_name}' environment variable is set."
        )


class ConfigFileNotFoundError(ConfigurationError):
    """Raised when the configuration YAML file cannot be found."""
    def __init__(self, file_path: str):
        super().__init__(f"Configuration file not found at path: '{file_path}'.")


class UnsupportedProviderError(ConfigurationError):
    """Raised when an unsupported AI provider is requested in the configuration."""
    def __init__(self, provider: str, supported: list[str]):
        super().__init__(
            f"Unsupported AI provider requested: '{provider}'. "
            f"Supported providers are: {supported}."
        )


class DatabaseError(Exception):
    """Base exception for all database-related errors."""
    pass


class DatabaseConnectionError(DatabaseError):
    """Raised when the application fails to connect to the database."""
    def __init__(self, db_url: str, details: str):
        super().__init__(f"Failed to connect to database at '{db_url}'. Details: {details}")


class RecordNotFoundError(DatabaseError):
    """Raised when a queried database record (Product, Order, etc.) does not exist."""
    def __init__(self, model: str, record_id: int):
        super().__init__(f"Record not found: {model} with ID {record_id} does not exist.")


class OutOfStockError(DatabaseError):
    """Raised when attempting to order a product that lacks sufficient inventory."""
    def __init__(self, product_name: str, requested: int, available: int):
        super().__init__(
            f"Cannot fulfill order for '{product_name}'. "
            f"Requested: {requested}, Available: {available}."
        )


class AgentError(Exception):
    """Base exception for all agent and LLM-related errors."""
    pass


class ToolExecutionError(AgentError):
    """Raised when a LangGraph tool encounters an unexpected error during execution."""
    def __init__(self, tool_name: str, error_msg: str):
        super().__init__(f"Error executing tool '{tool_name}': {error_msg}")


class AgentRoutingError(AgentError):
    """Raised when the LangGraph agent fails to determine the next node."""
    def __init__(self, current_node: str, intent: str):
        super().__init__(
            f"Agent failed to route from node '{current_node}' based on intent '{intent}'."
        )