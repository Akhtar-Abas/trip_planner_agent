from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Optional

from django.core.cache import cache

from langgraph.types import Command

from core.workflow import REQUIRED_FIELDS, graph, next_question
from trips.models import TripPlan

logger = logging.getLogger(__name__)

ACTIVE_TRIP_STATUSES = (TripPlan.Status.COLLECTING, TripPlan.Status.DRAFT)
SESSION_LOCK_TTL_SECONDS = 10
SESSION_LOCK_WAIT_SECONDS = 5
SESSION_LOCK_POLL_SECONDS = 0.1


def extract_days(text: str, *, allow_plain_number: bool = False) -> Optional[int]:
    text = text.lower().strip()
    if allow_plain_number and re.fullmatch(r"\d+", text):
        return int(text)

    word_to_num = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "week": 7,
    }
    if "a month" in text or "one month" in text:
        return 30
    if "a week" in text or "one week" in text:
        return 7

    for pattern in (r"(\d+)\s*days?", r"(\d+)\s*din", r"day\s*(\d+)", r"(\d+)\s*day\b"):
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))

    for word, value in word_to_num.items():
        if re.search(rf"\b{word}\s*(days?|din)\b", text):
            return value

    return None


def extract_budget(text: str) -> Optional[float]:
    text = text.lower().strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)

    patterns = (
        r"\$\s*(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*\$",
        r"(\d+(?:\.\d+)?)\s*(?:dollars?|budget|bucks|usd)",
        r"budget\s*(?:of\s*)?(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*(?:pkr|rs|rupees?)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        amount = float(match.group(1))
        if "pkr" in text or "rs" in text or "rupee" in text:
            return amount * 0.0036
        return amount

    return None


def extract_currency_code(text: str) -> Optional[str]:
    text = text.lower()
    if "usd" in text or "$" in text or "dollar" in text:
        return "USD"
    if "pkr" in text or "rs" in text or "rupee" in text:
        return "PKR"
    return None


def extract_destination(text: str) -> Optional[str]:
    text_lower = text.lower().strip()
    known_places = [
        "skardu",
        "hunza",
        "gilgit",
        "khaplu",
        "nagar",
        "astore",
        "deosai",
        "attabad",
        "fairy meadows",
        "naltar",
        "shigar",
        "karimabad",
        "hopper",
        "rama",
        "minimarg",
        "naran",
        "kaghan",
        "murree",
        "gwadar",
        "lahore",
        "karachi",
        "islamabad",
        "swat",
        "kumrat",
        "quetta",
        "multan",
        "peshawar",
        "muzaffarabad",
        "neelum",
    ]
    for place in known_places:
        if re.search(rf"\b{re.escape(place)}\b", text_lower):
            return place.title()

    patterns = [
        r"(?:visit|go|travel|plan|trip)\s+(?:to\s+)?([a-zA-Z\s]+?)(?:\s+for|\s+with|\s+on|\s+under|\s+budget|\s*$)",
        r"\bto\s+([a-zA-Z\s]+?)(?:\s+for|\s+with|\s+on|\s+under|\s+budget|\s*$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        destination = match.group(1).strip()
        if len(destination) > 2 and not re.search(r"\d", destination):
            return destination.title()

    return None


def extract_travel_style(text: str) -> Optional[str]:
    text = text.strip()
    if not text:
        return None
    style_markers = (
        "trek",
        "hiking",
        "food",
        "culture",
        "photography",
        "nature",
        "family",
        "adventure",
        "relax",
        "honeymoon",
        "mountain",
        "luxury",
        "road trip",
        "backpacking",
        "sightseeing",
    )
    if any(marker in text.lower() for marker in style_markers):
        return text
    return None


def extract_all_fields(message: str) -> Dict[str, Any]:
    updates: Dict[str, Any] = {}

    destination = extract_destination(message)
    if destination:
        updates["destination"] = destination

    budget = extract_budget(message)
    if budget is not None:
        updates["budget"] = budget

    duration = extract_days(message, allow_plain_number=False)
    if duration is not None:
        updates["duration"] = duration

    travel_style = extract_travel_style(message)
    if travel_style:
        updates["travel_style"] = travel_style

    return updates


def build_graph_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def get_graph_snapshot(thread_id: str):
    return graph.get_state(build_graph_config(thread_id))


@contextmanager
def session_lock(session_key: str):
    lock_key = f"trip-start-lock:{session_key}"
    token = str(uuid.uuid4())
    deadline = time.monotonic() + SESSION_LOCK_WAIT_SECONDS

    while time.monotonic() < deadline:
        if cache.add(lock_key, token, timeout=SESSION_LOCK_TTL_SECONDS):
            try:
                yield
            finally:
                if cache.get(lock_key) == token:
                    cache.delete(lock_key)
            return
        time.sleep(SESSION_LOCK_POLL_SECONDS)

    raise TimeoutError(f"Could not acquire startup lock for session {session_key}")


def active_trip_queryset(session_key: str):
    return TripPlan.objects.filter(
        session_key=session_key,
        status__in=ACTIVE_TRIP_STATUSES,
    ).order_by("-updated_at", "-id")


def collapse_duplicate_active_trips(session_key: str) -> Optional[TripPlan]:
    active_trips = list(active_trip_queryset(session_key))
    if not active_trips:
        return None
    primary = active_trips[0]
    duplicates = active_trips[1:]
    if duplicates:
        TripPlan.objects.filter(id__in=[trip.id for trip in duplicates]).update(status=TripPlan.Status.FAILED)
        logger.warning("Collapsed %s duplicate active trips for session %s", len(duplicates), session_key)
    return primary


def trip_status_from_graph(state_status: Optional[str]) -> str:
    if state_status == "draft_ready":
        return TripPlan.Status.DRAFT
    if state_status == "approved":
        return TripPlan.Status.APPROVED
    if state_status == "failed":
        return TripPlan.Status.FAILED
    return TripPlan.Status.COLLECTING


def serialize_current_state(thread_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
    values = values or {}

    status = values.get("status") or "collecting"
    if status == "approved":
        return {
            "type": "itinerary",
            "stage": "final",
            "thread_id": thread_id,
            "content": values.get("final_itinerary") or values.get("ai_draft") or "",
        }
    if status == "draft_ready":
        return {
            "type": "itinerary",
            "stage": "draft",
            "thread_id": thread_id,
            "content": values.get("ai_draft", ""),
            "metadata": {"prompt": "Reply with 'approve' or tell me what to change."},
        }
    if status == "failed":
        return {
            "type": "error",
            "thread_id": thread_id,
            "content": values.get("error_message") or "Something went wrong.",
        }

    last_emitted = values.get("last_emitted_message")
    if last_emitted:
        payload = dict(last_emitted)
        payload.setdefault("thread_id", thread_id)
        return payload

    return {
        "type": "chat",
        "thread_id": thread_id,
        "content": values.get("last_question") or next_question(values),
        "metadata": {
            "kind": "question",
            "collected": {field: values.get(field) for field in REQUIRED_FIELDS},
            "budget_currency": values.get("budget_currency"),
        },
    }


def build_response(thread_id: str, payload: Dict[str, Any], *, created: bool) -> Dict[str, Any]:
    return {
        "thread_id": thread_id,
        "created": created,
        "current_message": payload,
    }


def persist_trip_from_state(trip: TripPlan, values: Dict[str, Any]) -> None:
    if not values:
        return

    changed_fields = []
    field_mapping = {
        "destination": "destination",
        "budget": "budget",
        "duration": "days",
        "travel_style": "travel_type",
        "ai_draft": "ai_draft",
        "final_itinerary": "final_itinerary",
    }
    for state_field, model_field in field_mapping.items():
        state_value = values.get(state_field)
        model_value = getattr(trip, model_field)
        if state_value is not None and model_value != state_value:
            setattr(trip, model_field, state_value)
            changed_fields.append(model_field)

    new_status = trip_status_from_graph(values.get("status"))
    if trip.status != new_status:
        trip.status = new_status
        changed_fields.append("status")

    if changed_fields:
        trip.save(update_fields=changed_fields)


def create_initial_graph_state(session_key: str, thread_id: str) -> Dict[str, Any]:
    return {
        "thread_id": thread_id,
        "session_key": session_key,
        "destination": None,
        "budget": None,
        "budget_currency": None,
        "pending_budget_amount": None,
        "duration": None,
        "travel_style": None,
        "chat_history": [],
        "status": "collecting",
        "current_message": "",
        "extracted_fields": [],
        "suppress_ws_updates": True,
    }


def update_graph_state(config: dict, values: Dict[str, Any], *, as_node: Optional[str] = None) -> None:
    try:
        if as_node is None:
            graph.update_state(config, values)
        else:
            graph.update_state(config, values, as_node=as_node)
    except TypeError:
        graph.update_state(config, values)


def state_from_trip(trip: TripPlan) -> Dict[str, Any]:
    return {
        "destination": trip.destination or None,
        "budget": float(trip.budget) if trip.budget is not None else None,
        "duration": trip.days,
        "travel_style": trip.travel_type or None,
        "ai_draft": trip.ai_draft,
        "final_itinerary": trip.final_itinerary,
        "status": "draft_ready" if trip.status == TripPlan.Status.DRAFT else "collecting",
        "chat_history": [],
    }


def start_conversation(session_key: str) -> dict:
    try:
        with session_lock(session_key):
            trip = collapse_duplicate_active_trips(session_key)
            if trip and trip.thread_id:
                snapshot = get_graph_snapshot(trip.thread_id)
                if snapshot and snapshot.values and snapshot.values.get("suppress_ws_updates"):
                    update_graph_state(build_graph_config(trip.thread_id), {"suppress_ws_updates": False})
                    snapshot = get_graph_snapshot(trip.thread_id)
                values = snapshot.values if snapshot and snapshot.values else state_from_trip(trip)
                return build_response(trip.thread_id, serialize_current_state(trip.thread_id, values), created=False)

            thread_id = str(uuid.uuid4())
            config = build_graph_config(thread_id)
            initial_state = create_initial_graph_state(session_key, thread_id)
            for _ in graph.stream(initial_state, config, stream_mode="updates"):
                pass

            update_graph_state(config, {"suppress_ws_updates": False})
            snapshot = get_graph_snapshot(thread_id)
            values = snapshot.values if snapshot and snapshot.values else initial_state

            TripPlan.objects.create(
                session_key=session_key,
                thread_id=thread_id,
                destination=values.get("destination") or "",
                budget=values.get("budget"),
                days=values.get("duration"),
                travel_type=values.get("travel_style") or "",
                ai_draft=values.get("ai_draft") or "",
                final_itinerary=values.get("final_itinerary") or "",
                status=trip_status_from_graph(values.get("status")),
            )
            return build_response(thread_id, serialize_current_state(thread_id, values), created=True)
    except TimeoutError:
        logger.exception("Conversation startup lock timed out for session %s", session_key)
        trip = collapse_duplicate_active_trips(session_key)
        if not trip or not trip.thread_id:
            raise
        return build_response(
            trip.thread_id,
            serialize_current_state(trip.thread_id, state_from_trip(trip)),
            created=False,
        )


def build_user_chat_history(values: Dict[str, Any], message: str) -> list[dict]:
    history = list(values.get("chat_history", []))
    history.append({"role": "user", "content": message, "type": "chat", "metadata": {}})
    return history


def should_request_budget_currency(values: Dict[str, Any], extracted: Dict[str, Any], message: str) -> bool:
    if "budget" not in extracted:
        return False
    if extract_currency_code(message):
        return False
    return values.get("budget_currency") is None


def process_user_message(thread_id: str, message: str, session_key: str) -> dict:
    trip = TripPlan.objects.filter(session_key=session_key, thread_id=thread_id).first()
    if not trip:
        trip = collapse_duplicate_active_trips(session_key)
        if not trip:
            return {"error": "Conversation not found."}
        thread_id = trip.thread_id

    config = build_graph_config(thread_id)
    snapshot = get_graph_snapshot(thread_id)
    values = snapshot.values if snapshot and snapshot.values else state_from_trip(trip)

    extracted = extract_all_fields(message)
    current_currency = values.get("budget_currency")
    detected_currency = extract_currency_code(message)
    if values.get("pending_budget_amount") and detected_currency and "budget" not in extracted:
        extracted["budget"] = values["pending_budget_amount"]

    update_state: Dict[str, Any] = {
        "current_message": message,
        "chat_history": build_user_chat_history(values, message),
        "extracted_fields": list(extracted.keys()),
        **extracted,
    }
    if detected_currency:
        update_state["budget_currency"] = detected_currency

    if should_request_budget_currency(values, extracted, message):
        update_state.pop("budget", None)
        update_state["pending_budget_amount"] = extracted["budget"]
    elif "budget" in extracted:
        update_state["pending_budget_amount"] = None
        if not detected_currency and current_currency:
            update_state["budget_currency"] = current_currency

    missing = values.get("missing_fields") or [field for field in REQUIRED_FIELDS if not values.get(field)]
    if missing:
        current_field = missing[0]
        if current_field == "duration" and "duration" not in update_state:
            parsed_duration = extract_days(message, allow_plain_number=True)
            if parsed_duration is not None:
                update_state["duration"] = parsed_duration
                update_state["extracted_fields"] = list(set(update_state["extracted_fields"] + ["duration"]))
        elif current_field == "travel_style" and "travel_style" not in update_state:
            update_state["travel_style"] = message.strip()
            update_state["extracted_fields"] = list(set(update_state["extracted_fields"] + ["travel_style"]))
        elif current_field == "budget" and "budget" not in update_state:
            parsed_budget = extract_budget(message)
            if parsed_budget is not None:
                currency = extract_currency_code(message)
                if currency or current_currency:
                    update_state["budget"] = parsed_budget
                    update_state["budget_currency"] = currency or current_currency
                    update_state["pending_budget_amount"] = None
                    update_state["extracted_fields"] = list(set(update_state["extracted_fields"] + ["budget"]))
                else:
                    update_state["pending_budget_amount"] = parsed_budget
        elif current_field == "destination" and "destination" not in update_state:
            parsed_destination = extract_destination(message)
            if parsed_destination:
                update_state["destination"] = parsed_destination
                update_state["extracted_fields"] = list(set(update_state["extracted_fields"] + ["destination"]))

    for state_field, model_field in (("destination", "destination"), ("budget", "budget"), ("duration", "days"), ("travel_style", "travel_type")):
        if state_field in update_state:
            setattr(trip, model_field, update_state[state_field])
    trip.save(update_fields=["destination", "budget", "days", "travel_type"])

    try:
        for _ in graph.stream(
            Command(resume={"ok": True}, update=update_state),
            config,
            stream_mode="updates",
        ):
            pass
    except Exception as exc:
        logger.exception("Failed to process user message")
        return {"thread_id": thread_id, "error": f"Unable to process your request: {exc}"}

    snapshot = get_graph_snapshot(thread_id)
    new_values = snapshot.values if snapshot and snapshot.values else values
    persist_trip_from_state(trip, new_values)

    if new_values.get("status") == "approved":
        trip.final_itinerary = new_values.get("final_itinerary") or trip.ai_draft
        trip.status = TripPlan.Status.APPROVED
        trip.save(update_fields=["final_itinerary", "status"])

    return build_response(thread_id, serialize_current_state(thread_id, new_values), created=False)
