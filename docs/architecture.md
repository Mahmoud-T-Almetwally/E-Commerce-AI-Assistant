# Architecture

This document covers the system architecture, data flow through a single chat turn, key design decisions, and the relationships between the major subsystems.

---

## 1. High-Level Subsystems

```
┌────────────────────────────────────────────────────────────────┐
│                       Presentation Layer                       │
│   Web UI (Jinja2 + Vanilla JS + Socket.IO)                     │
│   Admin Dashboard (server-rendered Jinja2)                     │
│   Facebook Messenger (webhook transport)                       │
└────────────────┬───────────────────────────────────────────────┘
                 │
┌────────────────▼───────────────────────────────────────────────┐
│                     Flask Application Layer                    │
│                                                                │
│  Blueprints:                                                   │
│    auth       — session-based identity                         │
│    store      — storefront + cart + checkout (web)             │
│    admin      — product/order/customer management              │
│    chat       — Socket.IO event handlers + HTTP helpers        │
│    rag        — knowledge document CRUD                        │
│    llm_config — hot-reload LLM/agent configuration             │
│    stats      — telemetry queries                              │
│    lookup     — autocomplete read endpoints                    │
│    webhook    — Facebook Messenger transport (optional)        │
│                                                                │
│  Extensions: CSRFProtect, Flask-Limiter, Flask-SocketIO        │
└────────────────┬───────────────────────────────────────────────┘
                 │
┌────────────────▼───────────────────────────────────────────────┐
│                       Agent Layer (LangGraph)                  │
│                                                                │
│  Nodes:                                                        │
│    guard           — prompt-injection detection                │
│    classify_intent — intent routing                            │
│    retrieve_knowledge — RAG pre-fetch for customer_service     │
│    agent           — LLM call with bound tools                 │
│    execute_tools   — one tool per super-step, with retries     │
│                       and confirmation interrupts              │
│                                                                │
│  Persistence: SqliteSaver checkpointer (per thread_id)         │
└───────┬───────────────────────┬────────────────────────────────┘
        │                       │
┌───────▼──────────┐   ┌────────▼───────────────────────────────┐
│   LLM Provider   │   │          Data Layer                    │
│                  │   │                                        │
│  ModelFactory    │   │  SQLAlchemy 2.0 (SQLite by default)    │
│  ─────────────── │   │    users, products, cart_items         │
│  openai          │   │    orders, order_items                 │
│  anthropic       │   │    conversations                       │
│  groq            │   │    knowledge_documents                 │
│  google          │   │    agent_turn_stats                    │
│  huggingface     │   │    agent_tool_calls                    │
│  local (Ollama)  │   │    messenger_identities                │
│                  │   │                                        │
│  Embedding model │   │  ChromaDB (vector store)               │
│  (same factory)  │   │    collection: ecommerce_knowledge     │
└──────────────────┘   └────────────────────────────────────────┘
```

---

## 2. Request Lifecycle — Chat Turn

Below is the full call path for a single user message over the Socket.IO transport.

```
Client emits "user_message" {conversation_id, content}
  │
  ▼
[routes/chat.py] on_user_message()
  ├─ Authenticates via get_current_user() (session cookie)
  ├─ Validates concurrency (_ACTIVE_TURNS set — one turn per user)
  ├─ Provisions/looks up Conversation row in SQLite
  ├─ Builds HumanMessage (optionally with image content block)
  └─ Launches _run_turn() in socketio.start_background_task()
       │
       ▼
   [_run_turn()]
       ├─ Instantiates TurnStatsCollector
       ├─ Calls graph.stream({messages, user_id, user_name},
       │    config={thread_id}, stream_mode=["values","custom"])
       │       │
       │       ▼
       │  [LangGraph graph execution]
       │       │
       │       ├─ guard_node()
       │       │     ├─ Heuristic regex scan on raw text
       │       │     └─ LLM structured-output call (GuardVerdict)
       │       │         ► blocked=True → emit AIMessage(REFUSAL) → END
       │       │         ► blocked=False → continue
       │       │
       │       ├─ classify_node()
       │       │     └─ LLM structured-output call (IntentClassification)
       │       │         ► intent → route to retrieve_knowledge (customer_service)
       │       │                 or agent (all others)
       │       │
       │       ├─ retrieve_knowledge_node()  [customer_service only]
       │       │     └─ RAGManager.search(query, k=4)
       │       │         → rag_context injected into next system prompt
       │       │
       │       ├─ agent_node()
       │       │     ├─ build_system_prompt(state) → intent guidance + RAG
       │       │     ├─ get_toolset(intent) → filtered ToolSpec list
       │       │     └─ llm.bind_tools(specs).invoke(messages)
       │       │         → AIMessage (possibly with tool_calls)
       │       │
       │       └─ execute_tools()  [loops until no unanswered tool calls]
       │             ├─ Finds first unanswered tool_call from last AIMessage
       │             ├─ [sensitive tools] → interrupt() → pause graph
       │             │     Client emits "confirmation_response"
       │             │       └─ _run_resume() replays from checkpoint
       │             ├─ Executes tool.invoke(args) with retry budget
       │             └─ Returns ToolMessage → loops to agent_node
       │
       ├─ Processes stream chunks → emits Socket.IO events:
       │     agent_status / agent_note / display_product_carousel /
       │     get_user_confirmation / chat_message / chat_error
       │
       └─ TurnStatsCollector.finalize() → persist to agent_turn_stats
```

