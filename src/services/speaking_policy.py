"""the speaking decider: layer two of the three-layer routing spine
(ROOMS_DESIGN.md §3, the velvet antler scheduler conclusion).

    message -> routing rules -> decider -> reasoning -> ONE speaker
               deterministic    (this)     the helper

the deterministic rules (ChordialDirector) resolve everything they can for
free: dms, @-mentions, introductions, single-candidate casts. only the
genuine gray zone reaches this class - a shared-room message, no mention,
more than one plausible lane - and it is answered by one tiny
enum-constrained utility-model call: WHICH ONE helper fields this message.

guarantees live in the caller, not here: this class returns a validated
candidate id or None, and the director maps None to the chair. no path
through this file can break the conversation - a provider error, a
malformed reply, an invented helper id all collapse to None.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from dainframe.providers.types import AIRequest, ChatTurn, SystemBlock

from src.personas import load_personas

logger = logging.getLogger(__name__)

_MESSAGE_CLIP = 400
_RECENT_CLIP = 150
_RECENT_TURNS = 4

# byte-stable system prompt: one job, one word out.
_DECIDER_SYSTEM = (
    "you route one message in a shared room where a person lives with their "
    "council of helpers. exactly one helper will answer. pick the best fit "
    "from the candidates:\n"
    "- a message clearly in one helper's lane goes to that helper\n"
    "- general conversation, mixed topics, or anything ambiguous goes to "
    "the chair (the candidate marked so)\n"
    "- the recent context matters: a follow-up belongs to the lane already "
    "in play\n"
    "reply with ONLY the chosen helper's id. no punctuation, no reasoning, "
    "no other words."
)


def _clip(text: str, cap: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= cap else text[:cap - 1] + "…"


class SpeakingDecider:
    """one cheap call, strictly validated. constructed once at startup with
    the shared utility provider (the reconciler/curator pattern)."""

    def __init__(self, provider, provider_name: str, usage_recorder=None,
                 max_tokens: int = 16):
        self.provider = provider
        self.provider_name = provider_name
        self.usage = usage_recorder
        self.max_tokens = max_tokens

    async def decide(
        self,
        *,
        message: str,
        candidate_ids: Sequence[str],
        chair_id: str,
        recent=None,
        user_uuid: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> Optional[str]:
        """the chosen candidate id, or None for 'let the caller fall back'.
        never raises."""
        try:
            request = self._build_request(message, candidate_ids, chair_id,
                                          recent or [])
            response = await self.provider.create_message(request)
            if self.usage is not None and user_uuid:
                self.usage.record_call(
                    user_uuid=user_uuid, platform=platform or "unknown",
                    provider=self.provider_name, model=self.provider.model,
                    role="decider", usage=response.usage,
                )
            return self._parse(response.text, candidate_ids)
        except Exception as e:
            logger.warning("speaking decider failed (falling back): %s", e)
            return None

    # --- internals ----------------------------------------------------------

    def _build_request(self, message, candidate_ids, chair_id,
                       recent) -> AIRequest:
        cards = load_personas()
        lines = []
        for cid in candidate_ids:
            card = cards.get(cid)
            desc = f"{card.lane} - {card.specialty}" if card else "helper"
            marker = " (the chair)" if cid == chair_id else ""
            lines.append(f"- {cid}{marker}: {desc}")
        parts = ["candidates:\n" + "\n".join(lines)]

        tail = []
        for ev in list(recent)[-_RECENT_TURNS:]:
            if getattr(ev, "kind", "message") != "message":
                continue
            who = ("user" if getattr(ev, "author_type", "user") == "user"
                   else getattr(ev, "author", "helper"))
            tail.append(f"[{who}] {_clip(ev.content, _RECENT_CLIP)}")
        if tail:
            parts.append("recent context (older -> newer):\n" + "\n".join(tail))

        parts.append(f'their message:\n"{_clip(message, _MESSAGE_CLIP)}"')
        parts.append("which helper id?")
        return AIRequest(
            system=[SystemBlock(text=_DECIDER_SYSTEM)],
            messages=[ChatTurn(role="user", content="\n\n".join(parts))],
            tools=[],
            max_tokens=self.max_tokens,
            # utility model; a routing enum needs no thinking budget
            effort=None,
        )

    @staticmethod
    def _parse(text: Optional[str], candidate_ids) -> Optional[str]:
        """strict: the reply must be exactly one known candidate id (allowing
        stray whitespace/punctuation). anything else - prose, an invented
        helper, an empty reply - is None, never a guess."""
        token = (text or "").strip().strip(".,!\"'`* \n\t").lower()
        for cid in candidate_ids:
            if token == cid.lower():
                return cid
        logger.info("decider reply %r matched no candidate", text)
        return None
