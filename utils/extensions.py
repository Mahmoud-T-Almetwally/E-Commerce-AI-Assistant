"""Cross-cutting Flask extensions"""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect()

# Swap storage_uri to Redis in production (limits are per-process otherwise).
limiter = Limiter(key_func=get_remote_address, default_limits=[], storage_uri="memory://")