# Application Routing Architecture

This document maps out the HTTP endpoints, Webhooks, and WebSocket (Socket.IO) event handlers orchestrating the E-Commerce AI Assistant. 

The routing layer is organized into functional Flask Blueprints. It employs strict session-based authentication, dual-database synchronizations (SQLite/ChromaDB), optimistic locking for inventory control, and background task offloading for LangGraph LLM executions.

---

## Authorization Definitions

Most endpoints enforce access controls via decorators. In this document, the **Auth** column uses the following shorthand:
* **Public**: No authentication required.
* **User**: Requires an active session via `@login_required` (Customer or Admin).
* **Admin**: Requires an active session with Admin privileges via `@admin_required`.

---

## 1. Authentication (`routes/auth.py`)
Handles identity provisioning and session establishment. Follows standard cookie-based server-side session patterns with safe redirect validations.

| Method | Route | Auth | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/login` | Public | Renders the login form. Bypasses if already authenticated. |
| `POST` | `/login` | Public | Validates credentials, sets session state (`user_id`, `role`), and safely redirects based on the `next` param or Role. |
| `GET` | `/register` | Public | Renders the customer registration form. |
| `POST` | `/register` | Public | Validates input (regex constraints, password matches) and provisions a new `CUSTOMER` account. |
| `GET` | `/logout` | Public | Destroys the active session and redirects to the storefront. |

---

## 2. Storefront & Cart (`routes/store.py`)
Handles customer-facing catalog browsing, shopping cart mutation, and order processing. Cart operations implement optimistic stock boundary checks.

| Method | Route | Auth | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/` | Public | Root redirect to `/products`. |
| `GET` | `/products` | Public | Product catalog featuring search, filtering, category/tag queries, and pagination. |
| `GET` | `/products/<id>` | Public | Detailed product view and randomly sampled related items. |
| `GET` | `/cart` | User | Renders the active user's shopping cart items. |
| `POST` | `/cart/add` | User | Adds items to the cart. Checks requested quantity against `Product.stock_quantity`. |
| `POST` | `/cart/update` | User | Updates item quantity. Re-verifies stock bounds. |
| `POST` | `/cart/remove/<id>` | User | Drops a `CartItem` row. |
| `GET` | `/checkout` | User | Renders the order summary before final placement. |
| `POST` | `/checkout` | User | Processes checkout. Utilizes **optimistic row-level locking** (`Product.stock_quantity >= item.quantity`) to prevent stock-draining race conditions. |
| `GET` | `/orders` | User | Lists the authenticated user's historical orders. |
| `GET` | `/orders/<id>` | User | Specific order ledger and status display. |

---

## 3. Administrative Management (`routes/admin.py`)
Standard CRUD and operational management for the store. 

| Method | Route | Auth | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/admin/` | Admin | Dashboard. Computes live KPIs, 7-day revenue aggregates, and order distribution stats. |
| `GET` | `/admin/products` | Admin | Filterable, sortable, paginated inventory grid. |
| `GET/POST` | `/admin/products/add` | Admin | Product creation. Parses strings to finite Decimals and synchronizes Tag associations. |
| `GET/POST` | `/admin/products/<id>/edit` | Admin | Edits product data. Executes orphaned tag purging (`purge_orphan_tags`) post-update. |
| `POST` | `/admin/products/<id>/delete` | Admin | Deletes product (gracefully fails if restricted by existing cart/order constraints). |
| `GET` | `/admin/orders` | Admin | Filterable order ledger (by status, customer, date ranges). |
| `POST` | `/admin/orders/<id>/status` | Admin | Modifies order status. **Business logic**: If changed to `CANCELLED`, automatically releases inventory back to `Product.stock_quantity`. |
| `GET` | `/admin/customers` | Admin | Customer directory with active/inactive filtering. |
| `POST` | `/admin/users/create-admin` | Admin | Internal provisioning endpoint for generating new Administrator credentials. |

---

## 4. LLM & Agent Configuration (`routes/llm_config.py`)
Provides hot-reloading for the LangGraph agent parameters and LangChain LLM wrappers. Keys are managed exclusively via environment variables for security.

| Method | Route | Auth | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/admin/llm-config` | Admin | Returns current configs (secrets scrubbed) and evaluates provider package availability. |
| `PUT` | `/admin/llm-config` | Admin | Validates and saves partial updates to `llm_config` or `agent` namespaces. Applies on next graph turn. |
| `POST` | `/admin/llm-config/test` | Admin | Dry-run initialized provider construction (tests package presence and auth without burning API credits). |
| `POST` | `/admin/llm-config/reset` | Admin | Drops customized config and restores hard-coded Pydantic defaults. |

---

## 5. RAG / Vector Store (`routes/rag.py`)
Manages business knowledge documents. Implementing a dual-write architecture, SQLite remains the source of truth while `ChromaDB` provides vector embedding.

| Method | Route | Auth | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/knowledge` | Admin | Paginated SQLite document ledger. |
| `GET/POST` | `/knowledge/add` | Admin | Ingests direct text or extracts from uploaded `.pdf`/`.docx`/`.txt`. Executes SQLite insert + `rag_manager.add_document()`. |
| `GET/POST` | `/knowledge/<id>/edit` | Admin | Modifies knowledge row. Syncs updates to ChromaDB. Rollback on vector DB failure. |
| `POST` | `/knowledge/<id>/delete` | Admin | Deep deletes from SQLite and ChromaDB. |

---

## 6. Lookup & Autocomplete (`routes/lookup.py`)
Public read-only endpoints facilitating client-side autocomplete and agent tooling responses.

| Method | Route | Auth | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/lookup/categories` | Public | Returns distinct product categories ordered by usage frequency. |
| `GET` | `/lookup/tags` | Public | Returns available tags ordered by usage frequency. |
| `GET` | `/lookup/products` | Public | Accepts a `ids` comma-separated query string. Returns exact-match product cards (preserves requested array order, max 12 items). Used heavily by the Chat UI to render AI Carousels. |

