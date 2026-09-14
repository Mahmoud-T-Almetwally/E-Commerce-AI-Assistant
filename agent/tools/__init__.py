"""Tool registry and per-intent toolsets. Importing submodules registers tools."""

from typing import Dict, List, Optional

from agent.tooling import ToolSpec, TOOL_REGISTRY

from . import catalog, cart, knowledge, orders  # noqa: F401

_ALL = tuple(TOOL_REGISTRY)

_TOOLSETS: Dict[str, tuple] = {
    "product_browsing": ("search_products", "get_product_details", "search_knowledge_base"),
    "cart_management": ("search_products", "get_product_details", "view_cart",
                        "add_to_cart", "update_cart_quantity", "remove_from_cart"),
    "checkout": ("search_products", "get_product_details", "view_cart", "checkout"),
    "order_status": ("list_recent_orders", "get_order_status"),
    "customer_service": ("search_products", "get_product_details", "search_knowledge_base"),
    "general": ("search_knowledge_base",),
}


def get_toolset(intent: Optional[str]) -> List[ToolSpec]:
    """Tools offered to the agent for an intent. Unknown/None intent => everything."""
    names = _TOOLSETS.get(intent or "", _ALL)
    return [TOOL_REGISTRY[name] for name in names if name in TOOL_REGISTRY]