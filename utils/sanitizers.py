def escape_like(value: str) -> str:
    """Escapes SQL LIKE wildcards so user input is matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")