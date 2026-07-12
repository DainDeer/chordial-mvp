import asyncio

import pytest

from config import Config
from main import _close_provider, _run_services


def run(coro):
    return asyncio.run(coro)


class FakeRouter:
    def platforms(self):
        return ["telegram"]

    async def deliver(self, *args, **kwargs):
        return True


class FakeScheduler:
    def __init__(self):
        self.stopped = False
        self.cancelled = False

    async def run_scheduling_loop(self, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    def stop(self):
        self.stopped = True


class ReturningInterface:
    platform = "telegram"

    def __init__(self):
        self.stopped = False

    async def start(self):
        return

    async def stop(self):
        self.stopped = True


def test_service_return_stops_scheduler_and_interfaces():
    interface = ReturningInterface()
    scheduler = FakeScheduler()

    with pytest.raises(RuntimeError, match="telegram interface stopped unexpectedly"):
        run(_run_services([interface], scheduler, FakeRouter()))

    assert interface.stopped is True
    assert scheduler.stopped is True
    assert scheduler.cancelled is True


def test_provider_specific_utility_models(monkeypatch):
    monkeypatch.setattr(Config, "ANTHROPIC_UTILITY_MODEL", "claude-utility")
    monkeypatch.setattr(Config, "OPENAI_UTILITY_MODEL", "gpt-utility")

    assert Config.utility_model_for("anthropic") == "claude-utility"
    assert Config.utility_model_for("openai") == "gpt-utility"
    with pytest.raises(ValueError, match="unknown AI provider"):
        Config.utility_model_for("other")


def test_close_provider_closes_async_sdk_client():
    class Client:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    class Provider:
        client = Client()

    provider = Provider()
    run(_close_provider(provider))
    assert provider.client.closed is True
