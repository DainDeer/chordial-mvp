import asyncio
import json
import logging
from typing import Optional

from openai import AsyncOpenAI, RateLimitError, AuthenticationError, APIError

from .base import BaseAIProvider
from .types import (
    AIRequest,
    AIResponse,
    ChatTurn,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
    ToolCall,
    Usage,
)
from config import Config

logger = logging.getLogger(__name__)


class OpenAIProvider(BaseAIProvider):
    """openai provider (Responses API), adapted to the neutral interface.

    the anthropic provider is the primary/default path; this exists so the
    backend can be swapped via AI_PROVIDER and for A/B on personality. cache
    hints and `effort` are anthropic concepts and are ignored here.
    """

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
        self.model = model or Config.OPENAI_MODEL
        self.client = AsyncOpenAI(api_key=api_key or Config.OPENAI_API_KEY)
        self._semaphore = asyncio.Semaphore(Config.MAX_CONCURRENT_AI_CALLS)

    async def create_message(self, request: AIRequest) -> AIResponse:
        kwargs = {
            "model": self.model,
            "instructions": "\n\n".join(b.text for b in request.system),
            "input": self._render_input(request.messages),
            "max_output_tokens": request.max_tokens,
        }
        if request.tools:
            kwargs["tools"] = self._render_tools(request.tools)

        try:
            async with self._semaphore:
                response = await self.client.responses.create(**kwargs)
        except RateLimitError as e:
            # 429 is most commonly out of quota/funds, not true rate limiting
            raise ProviderRateLimited(str(e)) from e
        except AuthenticationError as e:
            raise ProviderUnavailable(str(e)) from e
        except APIError as e:
            raise ProviderError(str(e), retryable=True) from e

        return self._normalize(response)

    # --- rendering ---------------------------------------------------------

    def _render_tools(self, tools) -> list[dict]:
        return [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            }
            for t in tools
        ]

    def _render_input(self, messages: list[ChatTurn]) -> list[dict]:
        items: list[dict] = []
        for turn in messages:
            # assistant continuation: re-send the function_call items verbatim
            if turn.provider_blocks is not None:
                items.extend(turn.provider_blocks)
                continue

            if turn.tool_results:
                for r in turn.tool_results:
                    items.append({
                        "type": "function_call_output",
                        "call_id": r.tool_call_id,
                        "output": r.content,
                    })
                continue

            items.append({"role": turn.role, "content": turn.content or ""})
        return items

    # --- normalization -----------------------------------------------------

    def _normalize(self, response) -> AIResponse:
        text_parts = []
        tool_calls = []
        echo_blocks = []  # function_call items to re-send on continuation

        for item in getattr(response, "output", []) or []:
            item_type = getattr(item, "type", None)
            if item_type == "message":
                for part in getattr(item, "content", []) or []:
                    if getattr(part, "type", None) in ("output_text", "text"):
                        text_parts.append(getattr(part, "text", ""))
            elif item_type == "function_call":
                call_id = getattr(item, "call_id", None) or getattr(item, "id", "")
                name = getattr(item, "name", "")
                raw_args = getattr(item, "arguments", "") or "{}"
                try:
                    parsed_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    parsed_args = {}
                tool_calls.append(ToolCall(id=call_id, name=name, input=parsed_args))
                echo_blocks.append({
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": raw_args,
                })

        text = "".join(text_parts).strip() or None

        u = getattr(response, "usage", None)
        cached = 0
        if u is not None:
            details = getattr(u, "input_tokens_details", None)
            cached = getattr(details, "cached_tokens", 0) or 0 if details else 0
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0 if u else 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0 if u else 0,
            cache_read_tokens=cached,
            cache_write_tokens=0,
        )

        stop_reason = "tool_use" if tool_calls else "end_turn"
        assistant_turn = ChatTurn(
            role="assistant",
            content=text,
            tool_calls=tool_calls or None,
            provider_blocks=echo_blocks or None,
        )

        return AIResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            assistant_turn=assistant_turn,
            model=self.model,
        )

    async def is_available(self) -> bool:
        if not Config.OPENAI_API_KEY:
            return False
        try:
            await self.client.models.retrieve(self.model)
            return True
        except Exception as e:
            logger.warning("openai provider configured but unavailable: %s", e)
            return False