---

## 3. The LangGraph Graph Topology

```
START
  │
  ▼
guard ─── blocked ──────────────────────────────► END
  │
  │ (not blocked)
  ▼
classify_intent
  │
  ├─ customer_service ──► retrieve_knowledge ──► agent
  │                                                │
  └─ all other intents ──────────────────────────► agent
                                                   │
                                        ┌──────────┤ tool_calls present?
                                        │ yes      │ no
                                        ▼          ▼
                                 execute_tools     END
                                        │
                                        │ more unanswered calls?
                                        ├─ yes ──► execute_tools (self-loop)
                                        └─ no  ──► agent
```

**One tool call per super-step.** `execute_tools` only processes the first unanswered tool call from the last `AIMessage`. This is intentional: if a confirmation interrupt fires and the server process restarts, replaying from the checkpoint re-enters `execute_tools` with the same pending call — no mutation has occurred yet, so the replay is idempotent.

---

## 4. Data Layer Design

### 4.1 Dual-Write: SQLite + ChromaDB

The relational database (SQLite via SQLAlchemy) is the **source of truth** for all business data. ChromaDB holds embeddings derived from `KnowledgeDocument` rows — it is a secondary index, never the primary record.

Any write to `KnowledgeDocument` is followed immediately by a call to `RAGManager.add_document()` or `delete_document()`. If the vector store call fails after the SQL commit, the `_reconcile_vector_store()` function (run at startup) detects the drift via content-hash comparison and heals it — zero embedding API calls for unchanged documents.

### 4.2 Optimistic Inventory Locking

The checkout path (both web `routes/store.py` and agent `agent/tools/cart.py`) uses a conditional `UPDATE` rather than an application-level `SELECT FOR UPDATE`:

```sql
UPDATE products
   SET stock_quantity = stock_quantity - :qty
 WHERE id = :id AND stock_quantity >= :qty
```

A `rowcount == 0` result (SQLAlchemy `update().rowcount`) signals a concurrent depletion and rolls back the entire order atomically.

### 4.3 Conversation Threading

Each chat session maps to a `Conversation` row with a `thread_id` string (UUID). The LangGraph `SqliteSaver` checkpointer uses `thread_id` as the partition key — all graph state for a conversation lives in the checkpoints database under that key. The `Conversation` table in the application database is only for bookkeeping, linking threads to users and providing the admin UI with a list of sessions.

---

## 5. Security Design

| Concern | Mechanism |
|---|---|
| Prompt injection | Two-layer guard: heuristic regex + LLM classifier. Fail-open on classifier error (availability > blocking). |
| Identity injection | `user_id` is **never** passed by the model; `execute_tools` injects it from authenticated `AgentState`. `InjectedToolArg` hides the parameter from the model's tool schema. |
| CSRF | `Flask-WTF CSRFProtect` on all POSTs. Socket.IO polling transport is explicitly exempted (see `_exempt_socketio_from_csrf`). |
| Rate limiting | `Flask-Limiter` on login (10/min) and register (5/min). In-memory by default; swap to Redis in production. |
| Session security | `HttpOnly`, `SameSite=Lax`, configurable `Secure`. 12-hour default lifetime. |
| File uploads | In-memory only — never written to disk. Size cap (`max_file_size_mb`), extension allowlist, MIME validation. |
| Secrets | API keys and tokens sourced exclusively from environment variables. `save_config()` strips all secret fields before writing YAML. |
| Webhook HMAC | Facebook Messenger payloads verified with `X-Hub-Signature-256: sha256=HMAC_SHA256(app_secret, raw_body)`. |

---

## 6. Configuration Hot-Reload

`utils/config.py` exposes a module-level `config` singleton. `save_config()` rebinds every field on the live object in-place (using `object.__setattr__` to bypass Pydantic's immutability). Modules that imported `from utils.config import config` continue referencing the same object — no reload needed.

The agent graph is cached by LLM fingerprint (`_llm_fingerprint()` is a tuple of all LLM parameters). Changing any LLM setting via the admin UI causes `reset_agent_graph()` to clear the cache; the next turn rebuilds and compiles a new graph against the updated LLM client.

---

## 7. Facebook Messenger Transport

`routes/webhook.py` is a second transport adapter for the same agent. It implements:

- **HMAC signature verification** on every POST.
- **At-least-once dedup** via an LRU-bounded `OrderedDict` keyed on message MID + timestamp.
- **Per-PSID sequential queues** — messages from the same user are drained one at a time to prevent confirmation races (Messenger users frequently multi-text).
- **Protocol translation** — `agent_status` → typing indicator, `chat_message` → text (chunked to 2000 chars), `display_product_carousel` → generic template cards, `get_user_confirmation` → button template (Accept/Decline postbacks).
- **Auto-provisioning** — first contact from a PSID creates a `MessengerIdentity` + a synthetic `User` with no password hash. A future account-linking flow can update the `user_id` FK without touching conversation history.

The webhook blueprint is commented out by default in `routes/__init__.py` and must be explicitly enabled once `meta_config.enabled: true` and the three Meta secrets are set.
