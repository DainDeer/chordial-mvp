"""prompt-injection tests for the ambient agenda context.

the load-bearing invariant: the agenda digest rides ONLY in the volatile current
turn (after every cache breakpoint), so passing it must not change any system
block or any prior-history turn's bytes. that's what keeps the cached prefix
intact turn-to-turn.

user_uuid is None throughout so PromptService skips its core-memory db lookup -
these tests need no database.
"""
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.personas import load_personas  # noqa: E402
from src.services.prompt_service import PromptService  # noqa: E402
from src.managers.event_log import Event  # noqa: E402
from src.utils.timezone_utils import utc_now  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _user(content, ts):
    return Event(author_type="user", author="user", kind="message",
                 content=content, created_at=ts)


def _agent(content, ts):
    return Event(author_type="agent", author="chordial", kind="message",
                 content=content, created_at=ts)


def _history():
    base = utc_now() - timedelta(hours=2)
    return [
        _user("hey there", base),
        _agent("hi! how's it going", base + timedelta(minutes=1)),
        _user("what should i do today", base + timedelta(hours=2)),
    ]


def _svc():
    return PromptService(persona=load_personas()["chordial"], enable_prompt_logging=False)


DIGEST = "notion agenda: today (1): \"book dentist\" [To do]"


def test_ambient_appears_only_in_current_turn():
    svc = _svc()
    req = run(svc.build_conversation_request(
        conversation_history=_history(), user_name="dain",
        user_uuid=None, user_timezone="US/Pacific", ambient_context=DIGEST,
    ))
    current = req.messages[-1]
    assert DIGEST in current.content
    assert current.content.endswith("what should i do today")
    # no prior turn carries the digest
    assert all(DIGEST not in m.content for m in req.messages[:-1])


def test_ambient_none_matches_baseline_bytes():
    svc = _svc()
    hist = _history()
    with_none = run(svc.build_conversation_request(
        conversation_history=hist, user_name="dain",
        user_uuid=None, user_timezone="US/Pacific", ambient_context=None,
    ))
    without_arg = run(svc.build_conversation_request(
        conversation_history=hist, user_name="dain",
        user_uuid=None, user_timezone="US/Pacific",
    ))
    assert [m.content for m in with_none.messages] == [m.content for m in without_arg.messages]
    assert [b.text for b in with_none.system] == [b.text for b in without_arg.system]


def test_ambient_leaves_system_and_history_unchanged():
    svc = _svc()
    hist = _history()
    plain = run(svc.build_conversation_request(
        conversation_history=hist, user_name="dain",
        user_uuid=None, user_timezone="US/Pacific",
    ))
    withctx = run(svc.build_conversation_request(
        conversation_history=hist, user_name="dain",
        user_uuid=None, user_timezone="US/Pacific", ambient_context=DIGEST,
    ))
    # system blocks identical
    assert [b.text for b in plain.system] == [b.text for b in withctx.system]
    # every turn except the last (the volatile one) is byte-identical
    assert [m.content for m in plain.messages[:-1]] == [m.content for m in withctx.messages[:-1]]
    # only the last turn differs, and only by the injected block
    assert plain.messages[-1].content != withctx.messages[-1].content


def test_scheduled_request_injects_ambient():
    svc = _svc()
    req = run(svc.build_scheduled_request(
        conversation_history=_history(), user_name="dain",
        user_uuid=None, user_timezone="US/Pacific", ambient_context=DIGEST,
    ))
    current = req.messages[-1]
    assert DIGEST in current.content
    assert "scheduled check-in" in current.content
