# Configuration Reference

All non-secret runtime behavior is controlled by `config.yaml` at the project root. Secrets are sourced exclusively from environment variables (loaded from `.env` via `python-dotenv`). The application boots successfully with no YAML file, falling back to Pydantic-defined defaults.

The live configuration object is exposed as `utils.config.config` and is hot-reloadable via the admin UI at `/admin/llm-config`.

---

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `FLASK_SECRET_KEY` | Flask session signing key. Use a long random string. **Never use the default in production.** |

### LLM Provider Keys (one required, matching `llm_config.provider`)

| Variable | Provider |
|---|---|
| `OPENAI_API_KEY` | `openai` |
| `ANTHROPIC_API_KEY` | `anthropic` |
| `GROQ_API_KEY` | `groq` |
| `GOOGLE_API_KEY` | `google` |
| `HUGGINGFACEHUB_API_TOKEN` | `huggingface` |

The `local` provider (Ollama) requires no API key. Set `OLLAMA_BASE_URL` to override the default `http://localhost:11434`.

### Embedding Provider Keys

Same variables as above — the embedding provider and LLM provider can differ (e.g., Groq LLM + local Ollama embeddings).

### Facebook Messenger (optional)

| Variable | Description |
|---|---|
| `META_PAGE_ACCESS_TOKEN` | Page Access Token from Facebook for Developers |
| `META_VERIFY_TOKEN` | Arbitrary token used for webhook verification handshake |
| `META_APP_SECRET` | App Secret for HMAC-SHA256 payload signature verification |

### Database (optional overrides)

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///instance/ecommerce.db` | SQLAlchemy database URL |
| `CHROMA_DB_DIR` | `./instance/chroma_db` | ChromaDB persistence directory |
| `LANGGRAPH_CHECKPOINT_PATH` | `./instance/checkpoints.sqlite` | LangGraph SQLite checkpointer path |

---

## `config.yaml` Reference

### `flask_config`

Controls the Flask server and session behavior.

| Key | Type | Default | Description |
|---|---|---|---|
| `host` | str | `127.0.0.1` | Bind address |
| `port` | int | `5000` | Bind port |
| `debug` | bool | `false` | Werkzeug debug mode. Also controlled by `FLASK_DEBUG` env var. |
| `session_cookie_httponly` | bool | `true` | Prevents JS access to the session cookie |
| `session_cookie_samesite` | str | `Lax` | `Lax`, `Strict`, or `None` |
| `session_cookie_secure` | bool | `false` | Send cookie only over HTTPS. Enable in production. Also controlled by `FLASK_SECURE_COOKIES` env var. |
| `permanent_session_lifetime_minutes` | int | `720` | Session TTL (12 hours) |

---

### `database_config`

| Key | Type | Default | Description |
|---|---|---|---|
| `echo_queries` | bool | `false` | Log every SQLAlchemy-generated SQL statement |
| `chroma_persist_directory` | str | `./instance/chroma_db` | ChromaDB on-disk location. Overridden by `CHROMA_DB_DIR`. |
| `checkpoint_path` | str | `./instance/checkpoints.sqlite` | LangGraph checkpointer SQLite file. Overridden by `LANGGRAPH_CHECKPOINT_PATH`. |

---

### `rag_config`

Controls knowledge base ingestion and retrieval.

| Key | Type | Default | Description |
|---|---|---|---|
| `collection_name` | str | `ecommerce_knowledge` | ChromaDB collection name |
| `max_file_size_mb` | int | `10` | Maximum uploaded file size for knowledge documents |
| `max_content_chars` | int | `200000` | Maximum text content per document (post-extraction) |
| `supported_extensions` | list[str] | `[".txt", ".pdf", ".docx"]` | Allowed upload extensions |
| `chunk_size` | int | `1000` | `RecursiveCharacterTextSplitter` chunk size |
| `chunk_overlap` | int | `200` | Chunk overlap for context continuity |
| `sync_on_startup` | bool | `true` | Run `_reconcile_vector_store()` at boot |
| `separators` | list[str] | `["\n\n", "\n", ".", " ", ""]` | Splitter separator priority list |

