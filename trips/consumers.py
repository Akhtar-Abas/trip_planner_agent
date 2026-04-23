from __future__ import annotations

import json
import logging

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from .services import process_user_message, start_conversation

logger = logging.getLogger(__name__)


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.session_key = await self.get_session_key()
        self.room_group_name = f"chat_{self.session_key}"

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

        try:
            conversation = await sync_to_async(start_conversation)(self.session_key)
            self.thread_id = conversation["thread_id"]
            await self.send_current_state(conversation["current_message"])
        except Exception as exc:
            logger.exception("WebSocket connect bootstrap failed")
            await self.send_json(
                {
                    "type": "error",
                    "content": f"Unable to start conversation: {exc}",
                }
            )
            await self.close(code=1011)

    async def disconnect(self, close_code):
        if hasattr(self, "room_group_name"):
            try:
                await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
            except Exception:
                logger.exception("Failed to remove channel from group during disconnect")

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send_json({"type": "error", "content": "Invalid JSON payload."})
            return

        message = (data.get("message") or "").strip()
        client_thread_id = data.get("thread_id") or getattr(self, "thread_id", None)

        if not message or not client_thread_id:
            await self.send_json(
                {
                    "type": "error",
                    "content": "Missing message or conversation id.",
                }
            )
            return

        try:
            result = await sync_to_async(process_user_message)(
                client_thread_id,
                message,
                self.session_key,
            )
        except Exception as exc:
            logger.exception("WebSocket receive processing failed")
            await self.send_json({"type": "error", "content": f"Processing failed: {exc}"})
            return

        if result.get("thread_id"):
            self.thread_id = result["thread_id"]

        if "error" in result:
            await self.send_json({"type": "error", "content": result["error"], "thread_id": self.thread_id})

    async def chat_update(self, event):
        try:
            await self.send_json(event["data"])
        except Exception:
            logger.exception("Failed to send websocket chat update")

    async def send_current_state(self, payload: dict):
        message = dict(payload)
        message.setdefault("thread_id", getattr(self, "thread_id", None))
        await self.send_json(message)

    async def send_json(self, payload: dict):
        normalized = self.normalize_payload(payload)
        await self.send(text_data=json.dumps(normalized))

    def normalize_payload(self, payload: dict) -> dict:
        normalized = dict(payload)
        message_type = normalized.get("type")
        normalized.setdefault("category", message_type)

        if message_type == "chat":
            normalized["type"] = "question"
        elif message_type == "itinerary":
            normalized["type"] = "final" if normalized.get("stage") == "final" else "draft"

        return normalized

    @database_sync_to_async
    def get_session_key(self):
        session = self.scope["session"]
        if not session.session_key:
            session.create()
        return session.session_key
