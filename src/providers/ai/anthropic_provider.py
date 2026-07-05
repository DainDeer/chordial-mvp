import asyncio
import logging
from typing import Optional

import anthropic
from anthropic import AsyncAnthropic

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


class AnthropicProvider(BaseAIProvider):
    """anthropic (claude) provider.

    makes one Messages API call per create_message. adaptive thinking is on;
    `temperature`/`top_p` are intentionally never sent (rejected by current
    claude models). effort is the per-call cost dial.
    """

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
        self.model = model or Config.CHAT_MODEL
        self.client = AsyncAnthropic(api_key=api_key or Config.ANTHROPIC_API_KEY)
        # shared ceiling on concurrent in-flight calls; invisible at one user,
        # a guardrail against burst fan-out from the scheduler at scale.
        self._semaphore = asyncio.Semaphore(Config.MAX_CONCURRENT_AI_CALLS)

    async def create_message(self, request: AIRequest) -> AIResponse:
        kwargs = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "system": self._render_system(request),
            "messages": self._render_messages(request.messages),
            "thinking": {"type": "adaptive"},
        }
        if request.tools:
            kwargs["tools"] = self._render_tools(request.tools)
        if request.effort:
            kwargs["output_config"] = {"effort": request.effort}

        try:
            async with self._semaphore:
                response = await self.client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise ProviderRateLimited(str(e)) from e
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise ProviderUnavailable(str(e)) from e
        except anthropic.APIStatusError as e:
            raise ProviderError(str(e), retryable=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(str(e), retryable=True) from e

        return self._normalize(response)

    # --- rendering ---------------------------------------------------------

    def _render_system(self, request: AIRequest) -> list[dict]:
        blocks = []
        for block in request.system:
            rendered = {"type": "text", "text": block.text}
            if block.cache:
                rendered["cache_control"] = {"type": "ephemeral"}
            blocks.append(rendered)
        return blocks

    def _render_tools(self, tools) -> list[dict]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]

    def _render_messages(self, messages: list[ChatTurn]) -> list[dict]:
        rendered = []
        for turn in messages:
            # assistant continuation: echo the raw blocks back unchanged so
            # thinking/tool_use blocks survive round-trips on the same model.
            if turn.provider_blocks is not None:
                rendered.append({"role": turn.role, "content": turn.provider_blocks})
                continue

            if turn.tool_results:
                content = [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.tool_call_id,
                        "content": r.content,
                        "is_error": r.is_error,
                    }
                    for r in turn.tool_results
                ]
                rendered.append({"role": "user", "content": content})
                continue

            # plain text turn; wrap in a block if it's a cache breakpoint
            if turn.cache:
                rendered.append({
                    "role": turn.role,
                    "content": [{
                        "type": "text",
                        "text": turn.content or "",
                        "cache_control": {"type": "ephemeral"},
                    }],
                })
            else:
                rendered.append({"role": turn.role, "content": turn.content or ""})
        return rendered

    # --- normalization -----------------------------------------------------

    def _normalize(self, response) -> AIResponse:
        text_parts = []
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=block.input))

        text = "".join(text_parts).strip() or None
        if response.stop_reason == "refusal":
            text = None

        usage = Usage(
            input_tokens=getattr(response.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(response.usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        )

        assistant_turn = ChatTurn(
            role="assistant",
            content=text,
            tool_calls=tool_calls or None,
            provider_blocks=response.content,  # raw blocks for lossless continuation
        )

        return AIResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason or "end_turn",
            usage=usage,
            assistant_turn=assistant_turn,
            model=self.model,
        )

    async def is_available(self) -> bool:
        if not (Config.ANTHROPIC_API_KEY):
            logger.warning("anthropic provider: no ANTHROPIC_API_KEY set")
            return False
        try:
            await self.client.models.retrieve(self.model)
            return True
        except Exception as e:
            logger.warning("anthropic provider configured but unavailable: %s", e)
            return False
