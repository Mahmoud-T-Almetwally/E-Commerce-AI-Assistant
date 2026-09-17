# API Reference

This document covers the HTTP endpoints and the Socket.IO event protocol. For a condensed routing table, see [`routes/ROUTES.md`](../routes/ROUTES.md).

All protected endpoints use server-side session authentication. The session cookie is set on login and must be present on subsequent requests.

---

## Authentication Decorators

| Decorator | Behavior on failure |
|---|---|
| `@login_required` | Browsers: flash + redirect to `/login`. JSON clients (`Accept: application/json` or paths under `/chat/`): 401 JSON `{"error": "..."}` |
| `@admin_required` | Same as above, but also 403 for non-admin authenticated users |

---

## 1. Authentication (`/`)

### `GET /login`
Renders the login page. Redirects to dashboard or store if already authenticated.

### `POST /login`
Rate-limited: 10 per minute.

**Form fields:**

| Field | Type | Required |
|---|---|---|
| `email` | string | Yes |
| `password` | string | Yes |

On success, sets `session.user_id`, `session.role`, `session.name` and redirects based on role. Admins go to `/admin/`; customers go to `/products`.

### `GET /register`
Renders the customer registration form.

### `POST /register`
Rate-limited: 5 per minute. Creates a `CUSTOMER` role user.

**Form fields:**

| Field | Validation |
|---|---|
| `name` | Required |
| `email` | Required, `^[^\@\s]+@[^\@\s]+\.[^\@\s]+$` |
| `password` | Required, min 6 chars |
| `confirm_password` | Must match `password` |
| `phone` | Optional, 9 or 11 digits |

### `GET /logout`
Clears the session. Redirects to `/products`.

---

## 2. Storefront (`/`)

### `GET /`
Redirects to `/products`.

### `GET /products`
Public. Supports query parameters: `q` (search), `category`, `tag`, `min_price`, `max_price`, `page`, `per_page`, `sort`.

### `GET /products/<int:id>`
Public. Product detail page. Includes randomly sampled related products from the same category.

### `GET /cart`
Auth: User. Renders the current user's cart.

### `POST /cart/add`
Auth: User.

**Form fields:** `product_id` (int), `quantity` (int, default 1).

Validates `stock_quantity >= requested`. Upserts `CartItem`.

### `POST /cart/update`
Auth: User.

**Form fields:** `product_id` (int), `quantity` (int ≥ 1).

### `POST /cart/remove/<int:product_id>`
Auth: User. Deletes the `CartItem` row.

### `GET /checkout`
Auth: User. Renders order summary.

### `POST /checkout`
Auth: User. Atomic checkout with optimistic stock locking.

Returns to cart with flash message on stock failure. Redirects to `/orders/<id>` on success.

### `GET /orders`
Auth: User. Lists the authenticated user's orders (newest first).

### `GET /orders/<int:id>`
Auth: User. Order detail. Returns 403 if the order belongs to a different user.

---

## 3. Admin Management (`/admin/`)

All endpoints require `@admin_required`.

### `GET /admin/`
Dashboard. Returns KPIs: total products/orders/customers/revenue, 7-day revenue chart data, order status distribution.

### `GET /admin/products`
Paginated product grid. Query params: `q`, `category`, `tag`, `sort`, `page`.

### `GET /admin/products/add` / `POST /admin/products/add`
Product creation. Decimal validation, tag resolution (create if not exists).

### `GET /admin/products/<int:id>/edit` / `POST /admin/products/<int:id>/edit`
Product update. Runs orphan tag purge after update.

### `POST /admin/products/<int:id>/delete`
Product deletion. Gracefully handles FK constraints (e.g., existing orders reference the product).

### `GET /admin/orders`
Filterable order ledger. Query params: `q`, `status`, `date_from`, `date_to`, `page`.

### `POST /admin/orders/<int:id>/status`
**Form field:** `new_status` (one of: `processing`, `shipped`, `delivered`, `cancelled`).

Enforces the state machine:
```
pending → processing → shipped → delivered
       ↘ cancelled      ↘ cancelled
```

On cancellation, restores `stock_quantity` for all order items.

### `GET /admin/customers`
Customer directory. Query params: `q`, `active` (`true`/`false`), `page`.

### `POST /admin/users/create-admin`
Creates a new admin account. **Form fields:** `name`, `email`, `phone`, `password`, `confirm_password`.

---

## 4. LLM Configuration (`/admin/llm-config/`)

All endpoints require `@admin_required`. Returns/accepts JSON.

