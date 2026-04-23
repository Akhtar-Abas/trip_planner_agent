from __future__ import annotations

import logging
from typing import Literal

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from .llm import build_messages, get_llm
from .state import AgentState, ClientEvent

logger = logging.getLogger(__name__)
checkpointer = MemorySaver()

REQUIRED_FIELDS = ["destination", "budget", "duration", "travel_style"]
LOCAL_EXPERTISE_PROMPT = """
You are an Elite Travel Consultant for all of Pakistan.
Your expertise covers Gilgit-Baltistan, AJK, Khyber Pakhtunkhwa, Punjab, Sindh, and Balochistan.

Communication protocol:
- Strictly communicate in English.
- Maintain a professional, helpful, and hospitable tone.
- If the user mentions a city or region in Pakistan, provide detailed local insight.
- Do not behave like a rigid form filler. Acknowledge the destination and travel style naturally before asking the next relevant question.
- Never invent missing trip parameters. Ask for them clearly when needed.
- Keep recommendations realistic for transport times, terrain, seasonality, and budget constraints.
- Mention local highlights, practical logistics, and safety or weather notes where relevant.
""".strip()

DESTINATION_INSIGHTS = {
    "skardu": "Skardu is excellent for alpine landscapes, Satpara, Shangrila, and access toward Deosai and Shigar.",
    "hunza": "Hunza is ideal for dramatic mountain views, Karimabad culture, Altit and Baltit forts, and scenic road journeys.",
    "murree": "Murree works well for short hill retreats, forested viewpoints, and easy access from Islamabad and Rawalpindi.",
    "gwadar": "Gwadar stands out for its coastal scenery, marine drive, and dramatic landscapes near Kund Malir and the Makran Coast.",
    "lahore": "Lahore is strongest for heritage, food, architecture, and a rich old-city cultural experience.",
    "kumrat": "Kumrat is known for riverside meadows, forests, and a more raw nature-focused escape in Upper Dir.",
}


def send_update_to_client(session_key: str, message: ClientEvent) -> None:
    try:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"chat_{session_key}",
            {"type": "chat_update", "data": message},
        )
    except Exception:
        logger.exception("Failed to send WebSocket update")


def should_emit_updates(state: AgentState) -> bool:
    return not state.get("suppress_ws_updates", False)


def append_chat_history(state: AgentState, *, role: str, content: str, message_type: str, metadata: dict | None = None) -> None:
    history = list(state.get("chat_history", []))
    history.append(
        {
            "role": role,
            "content": content,
            "type": message_type,
            "metadata": metadata or {},
        }
    )
    state["chat_history"] = history


def emit_client_message(state: AgentState, payload: ClientEvent) -> None:
    payload = dict(payload)
    payload.setdefault("thread_id", state.get("thread_id", ""))
    payload.setdefault("role", "assistant")
    state["last_emitted_message"] = payload
    append_chat_history(
        state,
        role="assistant",
        content=payload.get("content", ""),
        message_type=payload.get("type", "chat"),
        metadata=payload.get("metadata"),
    )
    if should_emit_updates(state):
        send_update_to_client(state["session_key"], payload)


def missing_fields(state: AgentState) -> list[str]:
    return [field for field in REQUIRED_FIELDS if not state.get(field)]


def next_question(state: AgentState) -> str:
    if state.get("pending_budget_amount") and not state.get("budget_currency"):
        amount = state["pending_budget_amount"]
        return f"You mentioned a budget of {amount:.0f}. Is that in PKR or USD?"

    prompts = {
        "destination": "Which destination in Pakistan would you like to explore?",
        "budget": "What is your total budget for this trip? Please include the currency if possible.",
        "duration": "How many days would you like this trip to be?",
        "travel_style": "What kind of trip are you looking for, such as trekking, luxury, family, culture, food, or adventure?",
    }
    remaining = missing_fields(state)
    if not remaining:
        return ""
    return prompts[remaining[0]]


def destination_acknowledgement(state: AgentState) -> str:
    destination = (state.get("destination") or "").strip()
    travel_style = (state.get("travel_style") or "").strip()
    if not destination:
        return ""

    insight = DESTINATION_INSIGHTS.get(destination.lower(), f"{destination} has strong local character and several worthwhile travel experiences.")
    if travel_style:
        return f"{destination} is a strong choice for a {travel_style.lower()} trip. {insight}"
    return insight


def is_approval_message(message: str) -> bool:
    text = (message or "").lower().strip()
    return any(token in text for token in ("approve", "approved", "yes", "looks good", "final"))


