# Documentation Index

Welcome to the E-Commerce AI Assistant documentation. This index maps each document to its purpose — start with `architecture.md` if you are new to the codebase.

---

## Documents

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | High-level subsystem diagram, request lifecycle walkthrough, dual-write data design, security model, config hot-reload, and the Messenger transport adapter |
| [agent.md](agent.md) | LangGraph graph topology in detail: every node's logic, state schema, intent taxonomy, tool registration system (`@agent_tool`), result envelopes, all tool implementations, and the full Human-in-the-Loop confirmation flow |
| [api.md](api.md) | Complete HTTP endpoint reference for all blueprints, plus the Socket.IO event protocol (client→server and server→client events, payloads, and semantics) |
| [configuration.md](configuration.md) | Every `config.yaml` key with type, default, and description; all environment variables; hot-reload behavior |
| [deployment.md](deployment.md) | Production hardening checklist, single-worker vs. multi-worker scaling path, database recommendations, security guidance, Facebook Messenger enablement steps |

The [`routes/ROUTES.md`](../routes/ROUTES.md) file inside the source tree is a condensed quick-reference route table generated alongside the route implementations.
