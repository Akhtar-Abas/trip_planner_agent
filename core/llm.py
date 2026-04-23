from __future__ import annotations
from langchain_groq import ChatGroq
from langchain_google_genai import ChatGoogleGenerativeAI
import logging
from dataclasses import dataclass
from typing import Iterable, Sequence

from decouple import config
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

try:
    from langchain_groq import ChatGroq
except ImportError:  # pragma: no cover
    ChatGroq = None

try:
    from langchain_google_genai import ChatGoogleGenerativeAI
except ImportError:  # pragma: no cover
    ChatGoogleGenerativeAI = None

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str


PRIMARY_PROVIDER = ProviderConfig(
    provider="groq",
    model=config("GROQ_MODEL", default="llama-3.3-70b-versatile"),
)
FALLBACK_PROVIDER = ProviderConfig(
    provider="google",
    model=config("GOOGLE_MODEL", default="gemini-1.5-flash"),
)


class DynamicTravelLLM:
    def __init__(self) -> None:
        self._primary = None
        self._fallback = None

    def _build_primary(self, temperature: float):
        if ChatGroq is None:
            raise RuntimeError("langchain-groq is not installed.")
        api_key = config("GROQ_API_KEY", default="")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is not configured.")
        return ChatGroq(
            model=PRIMARY_PROVIDER.model,
            temperature=temperature,
            groq_api_key=api_key,
        )

    def _build_fallback(self, temperature: float):
        if ChatGoogleGenerativeAI is None:
            raise RuntimeError("langchain-google-genai is not installed.")
        api_key = config("GOOGLE_API_KEY", default="")
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY is not configured.")
        return ChatGoogleGenerativeAI(
            model=FALLBACK_PROVIDER.model,
            temperature=temperature,
            google_api_key=api_key,
        )

    def primary(self, temperature: float = 0.3):
        if self._primary is None:
            self._primary = self._build_primary(temperature)
        return self._primary

    def fallback(self, temperature: float = 0.3):
        if self._fallback is None:
            self._fallback = self._build_fallback(temperature)
        return self._fallback

    def invoke(
        self,
        messages: Sequence[BaseMessage],
        *,
        temperature: float = 0.3,
    ) -> AIMessage:
        try:
            logger.info("Invoking primary LLM provider: %s", PRIMARY_PROVIDER.provider)
            return self.primary(temperature=temperature).invoke(list(messages))
        except Exception:
            logger.exception("Primary LLM provider failed, attempting fallback")
            try:
                logger.info("Invoking fallback LLM provider: %s", FALLBACK_PROVIDER.provider)
                return self.fallback(temperature=temperature).invoke(list(messages))
            except Exception:
                logger.exception("Fallback LLM provider failed")
                raise


def build_messages(system_prompt: str, conversation: Iterable[dict], latest_user_message: str | None = None) -> list[BaseMessage]:
    messages: list[BaseMessage] = [SystemMessage(content=system_prompt)]
    for turn in conversation:
        role = turn.get("role")
        content = turn.get("content")
        if not role or not content:
            continue
        if role == "assistant":
            messages.append(AIMessage(content=content))
        elif role == "system":
            messages.append(SystemMessage(content=content))
        else:
            messages.append(HumanMessage(content=content))
    if latest_user_message:
        messages.append(HumanMessage(content=latest_user_message))
    return messages


_shared_llm = DynamicTravelLLM()


def get_llm() -> DynamicTravelLLM:
    return _shared_llm
