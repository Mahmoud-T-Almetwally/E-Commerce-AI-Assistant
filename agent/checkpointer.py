"""
Lazy singleton SqliteSaver bound to config.database_config.checkpoint_path.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading

from utils.config import config

logger = logging.getLogger(__name__)

_CHECKPOINTER = None
_LOCK = threading.Lock()


def get_checkpointer():
    """Thread-safe lazy checkpointer; SqliteSaver serializes its own connection."""
    global _CHECKPOINTER
    if _CHECKPOINTER is None:
        with _LOCK:
            if _CHECKPOINTER is None:
                from langgraph.checkpoint.sqlite import SqliteSaver
                path = config.database_config.checkpoint_path
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                conn = sqlite3.connect(path, check_same_thread=False)
                
                _CHECKPOINTER = SqliteSaver(conn)

                _CHECKPOINTER.setup()
                logger.info("LangGraph sqlite checkpointer ready at %s", path)
    return _CHECKPOINTER