---

## 7. Statistics & Dashboards (`routes/stats.py`)
Extracts telemetry and lifecycle data from `AgentTurnStats` and `AgentToolCall` tables.

| Method | Route | Auth | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/admin/stats/overview` | Admin | KPI totals with 24h and 7d sliding window deltas. |
| `GET` | `/admin/stats/tokens` | Admin | Rollup of `tokens_in` and `tokens_out` grouped by (day/week/month). |
| `GET` | `/admin/stats/activity` | Admin | Rollup of total turns, guard blocks, and tool executions. |
| `GET` | `/admin/stats/tools` | Admin | Aggregated execution count, success rate, and duration matrix per tool. |
| `GET` | `/admin/stats/checkouts...` | Admin | Graph of revenue strictly tied to AI chat checkouts + full paginated ledger. |
| `GET` | `/admin/conversations...` | Admin | Paginated thread list denoting token usage and turn count per session. |
| `GET` | `/admin/conversations/<id>` | Admin | Deep telemetry view of a specific conversation (detailed turn outcomes and tool calls). |
| `GET` | `/admin/conversations/<id>/transcript` | Admin | Calls `build_transcript()` to replay the thread's checkpointer state. |
| `DELETE` | `/admin/conversations/<id>` | Admin | Hard deletes the thread. Drops pending UI confirmations, clears Checkpointer memory, and drops SQLite stat telemetry (Orders remain untouched). |

---

## 8. Meta Webhook Adapter (`routes/webhook.py`)
Acts as a secondary transport for the LangGraph assistant next to the primary Web UI. Validates HMAC payloads, handles deduplication, queueing, and converts LLM actions to Facebook Messenger API calls.

| Method | Route | Auth | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/webhook` | Public | Facebook's handshake endpoint. Verifies `hub.verify_token`. |
| `POST` | `/webhook` | Public | The event sink. Verifies `X-Hub-Signature-256`, deduplicates based on Mid/Timestamp LRU, and dispatches to sequential per-PSID background queues. Resolves PSIDs to internal `User` accounts implicitly. |

---

## 9. Chat Protocol (`routes/chat.py`)
The primary bidirectional LLM transport. Implements HTTP handlers for file uploads and history replays alongside a complete `Flask-SocketIO` protocol. Execution happens via `socketio.start_background_task`.

### 9.1 Chat HTTP Endpoints
| Method | Route | Auth | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/chat` | User | Customer-facing Web UI loader. |
| `GET` | `/chat/admin` | Admin | Admin playground Web UI loader (same interface, plus config panels). |
| `GET` | `/chat/ping` | Public | Healthcheck and active configuration flag exposure (upload limits, auth state). |
| `GET` | `/chat/history/<id>` | User | Fetches historical `build_transcript()` data for restoring a previous session. |
| `POST` | `/chat/upload` | User | Buffers a `.png/.jpg` upload to an in-memory TTL store, returning an `attachment_id` for consumption by the next Socket emit. |

### 9.2 Socket.IO Event Protocol

**Client → Server Events**
| Event Name | Payload | Description |
| :--- | :--- | :--- |
| `connect` | `None` | Evaluates auth state. If authenticated, joins `user_<id>` room. Triggers timeout sweep on stale threads. |
| `user_message` | `{conversation_id?, content, attachment_id?}` | Validates concurrency (rejects if user is busy). If new, provisions thread. Emits `conversation_started`. Executes LangGraph stream (`_run_turn`) in background. |
| `confirmation_response`| `{request_id, accepted: bool}` | Receives user's Human-in-the-loop decision for a paused LangGraph node. Validates expiration. Resumes graph in background (`_run_resume`). |

**Server → Client Events (Emitted to `user_<id>` room)**
| Event Name | Payload | Description |
| :--- | :--- | :--- |
| `connected` | `{user_id, ...config_flags}` | Confirms socket establishment and passes LLM capabilities (vision enabled, timeouts). |
| `conversation_started` | `{conversation_id}` | Broadcasted when a null-ID `user_message` automatically generates a new thread. |
| `agent_status` | `{phase, tool?, conversation_id}` | UI indicator events (`thinking`, `tool_running`, `resuming`, `done`). |
| `agent_note` | `{content, conversation_id}` | Interim text updates outputted by the model while invoking tools. |
| `display_product_carousel`| `{product_ids, conversation_id}` | Instructs UI to hydrate a carousel via `/lookup/products`. |
| `get_user_confirmation`| `{request_id, tool, message, args, expires_at}` | Graph hit an interrupt boundary (e.g., Cart Checkout). UI must render Accept/Decline overlay. |
| `confirmation_resolved`| `{request_id, accepted, reason}` | Closes a pending confirmation UI block (reason: `timeout`, `superseded`, `deleted`, `accepted`, `declined`). |
| `chat_message` | `{role, content, conversation_id, usage}` | Standard finalized text message appended to the UI chat log. |
| `chat_error` | `{code, message, conversation_id?}` | Displays transient toast errors (e.g., `busy`, `loop_limit`, `internal_error`). |