"""
RAG tool: knowledge base search. get_rag_manager is imported
lazily to keep the import graph acyclic.
"""

from __future__ import annotations

import logging

from agent.tooling import agent_tool, error, success

logger = logging.getLogger(__name__)


@agent_tool()
def search_knowledge_base(query: str, retries: int | None = None) -> dict:
    """
    Search the store knowledge base (policies, FAQs, shipping and return
    rules, company information). Prefer this for any policy or company
    question rather than answering from memory.

    The optional `retries` argument sets automatic retries on transient failures.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("query must not be empty.")

    try:
        from database.rag_manager import get_rag_manager
        docs = get_rag_manager().search(query, k=4)
    except Exception as exc:
        logger.warning("Knowledge base search failed: %s", exc)
        return error("rag_unavailable", f"Knowledge base search failed: {exc}",
                     retryable=True,
                     hint="The knowledge base may be temporarily unavailable; retry "
                          "once, or answer from general knowledge and say you cannot "
                          "verify store specifics right now.")

    results = [{"title": (d.metadata or {}).get("title", "untitled"),
                "doc_type": (d.metadata or {}).get("doc_type", ""),
                "content": (d.page_content or "")[:800]}
               for d in docs]
    if not results:
        return success({"results": [], "message": "No knowledge base documents matched."})
    return success({"results": results, "count": len(results)})