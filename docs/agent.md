# Agent Internals

This document details the LangGraph agent: its state schema, every node's responsibility, the tool registration system, the safety pipeline, and the Human-in-the-Loop confirmation flow.

---

## 1. State Schema (`agent/state.py`)

`AgentState` is a `TypedDict` persisted per conversation thread by the `SqliteSaver` checkpointer.

| Field | Type | Description |
|---|---|---|
| `messages` | `List[Any]` (add_messages reducer) | Full message transcript. The `add_messages` reducer merges new messages into the list rather than replacing it — this is what gives LangGraph its append-only conversation memory. |
| `user_id` | `int` | Authenticated user ID. Set once by the transport layer and never overwritten by the model. The only authoritative source of identity for tools. |
| `user_name` | `Optional[str]` | Display name injected into the system prompt. |
| `intent` | `Optional[str]` | Classification from the latest turn. Controls which toolset is offered and which guidance is injected into the system prompt. `None` means classification failed — full toolset fallback. |
| `guard_verdict` | `Optional[Dict]` | Latest guard result: `{blocked, reason, confidence, method}`. |
| `rag_context` | `Optional[str]` | Retrieved knowledge base excerpts — only populated for `customer_service` intent turns. |
| `rag_unavailable` | `bool` | Set to `True` when RAG retrieval was attempted and failed. Triggers a degraded-mode prompt. |
| `declined_calls` | `List[Dict[str, str]]` | Records of calls the user refused this turn: `[{fingerprint, anchor}]`. `anchor` is scoped to the current user message, so a later explicit request gets a fresh confirmation prompt. |

### Structured Output Schemas

Two Pydantic models serve as structured-output targets for classifier LLM calls:

- **`GuardVerdict`** — `{blocked: bool, reason: str, confidence: float}`. Used by the guard node.
- **`IntentClassification`** — `{intent: str, confidence: float, reasoning: str}`. Used by the classify node.

---

## 2. Intent Taxonomy

The classifier selects exactly one intent from:

| Intent | Toolset | Description |
|---|---|---|
| `product_browsing` | search_products, get_product_details, search_knowledge_base | Catalog exploration and recommendations |
| `cart_management` | search_products, get_product_details, view_cart, add_to_cart, update_cart_quantity, remove_from_cart | Cart mutations |
| `checkout` | search_products, get_product_details, view_cart, checkout | Order placement |
| `order_status` | list_recent_orders, get_order_status | Post-purchase enquiries |
| `customer_service` | search_products, get_product_details, search_knowledge_base | Policy, FAQ, and company questions — also triggers RAG pre-fetch |
| `general` | search_knowledge_base | Greetings, small talk |

Unknown intent (`None`) → all registered tools.

---

## 3. Graph Nodes

### 3.1 `guard` — Prompt Injection Defense

Two-stage, fail-open:

1. **Heuristic scan** (`_heuristic_injection`): 14 compiled regexes covering common injection patterns ("ignore previous instructions", "DAN mode", "reveal system prompt", etc.) plus detection of suspiciously long base64 blobs (≥ 400 chars). Zero latency, zero API cost.

2. **LLM classifier** (`llm.with_structured_output(GuardVerdict)`): Called only if heuristics pass. Uses the guard system prompt which specifies exactly what to flag and what NOT to flag (normal shopping complaints, urgent language, off-topic questions).

3. **Fail-open on error**: If the classifier call throws (network, provider outage), `verdict = {blocked: False, method: "fail_open"}`. Availability is prioritized over blocking — a live shopping assistant beats a silently broken one.

The guard also respects `config.agent.guard_enabled` as a runtime toggle (hot-reloadable).

### 3.2 `classify_intent` — Routing

Calls `llm.with_structured_output(IntentClassification)` with the last 8 messages as transcript context. On failure, returns `{intent: None}`, which maps to the full toolset — a conservative fallback.

### 3.3 `retrieve_knowledge` — RAG Pre-fetch