### `GET /admin/llm-config`
Returns current `llm_config` and `agent` config (secrets scrubbed). Also evaluates provider package availability.

**Response:**
```json
{
  "llm_config": { "provider": "groq", "model_name": "...", ... },
  "agent": { "guard_enabled": true, ... },
  "provider_availability": { "openai": true, "groq": true, "local": false, ... }
}
```

### `PUT /admin/llm-config`
Validates and applies partial updates to `llm_config` or `agent`.

**Request body:**
```json
{ "llm_config": { "temperature": 0.5 } }
```

Saves to `config.yaml`, hot-reloads the singleton, clears the graph cache.

### `POST /admin/llm-config/test`
Dry-runs provider construction (validates package presence and API key resolution without burning tokens).

**Request body:** `{ "provider": "groq", "model_name": "..." }`

### `POST /admin/llm-config/reset`
Restores all LLM and agent config fields to Pydantic defaults.

---

## 5. RAG Knowledge Base (`/admin/knowledge/`)

All endpoints require `@admin_required`.

### `GET /admin/knowledge`
Paginated document list from `knowledge_documents` table.

### `GET /admin/knowledge/add` / `POST /admin/knowledge/add`
Two submission modes:
- **Text**: `title`, `content` (direct text), `doc_type` form fields.
- **File upload**: `title`, `doc_type`, and a file (`attachment`) in `.txt`, `.pdf`, or `.docx` format. Text is extracted in-memory.

After SQL insert, calls `RAGManager.add_document()`. SQL is rolled back if vector store write fails.

### `GET /admin/knowledge/<int:id>/edit` / `POST /admin/knowledge/<int:id>/edit`
Updates document. Syncs changes to ChromaDB. Rolls back SQL on vector store failure.

### `POST /admin/knowledge/<int:id>/delete`
Deletes from SQLite and ChromaDB. Handles vector store unavailability gracefully.

---

## 6. Lookup / Autocomplete (`/lookup/`)

Read-only, public endpoints.

### `GET /lookup/categories`
Returns distinct product categories ordered by usage count descending.

**Response:** `{ "categories": ["Electronics", "Laptops", ...] }`

### `GET /lookup/tags`
Returns available tags ordered by product count descending.

**Response:** `{ "tags": ["Sale", "Gaming", ...] }`

### `GET /lookup/products`
**Query param:** `ids` — comma-separated product IDs, max 12.

Returns compact product cards preserving the requested order. Used by the chat UI to hydrate AI-triggered product carousels.

**Response:**
```json
{
  "products": [
    { "id": 1, "name": "...", "price": 99.99, "category": "...", "image_url": "...", "in_stock": true }
  ]
}
```

---

## 7. Statistics (`/admin/stats/` and `/admin/conversations/`)

All endpoints require `@admin_required`. All return JSON.

### `GET /admin/stats/overview`
KPI totals with 24h and 7d sliding window deltas (turns, tokens, guard blocks, tool calls).

### `GET /admin/stats/tokens`
Token usage rolled up by day/week/month. Query param: `period` (`day`/`week`/`month`).

### `GET /admin/stats/activity`
Turn count, guard block count, tool execution count — same time groupings.

### `GET /admin/stats/tools`
Per-tool aggregated stats: execution count, success rate, error rate, avg duration.

### `GET /admin/stats/checkouts`
Revenue from AI-initiated chat checkouts (i.e., `checkout` tool calls that produced an `order_id`). Also returns a paginated ledger.

### `GET /admin/conversations`
Paginated conversation list: thread ID, user, turn count, total tokens, last activity.

### `GET /admin/conversations/<int:id>`
Deep telemetry for one conversation: all turns with status, intent, token usage, and full tool call breakdown.

### `GET /admin/conversations/<int:id>/transcript`
Replays the LangGraph checkpointer state to reconstruct the full message transcript.

**Response:**
```json
{
  "conversation_id": 1,
  "messages": [
    { "role": "human", "content": "..." },
    { "role": "ai", "content": "..." }
  ]
}
```

### `DELETE /admin/conversations/<int:id>`
Hard-deletes the conversation:
1. Cancels any pending confirmation interrupt.
2. Clears the LangGraph checkpoint state.
3. Deletes `agent_tool_calls` and `agent_turn_stats` rows.
4. Deletes the `Conversation` row.

Orders placed through this conversation are **not** affected.

---

## 8. Socket.IO Chat Protocol (`/chat`)

The chat UI connects to the Socket.IO server on the root namespace.

### `GET /chat`
Auth: User. Renders the customer-facing chat UI.

