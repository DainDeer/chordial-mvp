"""AnthropicProvider request-kwargs tests.

locks down the capability gating that broke the curator: adaptive thinking and
the effort param must be omitted for the utility tier (haiku 4.5 rejects both
with a 400), and present for the chat tier.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.providers.ai.anthropic_provider import AnthropicProvider  # noqa: E402
from src.providers.ai.types import AIRequest, ChatTurn, SystemBlock, ToolDef  # noqa: E402


def _request(effort=None, tools=None):
    return AIRequest(
        system=[SystemBlock(text="persona")],
        messages=[ChatTurn(role="user", content="hi")],
        tools=tools or [],
        max_tokens=512,
        effort=effort,
    )


def test_chat_tier_sends_adaptive_thinking():
    provider = AnthropicProvider(model="claude-sonnet-5", api_key="x", thinking=True)
    kwargs = provider._build_kwargs(_request(effort="low"))
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "low"}


def test_utility_tier_omits_thinking():
    """the curator's config: haiku, no thinking, no effort -> neither key sent."""
    provider = AnthropicProvider(model="claude-haiku-4-5", api_key="x", thinking=False)
    kwargs = provider._build_kwargs(_request(effort=None))
    assert "thinking" not in kwargs
    assert "output_config" not in kwargs
    # the essentials are still there
    assert kwargs["model"] == "claude-haiku-4-5"
    assert kwargs["max_tokens"] == 512
    assert kwargs["messages"]


def test_effort_only_sent_when_requested():
    provider = AnthropicProvider(model="claude-sonnet-5", api_key="x", thinking=True)
    assert "output_config" not in provider._build_kwargs(_request(effort=None))


def test_tools_included_when_present():
    provider = AnthropicProvider(model="claude-sonnet-5", api_key="x", thinking=True)
    tools = [ToolDef(name="t", description="d", input_schema={"type": "object"})]
    kwargs = provider._build_kwargs(_request(tools=tools))
    assert kwargs["tools"][0]["name"] == "t"