Only runs on `customer_service` turns. Issues `RAGManager.search(query, k=4)` and formats the top 4 chunks into a numbered block injected into the system prompt. If retrieval fails, sets `rag_unavailable: True` which triggers a degraded-mode prompt ("cannot verify store specifics right now").

This pre-fetch is separate from the `search_knowledge_base` tool — the tool is for explicit model-driven queries; this node front-loads relevant context before the first LLM call.

### 3.4 `agent` — LLM Reasoning

Assembles the system prompt via `build_system_prompt(state)`:
1. Company/tone identity from `SystemContext`.
2. Today's date (for temporal reasoning).
3. Customer name if present.
4. Intent-specific guidance from `INTENT_GUIDANCE` (or `FALLBACK_GUIDANCE`).
5. RAG context block (customer_service only).
6. `OPERATING_RULES` (tool call protocol, confirmation semantics, error handling instructions).

Calls `llm.bind_tools(toolspecs).invoke(messages)`. Any text the model produces alongside a tool call is emitted as an `agent_note` event — shown to the user in real time as an interim status ("Adding that to your cart…").

### 3.5 `execute_tools` — Tool Execution

Processes **one tool call per super-step** from the last `AIMessage`. This is the core design that makes confirmation interrupts safe:

```
call = _unanswered_call(state)
   if call is None:
       return {}   # routing returns to agent

   spec = TOOL_REGISTRY[call.name]

   # Confirmation gate
   if spec.sensitive and name in config.agent.sensitive_tool_names:
       if prior_decline_exists: return error envelope
       approved = interrupt({...})          # PAUSES graph here
       if not approved: record decline, return error envelope

   # Retry loop
   for attempt in range(retries + 1):
       result = spec.tool.invoke(args)
       if success or not retryable: break
       sleep(backoff * attempt)

   return {messages: [ToolMessage(result)]}
```

The routing edge `_route_after_tools` checks if there are more unanswered calls — if so, loops back to `execute_tools`; otherwise returns to `agent`.

---

## 4. Tool Registration (`agent/tooling.py`)

The `@agent_tool` decorator does three things:
1. Wraps the function with `langchain_tool` to generate a `StructuredTool` (schema inferred from type annotations and docstring).
2. Marks parameters annotated with `InjectedToolArg` as hidden from the model's tool schema — `user_id` is the canonical example.
3. Registers the `ToolSpec` in `TOOL_REGISTRY` (a module-level dict keyed by function name).

**`ToolSpec` fields:**

| Field | Type | Description |
|---|---|---|
| `tool` | `StructuredTool` | The LangChain tool object passed to `bind_tools()` |
| `fn` | `Callable` | The raw function (not used at runtime, kept for introspection) |
| `name` | `str` | Registered name (must be unique) |
| `sensitive` | `bool` | Whether the tool requires confirmation before execution |
| `needs_user_id` | `bool` | Whether `user_id` must be injected by `execute_tools` |
| `confirmation` | `Optional[Callable]` | Renderer for the human-facing confirmation message |

---

## 5. Tool Result Envelopes

Every tool returns a uniform envelope:

```python
# Success
{"status": "success", "data": {...}, "ui_event": {...}?}

# Error
{"status": "error", "error": {"code": str, "message": str, "retryable": bool, "hint": str}}
```

Error codes the model is trained to act on:

| Code | Retryable | Meaning |
|---|---|---|
| `user_declined` | No | User refused a confirmation prompt |
| `declined_earlier` | No | Same call was refused earlier this turn |
| `out_of_stock` | No | Insufficient inventory |
| `not_found` | No | Record doesn't exist |
| `invalid_arguments` | No | Argument type/value error |
| `cart_empty` | No | Checkout on empty cart |
| `rag_unavailable` | **Yes** | Vector store transient failure |
| `internal_error` | **Yes** | Unexpected tool exception |
| `unknown_tool` | No | Model called a non-existent tool |

---

## 6. Tools Reference

### Catalog Tools (`agent/tools/catalog.py`)