def looks_like_general_query(message: str, state: AgentState) -> bool:
    text = (message or "").lower().strip()
    if not text:
        return False

    extracted_fields = set(state.get("extracted_fields", []))
    if extracted_fields:
        return False

    question_markers = (
        "?",
        "best time",
        "weather",
        "road",
        "hotel",
        "route",
        "permit",
        "safety",
        "what",
        "where",
        "when",
        "how",
        "worth visiting",
    )
    planning_markers = (
        "plan",
        "itinerary",
        "trip",
        "visit",
        "budget",
        "days",
        "book",
    )

    has_question_signal = any(marker in text for marker in question_markers)
    has_planning_signal = any(marker in text for marker in planning_markers)
    return has_question_signal and not has_planning_signal


def router_node(state: AgentState) -> AgentState:
    current_status = state.get("status", "collecting")
    message = state.get("current_message", "")
    state["missing_fields"] = missing_fields(state)

    if current_status == "draft_ready":
        if not message:
            state["route"] = "review_trip"
        elif is_approval_message(message):
            state["user_action"] = "approve"
            state["route"] = "finalize_trip"
        else:
            state["user_action"] = "revise"
            state["route"] = "revise_trip"
        return state

    if state["missing_fields"]:
        if looks_like_general_query(message, state):
            state["route"] = "general_query"
        else:
            state["route"] = "collect_info"
        return state

    state["route"] = "plan_trip"
    return state


def collect_info_node(state: AgentState) -> AgentState:
    state["missing_fields"] = missing_fields(state)
    question = next_question(state)
    acknowledgement = destination_acknowledgement(state)
    if acknowledgement and state["missing_fields"] and state["missing_fields"][0] in {"budget", "duration", "travel_style"}:
        question = f"{acknowledgement} {question}"
    state["last_question"] = question
    state["status"] = "collecting"
    emit_client_message(
        state,
        {
            "type": "chat",
            "content": question,
            "metadata": {
                "kind": "question",
                "missing_fields": state["missing_fields"],
                "collected": {key: state.get(key) for key in REQUIRED_FIELDS},
            },
        },
    )
    return state


def wait_for_user_node(state: AgentState) -> AgentState:
    interrupt({"waiting_for": "user_input"})
    return state


def general_query_node(state: AgentState) -> AgentState:
    state["status"] = "answering_general"
    emit_client_message(
        state,
        {
            "type": "status",
            "content": "Let me think through that with local context across Pakistan...",
            "metadata": {"stage": "general_query"},
        },
    )
    llm = get_llm()
    prompt = f"""
Answer the user's Pakistan travel question with practical local guidance.
Always reply in English.
If helpful, mention realistic transport, weather, road conditions, and seasonal tradeoffs.

Known trip context:
- Destination: {state.get("destination") or "not provided"}
- Budget: {state.get("budget") or "not provided"}
- Duration: {state.get("duration") or "not provided"}
- Travel style: {state.get("travel_style") or "not provided"}
""".strip()
    try:
        response = llm.invoke(
            build_messages(prompt, state.get("chat_history", []), state.get("current_message")),
            temperature=0.2,
        )
        answer = response.content if isinstance(response.content, str) else str(response.content)
        state["general_answer"] = answer
        emit_client_message(
            state,
            {
                "type": "chat",
                "content": answer,
                "metadata": {"kind": "answer"},
            },
        )
    except Exception as exc:
        logger.exception("General query node failed")
        state["status"] = "failed"
        state["error_message"] = f"Unable to answer right now: {exc}"
        emit_client_message(
            state,
            {
                "type": "error",
                "content": state["error_message"],
            },
        )
        return state

    if missing_fields(state):
        follow_up = next_question(state)
        state["last_question"] = follow_up
        state["status"] = "collecting"
        emit_client_message(
            state,
            {
                "type": "chat",
                "content": follow_up,
                "metadata": {
                    "kind": "question",
                    "missing_fields": missing_fields(state),
                },
            },
        )
    else:
        state["status"] = "ready_to_plan"
    return state


