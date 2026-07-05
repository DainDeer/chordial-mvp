from abc import ABC, abstractmethod

from .types import AIRequest, AIResponse


class BaseAIProvider(ABC):
    """abstract base class for ai providers.

    a provider makes exactly ONE api call per `create_message` — it does not
    loop over tool calls. the agentic loop lives in AgentService so tool
    execution, iteration limits, and accounting stay provider-agnostic.
    """

    # the model id this provider is configured to use (for usage accounting)
    model: str = ""

    @abstractmethod
    async def create_message(self, request: AIRequest) -> AIResponse:
        """make a single completion request and return a normalized response."""
        raise NotImplementedError

    @abstractmethod
    async def is_available(self) -> bool:
        """check if the provider is configured and reachable."""
        raise NotImplementedError
