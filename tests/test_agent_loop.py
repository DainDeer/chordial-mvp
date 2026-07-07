"""agent-loop tests, focused on the terminal-tool behavior.

the bug these lock down: when the model writes a reply AND calls save_memory in
the same turn, the reply must survive - not get discarded and replaced by a thin
second-call closer. uses a scripted fake provider (no network) and follows the
repo's plain-asyncio test style.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.agent_service import AgentService  # noqa: E402
from src.services.tools.base import Tool, ToolRegistry  # noqa: E402
from src.providers.ai.types import (  # noqa: E402
    AIRequest, AIResponse, ChatTurn, SystemBlock, ToolCall, ToolDef, Usage,
)


def run(coro):
    return asyncio.run(coro)


class ScriptedProvider:
    """returns a pre-scripted AIResponse per call, recording how many calls it got."""
    model = "fake-model"

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def create_message(self, request: AIRequest) -> AIResponse:
        self.calls += 1
        return self._responses.pop(0)


def _resp(text, tool_calls=None, stop_reason="end_turn"):
    return AIResponse(
        text=text,
        tool_calls=tool_calls or [],
        stop_reason=stop_reason,
        usage=Usage(input_tokens=1, output_tokens=1),
        assistant_turn=ChatTurn(role="assistant", content=text),
    )


def _registry(*, save_terminal=True):
    reg = ToolRegistry()
    calls = []

    async def _save(tool_input, user_uuid):
        calls.append(("save_memory", tool_input))
        return "saved"

    async def _search(tool_input, user_uuid):
        calls.append(("search_memories", tool_input))
        return "found: user likes tea"

    reg.register(Tool(
        definition=ToolDef(name="save_memory", description="save", input_schema={"type": "object"}),
        handler=_save,
        terminal=save_terminal,
    ))
    reg.register(Tool(
        definition=ToolDef(name="search_memories", description="search", input_schema={"type": "object"}),
        handler=_search,
        terminal=False,
    ))
    return reg, calls


def _request():
    return AIRequest(
        system=[SystemBlock(text="persona")],
        messages=[ChatTurn(role="user", content="hi")],
        tools=[],
    )


def _agent(provider, reg):
    # usage_recorder writes to the db; a no-op double keeps this test db-free
    class _NoUsage:
        def record_call(self, **k): pass
        def record_trace(self, **k): pass
    return AgentService(provider, reg, "fake", usage_recorder=_NoUsage())


def test_reply_alongside_save_memory_is_kept_without_a_second_call():
    reg, calls = _registry()
    # one turn: a full reply PLUS a save_memory call
    provider = ScriptedProvider([
        _resp(
            "that sounds like a rough night, i'm glad the morning felt better 💛",
            tool_calls=[ToolCall(id="t1", name="save_memory", input={"instruction": "slept better"})],
            stop_reason="tool_use",
        ),
    ])
    agent = _agent(provider, reg)

    result = run(agent.run(_request(), user_uuid="u", platform="discord", turn_kind="conversation"))

    # the memory was saved...
    assert calls == [("save_memory", {"instruction": "slept better"})]
    # ...the reply survived...
    assert result.text == "that sounds like a rough night, i'm glad the morning felt better 💛"
    # ...and we did NOT make a second api call to regenerate a reply
    assert provider.calls == 1


def test_silent_save_still_round_trips_to_get_a_reply():
    """if the model saves with NO accompanying text, we must still round-trip so
    the user isn't left with silence."""
    reg, calls = _registry()
    provider = ScriptedProvider([
        _resp(None,
              tool_calls=[ToolCall(id="t1", name="save_memory", input={"instruction": "x"})],
              stop_reason="tool_use"),
        _resp("noted! anything else on your mind?"),
    ])
    agent = _agent(provider, reg)

    result = run(agent.run(_request(), user_uuid="u", platform="discord", turn_kind="conversation"))

    assert result.text == "noted! anything else on your mind?"
    assert provider.calls == 2  # had to round-trip for the reply


def test_non_terminal_tool_round_trips_and_keeps_all_text():
    """search_memories result matters, so we round-trip; preamble text is not lost."""
    reg, _ = _registry()
    provider = ScriptedProvider([
        _resp("let me check what i remember...",
              tool_calls=[ToolCall(id="t1", name="search_memories", input={"keywords": ["tea"]})],
              stop_reason="tool_use"),
        _resp("right - you're a tea person 🍵"),
    ])
    agent = _agent(provider, reg)

    result = run(agent.run(_request(), user_uuid="u", platform="discord", turn_kind="conversation"))

    assert provider.calls == 2
    assert result.text == "let me check what i remember...\n\nright - you're a tea person 🍵"


def test_mixed_terminal_and_non_terminal_round_trips():
    """save_memory (terminal) + search_memories (not) in one turn must NOT
    short-circuit - the search result still needs a response."""
    reg, calls = _registry()
    provider = ScriptedProvider([
        _resp("one sec",
              tool_calls=[
                  ToolCall(id="t1", name="save_memory", input={"instruction": "x"}),
                  ToolCall(id="t2", name="search_memories", input={"keywords": ["x"]}),
              ],
              stop_reason="tool_use"),
        _resp("all set"),
    ])
    agent = _agent(provider, reg)

    result = run(agent.run(_request(), user_uuid="u", platform="discord", turn_kind="conversation"))

    assert provider.calls == 2
    assert ("save_memory", {"instruction": "x"}) in calls
    assert result.text == "one sec\n\nall set"


def test_plain_reply_no_tools_unchanged():
    reg, _ = _registry()
    provider = ScriptedProvider([_resp("just a normal reply")])
    agent = _agent(provider, reg)

    result = run(agent.run(_request(), user_uuid="u", platform="discord", turn_kind="conversation"))

    assert result.text == "just a normal reply"
    assert provider.calls == 1
