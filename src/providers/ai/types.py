"""provider-neutral request/response types for ai backends.

these are shaped close to anthropic's structured wire format (system, messages,
tools as distinct concepts). the openai adapter flattens them into its own
format. keeping one neutral vocabulary here means the agent loop, prompt
builder, and everything above the provider never has to know which backend is
active.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# --- tool primitives -------------------------------------------------------

@dataclass
class ToolDef:
    """a tool the model may call. input_schema is JSON Schema (both providers
    accept it natively)."""
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    """a tool invocation the model produced."""
    id: str            # provider-assigned id; used to correlate the result
    name: str
    input: dict


@dataclass
class ToolResult:
    """the outcome of running a ToolCall, fed back to the model."""
    tool_call_id: str
    content: str
    is_error: bool = False


# --- conversation primitives ----------------------------------------------

@dataclass
class ChatTurn:
    """one turn in the messages array.

    - a plain user/assistant turn sets `content`.
    - an assistant turn that called tools carries `tool_calls` (and, for
      round-tripping thinking/tool blocks losslessly on the same model,
      `provider_blocks` — the raw provider-native content to echo back).
    - the user turn that answers tool calls carries `tool_results`.
    - `cache` marks this turn as a prompt-cache breakpoint (see prompt layout).
    """
    role: str                                       # "user" | "assistant"
    content: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None
    tool_results: Optional[list[ToolResult]] = None
    provider_blocks: Any = None                     # opaque, provider-native
    cache: bool = False


@dataclass
class SystemBlock:
    """one block of the system prompt. `cache=True` places a cache breakpoint
    at the end of this block (caches tools + all system content up to here)."""
    text: str
    cache: bool = False


# --- request / response ----------------------------------------------------

@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass
class AIRequest:
    system: list[SystemBlock]
    messages: list[ChatTurn]
    tools: list[ToolDef] = field(default_factory=list)
    max_tokens: int = 2048
    effort: Optional[str] = None   # "low" | "medium" | "high" (anthropic)


@dataclass
class AIResponse:
    text: Optional[str]                    # user-visible text, if any
    tool_calls: list[ToolCall]             # empty when the model is done
    stop_reason: str                       # "end_turn"|"tool_use"|"max_tokens"|"refusal"
    usage: Usage
    assistant_turn: ChatTurn               # append verbatim to continue the turn
    model: str = ""


# --- errors ----------------------------------------------------------------

class ProviderError(Exception):
    """base for provider failures. raised (not returned as text) so callers
    decide what the user sees and never persist an error as an assistant turn.
    `retryable` hints whether a later attempt might succeed."""
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class ProviderRateLimited(ProviderError):
    def __init__(self, message: str = "rate limited"):
        super().__init__(message, retryable=True)


class ProviderUnavailable(ProviderError):
    """auth failure, misconfiguration, or the service being down."""
    def __init__(self, message: str = "provider unavailable"):
        super().__init__(message, retryable=False)