### `GET /chat/admin`
Auth: Admin. Renders the admin chat playground.

### `GET /chat/ping`
Public. Health check. Returns upload capabilities and auth state.

### `GET /chat/history/<int:conversation_id>`
Auth: User. Returns the message transcript for a previous conversation (same format as admin transcript endpoint, user-scoped).

### `POST /chat/upload`
Auth: User. Accepts a multipart form upload. Stores image bytes in memory with a 10-minute TTL.

**Response:** `{ "attachment_id": "uuid" }`

### Socket.IO Events

#### Client → Server

**`connect`**

Evaluated on every socket connection. Joins the `user_<id>` room. Triggers a sweep of timed-out pending confirmations.

---

**`user_message`**

```json
{
  "conversation_id": 42,
  "content": "Show me wireless headphones under $100",
  "attachment_id": "optional-uuid"
}
```

- `conversation_id` may be `null` or omitted to start a new thread.
- `attachment_id` references a previously uploaded image (requires `vision_capable: true`).
- Rejected with `chat_error` code `busy` if the user already has a running turn.
- Launches `_run_turn()` in a background task.

---

**`confirmation_response`**

```json
{
  "request_id": "uuid",
  "conversation_id": 42,
  "accepted": true
}
```

Resolves a pending graph interrupt. Must arrive before the `confirmation_timeout_seconds` deadline. Launches `_run_resume()` in a background task.

---

#### Server → Client (emitted to `user_<id>` room)

**`connected`**
```json
{
  "user_id": 5,
  "uploads_enabled": false,
  "max_upload_mb": 5,
  "allowed_upload_extensions": [".png", ".jpg", ".jpeg", ".webp", ".gif"],
  "max_message_chars": 8000,
  "confirmation_timeout_seconds": 600
}
```

**`conversation_started`**
```json
{ "conversation_id": 42 }
```
Emitted when a `null` conversation ID is auto-assigned a new thread.

**`agent_status`**
```json
{ "phase": "thinking", "tool": null, "conversation_id": 42 }
```
`phase` values: `safety_check`, `classifying`, `retrieving`, `thinking`, `running <tool>`, `resuming`, `done`.

**`agent_note`**
```json
{ "content": "I'll add that to your cart now.", "conversation_id": 42 }
```
Interim text the model produced alongside a tool call. Shown as a transient status bubble.

**`display_product_carousel`**
```json
{ "product_ids": [1, 5, 12], "conversation_id": 42 }
```
Instructs the UI to fetch and render product cards via `GET /lookup/products?ids=1,5,12`.

**`get_user_confirmation`**
```json
{
  "request_id": "uuid",
  "tool": "add_to_cart",
  "message": "Add 2 × Wireless Headphones (49.99) to your cart?",
  "args": { "product_id": 5, "quantity": 2 },
  "expires_at": "2026-09-17T07:00:00Z"
}
```
The graph has paused for user confirmation. The UI must render an Accept/Decline overlay. The client must respond with `confirmation_response` before `expires_at`.

**`confirmation_resolved`**
```json
{
  "request_id": "uuid",
  "accepted": true,
  "reason": "accepted"
}
```
`reason` values: `accepted`, `declined`, `timeout`, `superseded`, `deleted`.

**`chat_message`**
```json
{
  "role": "ai",
  "content": "Here are some wireless headphones under $100...",
  "conversation_id": 42,
  "usage": { "input_tokens": 512, "output_tokens": 128 }
}
```

**`chat_error`**
```json
{ "code": "busy", "message": "...", "conversation_id": 42 }
```
`code` values: `busy`, `loop_limit`, `internal_error`, `auth_required`.

---

## 9. Facebook Messenger Webhook (`/webhook/`)

The webhook blueprint is disabled by default. Enable via `meta_config.enabled: true` in `config.yaml` and uncomment the registration in `routes/__init__.py`.

### `GET /webhook`
Public. Facebook webhook verification. Validates `hub.verify_token` against `META_VERIFY_TOKEN`.

### `POST /webhook`
Public. The event sink.

1. Verifies `X-Hub-Signature-256` HMAC.
2. Deduplicates by MID (message ID) + timestamp using an LRU-bounded `OrderedDict`.
3. Skips echo entries (`is_echo: true`).
4. Enqueues text messages and postback events onto a per-PSID sequential queue.
5. Returns HTTP 200 immediately (Meta times out at ~20s; LLM turns take longer).

Confirmation postbacks use the format: `CONFIRM:<request_id>:ACCEPT` or `CONFIRM:<request_id>:DECLINE`.