**`search_products`**
- Args: `query`, `category`, `tags` (comma-separated), `price_min`, `price_max`, `max_results` (≤ 8), `retries`
- Performs case-insensitive ILIKE filtering with SQL LIKE escape. Name matches are prioritized over description matches.
- On success, emits a `display_product_carousel` UI event with the returned product IDs.

**`get_product_details`**
- Args: `product_id`, `retries`
- Returns full product card including description (truncated to 1200 chars).

### Cart & Checkout Tools (`agent/tools/cart.py`)

**`view_cart`** — Read-only; no confirmation required.

**`add_to_cart`** — `sensitive=True`. Requires confirmation. Checks `stock_quantity >= current + requested` before inserting/updating `CartItem`. Confirmation message resolves the product name by ID.

**`update_cart_quantity`** — Not sensitive. Re-validates stock bounds.

**`remove_from_cart`** — Not sensitive. Soft-fail with `not_found` if the item isn't in the cart.

**`checkout`** — `sensitive=True`. Requires confirmation. Uses the atomic `UPDATE ... WHERE stock_quantity >= qty` pattern. Any stock failure rolls back the entire order.

### Order Tools (`agent/tools/orders.py`)

Both `list_recent_orders` and `get_order_status` enforce `WHERE customer_id = user_id` — users can only see their own orders. `get_order_status` also raises `RecordNotFoundError` if the order belongs to a different user.

### Knowledge Tool (`agent/tools/knowledge.py`)

**`search_knowledge_base`** — Thin wrapper over `RAGManager.search()`. Imports `get_rag_manager` lazily to keep the import graph acyclic.

---

## 7. Human-in-the-Loop Confirmation Flow

```
execute_tools detects sensitive call
  │
  ▼
interrupt({request_id, tool, args, message}) ← graph PAUSES here
  │
  │ LangGraph serializes state to SqliteSaver checkpoint
  │ execute_tools returns, background thread exits
  │
  ▼
[routes/chat.py] processes interrupt value
  │
  ├─ Stores record in _PENDING: {request_id → {thread_id, timeout, ...}}
  ├─ Emits Socket.IO event "get_user_confirmation" to user's room
  └─ Returns (no worker held open)

Client renders Accept/Decline overlay
  │
Client emits "confirmation_response" {request_id, accepted}
  │
  ▼
[on_confirmation_response()]
  │
  ├─ Validates request_id exists and hasn't timed out
  ├─ Emits "confirmation_resolved" to the user's room
  └─ Launches _run_resume() in background:
       │
       ▼
   graph.stream(Command(resume=accepted), config={thread_id})
       │
       ▼
   execute_tools resumes from checkpoint
   approved = interrupt(...)  ←  receives True or False
       │
       ├─ True  → executes tool, continues graph
       └─ False → records decline in declined_calls, returns error envelope
```

**Timeout handling:** A background sweep in `on_connect` checks `_PENDING` for entries older than `confirmation_timeout_seconds`. Timed-out confirmations are automatically declined with `Command(resume=False)` and `confirmation_resolved {reason: "timeout"}` is emitted.

**Supersession:** If a new user message arrives while a confirmation is pending on the same thread, the pending confirmation is auto-declined before processing the new message.

---

## 8. Stats Collection (`agent/stats.py`)

`TurnStatsCollector` is instantiated at the start of each turn and fed chunks from the graph stream:

- **`note_custom(payload)`** — processes `custom` stream chunks: tool_status events → appends to `tool_calls` list; carousel events → increments counter; agent_note events → increments counter.
- **`note_update(chunk)`** — processes `values` stream chunks: counts AI messages, accumulates token usage from `usage_metadata`.
- **`finalize(status, values, error)`** — persists to `agent_turn_stats` + `agent_tool_calls` SQL tables (best-effort, never raises), then pushes to in-memory `StatsRegistry`.

`StatsRegistry` is a process-local singleton holding the last 50 turns per user (as a bounded `deque`) plus global counters. The `/admin/stats/overview` endpoint reads from both the registry (fast, in-memory tail) and the SQL tables (historical aggregates).
