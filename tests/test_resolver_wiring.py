"""chordial's ProviderTable wiring (§4.8): the chat provider is the table's
default route, thinking is a per-model decision, and every resolved instance
shares one concurrency ceiling."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from dainframe.core import ExecutionHints
from dainframe.providers import HintResolutionError

from config import Config
from main import _build_resolver


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    # the sdk clients want SOME key at construction; none is ever used
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


def test_default_route_is_the_chat_model_with_thinking():
    resolver = _build_resolver("anthropic")
    resolved = resolver.resolve(ExecutionHints(), agent="chordial")
    assert resolved.provider_name == "anthropic"
    assert resolved.model == Config.CHAT_MODEL
    assert resolved.provider._thinking is True


def test_utility_tier_hint_builds_without_thinking():
    """anthropic utility models reject adaptive thinking; the builder knows
    the tier by model, so a director hinting the cheap model just works."""
    resolver = _build_resolver("anthropic")
    resolved = resolver.resolve(
        ExecutionHints(model=Config.ANTHROPIC_UTILITY_MODEL), agent="chordial"
    )
    assert resolved.model == Config.ANTHROPIC_UTILITY_MODEL
    assert resolved.provider._thinking is False


def test_all_resolved_instances_share_one_ceiling():
    resolver = _build_resolver("anthropic")
    chat = resolver.resolve(ExecutionHints(), agent="a").provider
    fast = resolver.resolve(
        ExecutionHints(model=Config.ANTHROPIC_UTILITY_MODEL), agent="b"
    ).provider
    assert chat is not fast
    assert chat.limiter is fast.limiter


def test_openai_route_uses_its_own_default_model():
    resolver = _build_resolver("anthropic")
    resolved = resolver.resolve(ExecutionHints(provider="openai"), agent="a")
    assert resolved.model == Config.OPENAI_MODEL


def test_effort_hint_toward_openai_routes():
    # the dainframe's openai adapter maps effort onto reasoning.effort now,
    # so an effort hint routed there resolves instead of raising.
    resolver = _build_resolver("anthropic")
    resolved = resolver.resolve(
        ExecutionHints(provider="openai", effort="medium"), agent="a"
    )
    assert (resolved.provider_name, resolved.effort) == ("openai", "medium")


def test_unknown_effort_level_still_fails_loudly():
    resolver = _build_resolver("anthropic")
    with pytest.raises(HintResolutionError):
        resolver.resolve(
            ExecutionHints(provider="openai", effort="maximal"), agent="a"
        )
