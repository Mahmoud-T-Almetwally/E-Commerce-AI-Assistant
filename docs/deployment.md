# Deployment Guide

This document covers what needs to change before taking this application to a production environment. It is not a step-by-step tutorial — it assumes familiarity with Linux server administration and Python deployment tooling.

---

## Critical Pre-Requisites

The development setup makes several simplifying assumptions that **must** be addressed before production:

1. **Single-process only.** The in-memory stores (`_PENDING`, `_ACTIVE_TURNS`, `_ATTACHMENTS` in `routes/chat.py`) and the `StatsRegistry` in `agent/stats.py` are process-local. Running multiple worker processes will cause split-brain: a confirmation emitted by worker A will never be received by a `confirmation_response` arriving at worker B. See the scaling section below.

2. **Werkzeug is a development server.** `socketio.run(..., allow_unsafe_werkzeug=True)` uses Werkzeug's built-in server, which is single-threaded and not production-grade. It is used because `python-engineio` requires a Socket.IO-aware WSGI server.

3. **`FLASK_SECRET_KEY` must be changed.** The default `"dev-secret-key-change-in-production"` is logged as a warning on startup. Generate a random 64-byte key: `python -c "import secrets; print(secrets.token_hex(64))"`.

4. **`SESSION_COOKIE_SECURE` must be `true` behind HTTPS.** Set `FLASK_SECURE_COOKIES=true` in the environment.

---

## Single-Worker Production (Recommended Starting Point)

For a single-server deployment, use **eventlet** or **gevent** in threading mode with a process supervisor:

```bash
# Using gunicorn with the eventlet worker (supports Socket.IO)
gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:5000 "app:create_app()"
```

> **Why 1 worker?** Socket.IO long-polling and the in-memory maps require a single process. Eventlet makes that single process handle many concurrent connections via cooperative multitasking.

A production-grade setup with **nginx** as a reverse proxy:

```nginx
server {
    listen 443 ssl;
    server_name yourdomain.com;

    # ... SSL certificate config ...

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "Upgrade";   # required for WebSocket
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;   # accommodate long LLM turns
    }
}
```

---

## Scaling Beyond One Worker

Scaling to multiple workers requires externalizing all shared state:

| Component | Current (dev) | Production replacement |
|---|---|---|
| `_ACTIVE_TURNS` / `_PENDING` / `_ATTACHMENTS` | In-memory dicts | Redis hash/set (with TTL for attachments and confirmations) |
| `StatsRegistry` | In-memory deque | Query `agent_turn_stats` directly (already persisted to SQL) |
| Flask-Limiter storage | `memory://` | `RedisStorage` — `limiter = Limiter(..., storage_uri="redis://...")` |
| Socket.IO message queue | None (single process) | `socketio = SocketIO(..., message_queue="redis://...")` |
| LangGraph checkpointer | SQLite (`check_same_thread=False`) | PostgreSQL checkpointer (`langgraph-checkpoint-postgres`) — SQLite does not support concurrent writes from multiple processes |

---

## Database

### SQLite (default)
Adequate for low-traffic single-server deployments. Enable WAL mode for better concurrent read performance:

```python
# in database/db_setup.py, after creating the engine
from sqlalchemy import event
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()
```

### PostgreSQL
Set `DATABASE_URL=postgresql+psycopg2://user:pass@host/dbname` and install `psycopg2-binary`. All models are compatible with PostgreSQL — SQLAlchemy handles dialect differences.

---

## Vector Store (ChromaDB)

The default `Chroma` instance is embedded (in-process). For a multi-server deployment, switch to a ChromaDB HTTP client pointing at a shared ChromaDB server:

```python
# In database/rag_manager.py, replace the Chroma(...) instantiation:
import chromadb
client = chromadb.HttpClient(host="chroma-server", port=8000)
self.vector_store = Chroma(
    client=client,
    collection_name=config.rag_config.collection_name,
    embedding_function=self.embeddings,
)
```

---

## Environment Configuration Checklist

```bash
# Required
FLASK_SECRET_KEY="<64-byte-random-hex>"
FLASK_SECURE_COOKIES="true"

# LLM provider key (whichever matches config.yaml llm_config.provider)
GROQ_API_KEY="..."          # or OPENAI_API_KEY, etc.

# Database (if not using default SQLite)
DATABASE_URL="postgresql+psycopg2://user:pass@host/dbname"

# Paths
CHROMA_DB_DIR="/var/data/chroma"
LANGGRAPH_CHECKPOINT_PATH="/var/data/checkpoints.sqlite"

# Messenger (if enabled)
META_PAGE_ACCESS_TOKEN="..."
META_VERIFY_TOKEN="..."
META_APP_SECRET="..."
```

---

## Security Hardening

| Item | Action |
|---|---|
| Flask secret key | Set to a long random value. Rotate periodically. |
| Session cookies | Enable `Secure` flag. Ensure the app is only served over HTTPS. |
| Rate limits | Default limits on login/register only. Add application-level limits on the Socket.IO connection and message events for DDoS protection. |
| CSRF | Enabled by default. Ensure no non-webhook POST endpoint is accidentally exempted. |
| Webhook HMAC | Already implemented. Keep `META_APP_SECRET` secret and rotate it if compromised. |
| Debug mode | Ensure `FLASK_DEBUG` is **not** set in production. The Werkzeug debugger exposes a code execution PIN. |
| Database | Use a dedicated database user with minimal permissions (SELECT, INSERT, UPDATE, DELETE on application tables — no DDL rights). |
| File uploads | Already in-memory only. The `MAX_CONTENT_LENGTH` config key limits total request body size. |
| LLM API keys | Never write to disk (enforced by `save_config()`). Rotate if exposed. |

---

## Logging

The app uses Python's standard `logging` module at `INFO` level. In production, configure structured JSON logging and route output to your log aggregator:

```python
# Example: add to app.py before create_app() call
import logging
import json

class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "time": self.formatTime(record),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        })

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logging.basicConfig(handlers=[handler], level=logging.INFO)
```

---

## Enabling Facebook Messenger

1. Set `meta_config.enabled: true` in `config.yaml`.
2. Set `META_PAGE_ACCESS_TOKEN`, `META_VERIFY_TOKEN`, `META_APP_SECRET` in `.env`.
3. Set `meta_config.public_base_url` to your public HTTPS domain (used for product image URLs in carousel cards).
4. Uncomment the webhook blueprint registration in `routes/__init__.py`.
5. Register your webhook URL (`https://yourdomain.com/webhook`) in the Meta for Developers portal and complete the verification handshake.

The webhook endpoint must be reachable over HTTPS with a valid certificate — Meta does not accept self-signed certificates.

---

## Health Check

`GET /chat/ping` returns a JSON response with auth state and capability flags. It is public and cheap — suitable for load balancer health checks.

```json
{
  "status": "ok",
  "authenticated": false,
  "uploads_enabled": false,
  "max_upload_mb": 5
}
```
