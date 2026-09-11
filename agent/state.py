from typing import Annotated, Optional, Literal, TypedDict
from langgraph.graph.message import add_messages
from langchain_core.messages import AnyMessage


class AgentState(TypedDict):
    """
    State representing the entire AI conversation and workflow context.

    `intent` is (re)computed per user turn by the guard node; it stays None
    until the first classification. HITL is handled via langgraph's dynamic
    `interrupt()` payloads, so no explicit confirmation flag is required here.
    """
    messages: Annotated[list[AnyMessage], add_messages]
    intent: Optional[Literal["sales", "customer_service", "malicious"]]
    user_id: Optional[int]