def plan_trip_node(state: AgentState) -> AgentState:
    state["status"] = "planning"
    emit_client_message(
        state,
        {
            "type": "status",
            "content": "Building a locally realistic Pakistan itinerary...",
            "metadata": {"stage": "planning"},
        },
    )
    llm = get_llm()
    destination = state.get("destination", "Pakistan")
    budget = state.get("budget", "not provided")
    budget_currency = state.get("budget_currency", "")
    duration = state.get("duration", "not provided")
    travel_style = state.get("travel_style", "general travel")
    planning_notes = state.get("planning_notes", "")
    refined_prompt = state.get("refined_prompt", "")

    planner_prompt = refined_prompt or f"""
Create a detailed, realistic {duration}-day itinerary for {destination} in Pakistan.
Trip budget: {budget} {budget_currency}
Trip style: {travel_style}
Additional user feedback: {planning_notes or "None"}

Requirements:
- Give a day-by-day itinerary.
- Keep costs realistic and aligned with the stated budget.
- Mention transport logistics, road or weather realities, and strong local food or cultural experiences.
- Mention local tips that a knowledgeable Pakistan travel specialist would know.
- Always write in English.
- Do not invent extra days or budget.
""".strip()

    try:
        response = llm.invoke(
            build_messages(LOCAL_EXPERTISE_PROMPT, state.get("chat_history", []), planner_prompt),
            temperature=0.35,
        )
        draft = response.content if isinstance(response.content, str) else str(response.content)
        state["ai_draft"] = draft
        state["status"] = "draft_ready"
        emit_client_message(
            state,
            {
                "type": "itinerary",
                "stage": "draft",
                "content": draft,
                "metadata": {
                    "destination": destination,
                    "budget": budget,
                    "budget_currency": budget_currency,
                    "duration": duration,
                },
            },
        )
    except Exception as exc:
        logger.exception("Planner node failed")
        state["status"] = "failed"
        state["error_message"] = f"Unable to generate itinerary right now: {exc}"
        emit_client_message(
            state,
            {
                "type": "error",
                "content": state["error_message"],
            },
        )
    return state


def review_trip_node(state: AgentState) -> AgentState:
    if state.get("status") != "draft_ready":
        return state
    emit_client_message(
        state,
        {
            "type": "chat",
            "content": "If the itinerary looks right, reply with 'approve'. Otherwise, tell me what you would like changed.",
            "metadata": {"kind": "review_prompt"},
        },
    )
    return state


def revise_trip_node(state: AgentState) -> AgentState:
    feedback = state.get("current_message", "")
    state["planning_notes"] = feedback
    state["refined_prompt"] = f"""
Revise the existing Pakistan itinerary using this user feedback:
{feedback}

Keep these confirmed trip parameters fixed unless the user explicitly changed them:
- Destination: {state.get("destination")}
- Budget: {state.get("budget")}
- Budget currency: {state.get("budget_currency")}
- Duration: {state.get("duration")}
- Travel style: {state.get("travel_style")}
""".strip()
    state["status"] = "ready_to_plan"
    emit_client_message(
        state,
        {
            "type": "status",
            "content": "Reworking the itinerary with your feedback...",
            "metadata": {"stage": "revision"},
        },
    )
    return state


def finalize_trip_node(state: AgentState) -> AgentState:
    final_itinerary = state.get("ai_draft") or state.get("final_itinerary") or ""
    state["final_itinerary"] = final_itinerary
    state["status"] = "approved"
    emit_client_message(
        state,
        {
            "type": "itinerary",
            "stage": "final",
            "content": final_itinerary,
            "metadata": {
                "destination": state.get("destination"),
                "budget": state.get("budget"),
                "budget_currency": state.get("budget_currency"),
                "duration": state.get("duration"),
            },
        },
    )
    return state


def after_router(state: AgentState) -> Literal[
    "collect_info",
    "general_query",
    "plan_trip",
    "review_trip",
    "revise_trip",
    "finalize_trip",
]:
    return state.get("route", "collect_info")


builder = StateGraph(AgentState)

builder.add_node("router", router_node)
builder.add_node("collect_info", collect_info_node)
builder.add_node("wait_for_user", wait_for_user_node)
builder.add_node("general_query", general_query_node)
builder.add_node("plan_trip", plan_trip_node)
builder.add_node("review_trip", review_trip_node)
builder.add_node("revise_trip", revise_trip_node)
builder.add_node("finalize_trip", finalize_trip_node)

builder.set_entry_point("router")

builder.add_conditional_edges(
    "router",
    after_router,
    {
        "collect_info": "collect_info",
        "general_query": "general_query",
        "plan_trip": "plan_trip",
        "review_trip": "review_trip",
        "revise_trip": "revise_trip",
        "finalize_trip": "finalize_trip",
    },
)
builder.add_edge("collect_info", "wait_for_user")
builder.add_edge("general_query", "wait_for_user")
builder.add_edge("plan_trip", "review_trip")
builder.add_edge("review_trip", "wait_for_user")
builder.add_edge("revise_trip", "plan_trip")
builder.add_edge("finalize_trip", END)
builder.add_edge("wait_for_user", "router")

graph = builder.compile(checkpointer=checkpointer)
