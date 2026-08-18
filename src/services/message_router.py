"""outbound message router: the one place that knows how to deliver a message
to a (platform, user) - and, since phase 3, as a specific helper.

replaces the hand-rolled callback in main.py that string-matched the platform
and sniffed `hasattr(interface, 'send_message')`. interfaces register by their
`platform` name; the scheduler (or anything else) calls `deliver(...)` without
knowing which interface backs a platform. when a send is permanently
undeliverable, the router deactivates that platform link so we stop paying to
generate messages for a dead channel.

the registry keys on `(platform, helper_id)`; every current platform is
single-bot and registers under `(platform, None)`, attributing speakers
in-band (the app puts the speaker in the payload; the tether renders it into
the text). the helper_id sub-key survives the phase-7a ensemble retirement
because the resolution rule it encodes - never impersonate a helper through
an account that isn't theirs - is the right default for any future platform
that does grow per-speaker accounts. `deliver_as(..., speaker=...)` names
the speaking helper; the legacy 3-arg `deliver` speaks as the chair.

the tether mirror (docs/ROOMS_DESIGN.md section 9, "one conversation, two
windows"): a council line CONFIRMED delivered to telegram is also fanned,
best-effort, to the user's connected app surfaces - the desktop shows the
phone conversation live instead of at the next refetch. the mirror never
affects the delivery verdict: the event log records against the telegram
confirmation, and a desk that misses the echo still sees the line in
history.
"""

import logging
from typing import Dict, Optional, Tuple

from src.providers.platforms.base import BaseInterface, UndeliverableError
from src.managers.user_manager import UserManager
from src.personas import CHAIR_ID

logger = logging.getLogger(__name__)


class MessageRouter:
    def __init__(self, user_manager: UserManager):
        # keyed by (platform, helper_id); helper_id is None for single-bot
        # platforms (discord). one telegram entry per helper bot.
        self._interfaces: Dict[Tuple[str, Optional[str]], BaseInterface] = {}
        self._user_manager = user_manager

    def register(self, interface: BaseInterface) -> None:
        platform = interface.platform
        if not platform:
            raise ValueError(
                f"{type(interface).__name__} must set a `platform` name to register"
            )
        helper_id = getattr(interface, "helper_id", None)
        key = (platform, helper_id)
        if key in self._interfaces:
            logger.warning("overwriting already-registered interface for %s", key)
        self._interfaces[key] = interface
        logger.info(
            "registered interface for platform '%s' (helper=%s)", platform, helper_id
        )

    def platforms(self) -> list[str]:
        """distinct platforms that currently have a live interface - the
        scheduler drives its loop off this instead of a hardcoded list. deduped
        across the per-helper interfaces a platform may have."""
        seen: list[str] = []
        for platform, _helper_id in self._interfaces:
            if platform not in seen:
                seen.append(platform)
        return seen

    def deliverable_speakers(self, platform: str) -> Optional[set]:
        """which speaker ids `platform` can actually send as - the director's
        candidate filter (casting a speaker this returns False for would fail
        closed in _resolve). None means unrestricted: a (platform, None)
        interface serves ANY speaker - the app attributes in-band via the
        payload, the tether renders attribution into the text, discord is a
        single untyped bot. a platform with only helper-keyed interfaces
        (none today; the rule outlives the ensemble) can send only as
        exactly those helpers."""
        speakers: set = set()
        for p, helper_id in self._interfaces:
            if p != platform:
                continue
            if helper_id is None:
                return None
            speakers.add(helper_id)
        return speakers

    def _resolve(
        self, platform: str, speaker: Optional[str]
    ) -> Optional[BaseInterface]:
        """find the interface that should send as `speaker` on `platform`:
        the exact (platform, speaker) bot, else the platform's single-bot
        (platform, None) interface. Platforms with helper-specific interfaces
        fail closed if the requested speaker is unavailable, rather than
        impersonating that helper through a different bot account."""
        interface = self._interfaces.get((platform, speaker))
        if interface is not None:
            return interface
        interface = self._interfaces.get((platform, None))
        if interface is not None:
            return interface
        return None

    async def deliver_as(
        self, platform: str, target_id: str, message: str, speaker: str,
        stream_id: Optional[str] = None,
    ) -> bool:
        """send `message` to `target_id` on `platform` AS `speaker` (a helper
        id). resolves the speaker's bot (falling back only to the platform's
        single-bot interface), returns True on success. on a permanent
        failure, deactivates the platform link and returns False.
        `stream_id` names the room the line belongs to, for interfaces that
        attribute in-band (the app puts it in the payload so a client showing
        one room can ignore another room's lines); others ignore it."""
        interface = self._resolve(platform, speaker)
        if interface is None:
            logger.error(
                "no interface registered for platform '%s' (speaker=%s)",
                platform,
                speaker,
            )
            return False

        try:
            # every platform attributes in-band now: the app carries the
            # speaker in its payload, the tether renders it into the text
            ok = await interface.send_message(target_id, message,
                                              speaker=speaker,
                                              stream_id=stream_id)
            if ok and platform == "telegram":
                await self._mirror_to_app(target_id, message, speaker,
                                          stream_id)
            return ok
        except UndeliverableError as e:
            logger.warning(
                "permanent delivery failure for %s:%s (speaker=%s) - %s; deactivating link",
                platform,
                target_id,
                speaker,
                e,
            )
            await self._user_manager.deactivate_platform_identity(platform, target_id)
            return False

    async def _mirror_to_app(
        self, telegram_user_id: str, message: str, speaker: str,
        stream_id: Optional[str],
    ) -> None:
        """one conversation, two windows: echo a telegram-confirmed council
        line to the user's connected app surfaces. best-effort by contract -
        no app interface, no linked user, or no listening surface all no-op
        silently (history covers the desk on its next fetch), and a failure
        here never touches the already-confirmed delivery verdict."""
        interface = self._interfaces.get(("app", None))
        if interface is None:
            return
        try:
            user_uuid = await self._user_manager.lookup_user_uuid(
                "telegram", telegram_user_id)
            if user_uuid is None:
                return
            await interface.send_message(user_uuid, message, speaker=speaker,
                                         stream_id=stream_id,
                                         source_platform="telegram")
        except Exception:
            logger.exception("telegram->app mirror failed; continuing without")

    async def deliver_choice_as(
        self,
        platform: str,
        target_id: str,
        message: str,
        choices: list,
        speaker: str,
    ) -> bool:
        """send a message with inline choices (the rewind tether's ping) as
        `speaker`. same resolution and same deactivate-on-permanent-failure
        contract as deliver_as; interfaces without an interactive surface
        return False and the link stays active."""
        interface = self._resolve(platform, speaker)
        if interface is None:
            logger.error(
                "no interface registered for platform '%s' (speaker=%s)",
                platform,
                speaker,
            )
            return False
        try:
            return await interface.send_choice(target_id, message, choices,
                                               speaker=speaker)
        except UndeliverableError as e:
            logger.warning(
                "permanent delivery failure for %s:%s (speaker=%s) - %s; deactivating link",
                platform,
                target_id,
                speaker,
                e,
            )
            await self._user_manager.deactivate_platform_identity(platform, target_id)
            return False

    async def deliver(self, platform: str, platform_user_id: str, message: str) -> bool:
        """send `message` to `platform_user_id` on `platform`, as the chair.
        the legacy 3-arg hook the scheduler and the orchestrator's
        switch-notice call; delegates to deliver_as with the default speaker."""
        return await self.deliver_as(
            platform, platform_user_id, message, speaker=CHAIR_ID
        )