---

### `llm_config`

Controls the primary LLM used by the agent.

| Key | Type | Default | Description |
|---|---|---|---|
| `provider` | str | `openai` | `openai` \| `anthropic` \| `groq` \| `google` \| `huggingface` \| `local` |
| `model_name` | str | `gpt-4o-mini` | Provider-specific model identifier |
| `temperature` | float | `0.2` | Sampling temperature (0.0–2.0) |
| `max_tokens` | int | `2048` | Maximum tokens in the LLM response |
| `top_p` | float \| null | `null` | Nucleus sampling. `null` = provider default. |
| `seed` | int \| null | `null` | Reproducibility seed. `null` = provider default. |
| `max_retries` | int | `3` | LangChain-level HTTP retries |
| `timeout_seconds` | float | `60.0` | Per-call timeout |
| `vision_capable` | bool | `false` | Gates image attachment uploads in the chat UI. Set `true` for multimodal models. |

---

### `embedding_config`

Controls the embedding model used by the RAG pipeline.

| Key | Type | Default | Description |
|---|---|---|---|
| `provider` | str | `openai` | `openai` \| `huggingface` \| `google` \| `local` |
| `model_name` | str | `text-embedding-3-small` | Model identifier |

> **Note:** Anthropic and Groq do not currently expose public embedding APIs. If using those LLM providers, configure a separate embedding provider (e.g., `local` with Ollama).

---

### `system_context`

Brand identity injected into every agent system prompt.

| Key | Type | Default | Description |
|---|---|---|---|
| `company_name` | str | `OmniCart` | Used in the system prompt template |
| `tone` | str | `Professional, helpful, and concise.` | Tone directive for the agent |

---

### `meta_config`

Facebook Messenger integration settings.

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Master toggle for the Messenger transport |
| `graph_api_version` | str | `v21.0` | Meta Graph API version |
| `public_base_url` | str \| null | `null` | Base URL for product image links in carousel cards |
| `welcome_new_users` | bool | `true` | Send a welcome message to first-time Messenger users |

Secrets (`verify_token`, `page_access_token`, `app_secret`) are always sourced from environment variables and are never written to YAML.

---

### `agent`

Runtime behavior of the LangGraph agent execution loop.

| Key | Type | Default | Description |
|---|---|---|---|
| `guard_enabled` | bool | `true` | Toggle the guard node on/off. Hot-reloadable. |
| `guard_include_context` | bool | `true` | Include recent transcript in the guard classifier input |
| `max_tool_retries` | int | `2` | Default retry budget per tool call (0–5). Model can override per call. |
| `tool_retry_backoff_seconds` | float | `0.5` | Linear backoff base: `sleep(backoff * attempt)` |
| `status_events_enabled` | bool | `true` | Emit `agent_status` Socket.IO events |
| `sensitive_tool_names` | list[str] | `["add_to_cart", "checkout"]` | Tools that trigger the confirmation interrupt |
| `confirmation_timeout_seconds` | int | `600` | Seconds before a pending confirmation is auto-declined (10 minutes) |
| `max_upload_mb` | int | `5` | Maximum image attachment size in the chat UI |
| `allowed_upload_extensions` | list[str] | `[".png", ".jpg", ".jpeg", ".webp", ".gif"]` | Allowed image attachment extensions |

---

## Runtime Hot-Reload

The admin endpoint `PUT /admin/llm-config` accepts partial updates to `llm_config` and `agent` namespaces. Changes are validated, saved to `config.yaml` (secrets stripped), and applied in-place to the live `config` singleton. The agent graph cache is cleared, causing the next chat turn to compile a new graph against the updated LLM client.

Secrets are **not** writable via this endpoint — they must be updated in `.env` and the process restarted.
