# E-Commerce AI Assistant

A production-grade, AI-powered shopping and customer service platform built on **Flask**, **LangGraph**, and **SQLAlchemy**. The system embeds a stateful conversational agent directly into an e-commerce storefront, exposing the same agent over both a real-time web chat (Socket.IO) and a Facebook Messenger webhook adapter.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Key Features](#key-features)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the Application](#running-the-application)
- [Seeding Demo Data](#seeding-demo-data)
- [Running Tests](#running-tests)
- [Documentation](#documentation)
- [License](#license)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                     Flask Application                   │
│                                                         │
│   ┌───────────┐   ┌──────────────┐   ┌──────────────┐   │
│   │  Web UI   │   │   REST API   │   │  Admin Panel │   │
│   │ (Socket.IO│   │  Blueprints  │   │  /admin/*    │   │
│   │  chat.py) │   │              │   │              │   │
│   └─────┬─────┘   └──────┬───────┘   └──────┬───────┘   │
│         │                │                  │           │
│         └────────────────┼──────────────────┘           │
│                          │                              │
│              ┌───────────▼────────────┐                 │
│              │    LangGraph Agent     │                 │
│              │  guard → classify →    │                 │
│              │  retrieve → agent ↔    │                 │
│              │  execute_tools (loop)  │                 │
│              └───────────┬────────────┘                 │
│                          │                              │
│          ┌───────────────┼───────────────┐              │
│          │               │               │              │
│   ┌──────▼──────┐ ┌──────▼──────┐ ┌──────▼─────┐        │
│   │   SQLite    │ │  ChromaDB   │ │  LLM API   │        │
│   │  (SQLAlch.) │ │  (vectors)  │ │  Provider  │        │
│   └─────────────┘ └─────────────┘ └────────────┘        │
└─────────────────────────────────────────────────────────┘
         ▲ also reachable via
         │
┌────────┴──────────┐
│ Messenger Webhook │  (routes/webhook.py — optional)
│  (Meta Graph API) │
└───────────────────┘
```

The agent graph is a stateful **LangGraph** `StateGraph` compiled with a **SQLite checkpointer**. Every conversation thread is persisted across requests; the graph can pause mid-execution on sensitive tool calls (Human-in-the-Loop interrupts) and resume safely from the checkpoint without replaying already-executed mutations.

See [`docs/architecture.md`](docs/architecture.md) for a deeper walkthrough.

---

## Key Features

- **Stateful conversational agent** — LangGraph graph with per-conversation SQLite checkpointing; survives server restarts.
- **Multi-layer safety** — heuristic prompt-injection detection + LLM guard classifier, fail-open by design.
- **Intent-aware toolset** — the agent only receives the tools relevant to the classified intent, reducing hallucination surface.
- **Human-in-the-Loop confirmations** — `add_to_cart` and `checkout` pause the graph and require explicit user approval before mutations commit.
- **RAG knowledge base** — ChromaDB vector store backed by SQLite; hash-based drift reconciliation on startup eliminates stale embeddings without wasted API calls.
- **Hot-reloadable LLM config** — swap provider/model at runtime via the admin UI; the compiled graph is rebuilt on the next turn.
- **Dual transport** — identical agent graph served over Socket.IO (web) and Facebook Messenger (webhook).
- **Multi-provider support** — OpenAI, Anthropic, Groq, Google Gemini, HuggingFace, and local Ollama models via a unified `ModelFactory`.
- **Admin dashboard** — KPI overview, revenue charts, per-conversation telemetry, tool call analytics, and full conversation transcripts.
- **Atomic inventory** — checkout uses optimistic row-level locking to prevent overselling under concurrent load.

---

## Project Structure

```
E-Commerce-AI-Assistant/
├── app.py                  # Application factory (create_app)
├── config.yaml             # Non-secret runtime configuration
├── requirements.txt        # Pinned dependency set
├── .env.example            # Template for required secrets
│
├── agent/                  # LangGraph agent — graph, tools, prompts
│   ├── graph.py            # Graph topology and node implementations
│   ├── state.py            # AgentState TypedDict + structured output schemas
│   ├── prompts.py          # System prompt assembly + guard/intent prompts
│   ├── providers.py        # LLM/embedding provider abstraction (ModelFactory)
│   ├── tooling.py          # @agent_tool decorator, result envelopes, retry helpers
│   ├── checkpointer.py     # Lazy SqliteSaver singleton
│   ├── stats.py            # Per-turn telemetry collection and in-memory registry
│   └── tools/              # Tool implementations (one file per domain)
│       ├── catalog.py      # search_products, get_product_details
│       ├── cart.py         # view_cart, add_to_cart, update_cart_quantity, remove_from_cart, checkout
│       ├── orders.py       # list_recent_orders, get_order_status
│       └── knowledge.py    # search_knowledge_base
│
├── database/               # Data layer
│   ├── models.py           # SQLAlchemy 2.0 ORM models
│   ├── db_setup.py         # Engine, SessionLocal, init_db()
│   └── rag_manager.py      # RAGManager: ChromaDB CRUD + reconcile/resync
│
├── routes/                 # Flask blueprints
│   ├── __init__.py         # Blueprint registration (register_routes)
│   ├── auth.py             # /login, /register, /logout
│   ├── store.py            # /products, /cart, /checkout, /orders
│   ├── admin.py            # /admin/* management endpoints
│   ├── chat.py             # Socket.IO chat protocol + HTTP helpers
│   ├── rag.py              # /admin/knowledge CRUD
│   ├── llm_config.py       # /admin/llm-config hot-reload API
│   ├── stats.py            # /admin/stats/* + /admin/conversations/*
│   ├── lookup.py           # /lookup/* autocomplete endpoints
│   ├── webhook.py          # Facebook Messenger transport (optional)
│   └── ROUTES.md           # HTTP + Socket.IO endpoint reference
│
├── utils/                  # Cross-cutting utilities
│   ├── config.py           # Pydantic config models + YAML load/save
│   ├── auth.py             # login_required / admin_required decorators
│   ├── extensions.py       # Flask extension singletons (csrf, limiter, socketio)
│   ├── exceptions.py       # Typed exception hierarchy
│   ├── meta_client.py      # Facebook Graph API adapter (MetaMessenger)
│   ├── file_extraction.py  # In-memory .txt/.pdf/.docx text extraction
│   ├── seed_data.py        # Deterministic demo data seeder
│   ├── pagination.py       # Shared pagination helper
│   ├── sorting.py          # Shared sorting helper
│   └── sanitizers.py       # SQL LIKE escape helper
│
├── templates/              # Jinja2 templates
│   ├── base.html           # Root layout
│   ├── macros.html         # Shared UI macros
│   ├── admin/              # Admin dashboard templates
│   ├── auth/               # Login / register templates
│   ├── chat/               # Chat UI templates (customer + admin)
│   └── store/              # Storefront templates
│
├── static/                 # CSS, JS, images
│
├── tests/                  # Pytest test suite
│   ├── conftest.py         # App fixture, test database setup
│   ├── agent/              # Unit tests for graph nodes and tools
│   └── routes/             # Integration tests for all blueprints
│
├── instance/               # Runtime data (gitignored)
│   ├── ecommerce.db        # SQLite application database
│   ├── checkpoints.sqlite  # LangGraph conversation checkpoints
│   └── chroma_db/          # ChromaDB vector store persistence
│
└── docs/                   # Extended documentation
    ├── architecture.md     # Deep-dive: graph topology, data flow, design decisions
    ├── agent.md            # Agent internals: nodes, tools, safety, confirmation flow
    ├── api.md              # HTTP endpoint and Socket.IO event reference
    ├── configuration.md    # All config keys with types, defaults, and env var overrides
    └── deployment.md       # Production hardening and deployment guidance
```

---

## Prerequisites

- Python **3.11+**
- An API key for at least one supported LLM provider (or a locally running [Ollama](https://ollama.ai/) instance)
- An API key for the embedding provider configured in `config.yaml` (defaults to `local` / Ollama)

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/Mahmoud-T-Almetwally/E-Commerce-AI-Assistant.git
cd E-Commerce-AI-Assistant

# 2. Create and activate a virtual environment
python -m venv env
source env/bin/activate        # Windows: env\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure secrets
cp .env.example .env
# Edit .env — at minimum set FLASK_SECRET_KEY and your LLM provider key.
```

---

## Configuration

Configuration is split between **`config.yaml`** (non-secret runtime knobs) and **environment variables** (secrets). The application boots successfully without a YAML file, falling back to Pydantic defaults plus any set environment variables.

### Required environment variables

| Variable | Purpose |
|---|---|
| `FLASK_SECRET_KEY` | Session signing key — **must** be changed in production |
| `OPENAI_API_KEY` | Required when `llm_config.provider: openai` |
| `ANTHROPIC_API_KEY` | Required when `llm_config.provider: anthropic` |
| `GROQ_API_KEY` | Required when `llm_config.provider: groq` |
| `GOOGLE_API_KEY` | Required when `llm_config.provider: google` |
| `HUGGINGFACEHUB_API_TOKEN` | Required when `llm_config.provider: huggingface` |

For Messenger integration, also set `META_PAGE_ACCESS_TOKEN`, `META_VERIFY_TOKEN`, and `META_APP_SECRET`.

### Key `config.yaml` sections

```yaml
llm_config:
  provider: groq          # openai | anthropic | groq | google | huggingface | local
  model_name: openai/gpt-oss-20b
  temperature: 0.2
  max_tokens: 2048
  vision_capable: false   # enables image attachment uploads in the chat UI

embedding_config:
  provider: local         # openai | huggingface | google | local (Ollama)
  model_name: text-embedding-3-small

agent:
  guard_enabled: true
  sensitive_tool_names: ["add_to_cart", "checkout"]
  confirmation_timeout_seconds: 600
```

See [`docs/configuration.md`](docs/configuration.md) for the full reference.

---

## Running the Application

```bash
# Development — uses socketio.run() with Werkzeug (NOT gunicorn/uvicorn directly)
python app.py
```

The server starts on `http://127.0.0.1:5000` by default.

> **Production note:** The Socket.IO server **must** be started via `socketio.run()`, not `app.run()`. For production, front it with a reverse proxy (nginx) and an appropriate async worker. See [`docs/deployment.md`](docs/deployment.md).

---

## Seeding Demo Data

The seeder drops and recreates all tables, inserts 50 products, 20 customers, an admin account, sample orders, and 4 knowledge documents (vectorised into ChromaDB).

```bash
python -m utils.seed_data
```

**Demo credentials (after seeding):**

| Role | Email | Password |
|---|---|---|
| Admin | `admin@store.com` | `Admin123!` |
| Customer | `customer1@example.com` | `Customer123!` |

---

## Running Tests

```bash
pytest
# or with verbose output:
pytest -v
```

The test suite uses an **in-memory SQLite database** and mocked LLM/embedding clients — no API keys are required. See [`tests/conftest.py`](tests/conftest.py) for fixture definitions.

---

## Documentation

| Document | Description |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | System architecture, data flow, and key design decisions |
| [`docs/agent.md`](docs/agent.md) | LangGraph graph internals, tool system, safety pipeline |
| [`docs/api.md`](docs/api.md) | Complete HTTP and Socket.IO API reference |
| [`docs/configuration.md`](docs/configuration.md) | Full `config.yaml` and `.env` reference |
| [`docs/deployment.md`](docs/deployment.md) | Production deployment guidance |
| [`routes/ROUTES.md`](routes/ROUTES.md) | Quick-reference route table |

---

## License

MIT — see [LICENSE](LICENSE).
