from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, TypedDict


MessageType = Literal["status", "chat", "itinerary", "error"]
RouteType = Literal["collect_info", "general_query", "plan_trip", "review_trip", "revise_trip", "finalize_trip"]


class ChatMessage(TypedDict, total=False):
    role: Literal["system", "user", "assistant"]
    content: str
    type: MessageType
    metadata: Dict[str, Any]


class ClientEvent(TypedDict, total=False):
    type: MessageType
    thread_id: str
    content: str
    stage: str
    role: str
    metadata: Dict[str, Any]


class AgentState(TypedDict, total=False):
    thread_id: str
    trip_id: Optional[int]
    session_key: str

    destination: Optional[str]
    budget: Optional[float]
    budget_currency: Optional[str]
    pending_budget_amount: Optional[float]
    duration: Optional[int]
    travel_style: Optional[str]

    missing_fields: List[str]
    route: RouteType
    status: str
    last_question: str
    current_message: str
    user_action: str
    user_feedback: str
    planning_notes: str
    refined_prompt: str
    error_message: Optional[str]

    ai_draft: str
    final_itinerary: str
    general_answer: str

    extracted_fields: List[str]
    chat_history: List[ChatMessage]
    last_emitted_message: ClientEvent
    suppress_ws_updates: bool
