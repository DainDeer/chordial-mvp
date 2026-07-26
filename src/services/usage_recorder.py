"""writes the usage ledger and agent traces.

kept as a thin seam so cost accounting has exactly one home. the sqlite writes
are synchronous (matching the rest of the codebase) - fine at conversational
pace; swap for a batched/async sink if write volume ever matters.

this is also chordial's dainframe UsageSink: `emit` receives the loop's
ProviderCallUsage / AgentRunTrace events and maps them onto the two ledger
writes (actor -> helper_id, stream_id -> user_uuid, turn_kind -> role). the
sync record_call/record_trace methods stay for the direct utility-model
callers (curator, reconciler) that don't run through an AgentLoop.
"""

import logging
from typing import Optional

from dainframe.loop.usage import AgentRunTrace, ProviderCallUsage, UsageEvent
from dainframe.providers.types import Usage

from src.database.database import get_db
from src.database.models import UsageLog, AgentTrace

logger = logging.getLogger(__name__)


class UsageRecorder:
    async def emit(self, event: UsageEvent) -> None:
        """the dainframe UsageSink entry point. failure is already guarded on
        the loop side; the sync writes below guard themselves too."""
        if isinstance(event, ProviderCallUsage):
            self.record_call(
                user_uuid=event.stream_id,
                platform=event.platform,
                provider=event.provider,
                model=event.model,
                role=event.turn_kind,
                usage=event.usage,
                helper_id=event.actor,
            )
        elif isinstance(event, AgentRunTrace):
            self.record_trace(
                user_uuid=event.stream_id,
                platform=event.platform,
                turn_kind=event.turn_kind,
                iterations=event.iterations,
                hit_iteration_cap=event.hit_iteration_cap,
                tool_trace=list(event.tool_trace),
                final_text_length=event.final_text_length,
                stop_reason=event.stop_reason,
                total_usage=event.total_usage,
                helper_id=event.actor,
            )
        else:
            logger.warning("unknown usage event type: %r", type(event))

    def record_call(
        self,
        *,
        user_uuid: Optional[str],
        platform: Optional[str],
        provider: str,
        model: str,
        role: str,
        usage: Usage,
        helper_id: Optional[str] = None,
    ) -> None:
        try:
            with get_db() as db:
                db.add(
                    UsageLog(
                        user_uuid=user_uuid,
                        platform=platform,
                        helper_id=helper_id or self._utility_helper(role),
                        provider=provider,
                        model=model,
                        role=role,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cache_read_tokens=usage.cache_read_tokens,
                        cache_write_tokens=usage.cache_write_tokens,
                    )
                )
        except Exception as e:
            # accounting must never break the chat path
            logger.error("failed to record usage: %s", e)

    def record_trace(
        self,
        *,
        user_uuid: Optional[str],
        platform: Optional[str],
        turn_kind: str,
        iterations: int,
        hit_iteration_cap: bool,
        tool_trace: list,
        final_text_length: int,
        stop_reason: Optional[str],
        total_usage: Usage,
        helper_id: Optional[str] = None,
    ) -> None:
        try:
            with get_db() as db:
                db.add(
                    AgentTrace(
                        user_uuid=user_uuid,
                        platform=platform,
                        helper_id=helper_id or self._utility_helper(turn_kind),
                        turn_kind=turn_kind,
                        iterations=iterations,
                        hit_iteration_cap=hit_iteration_cap,
                        tool_trace=tool_trace,
                        final_text_length=final_text_length,
                        stop_reason=stop_reason,
                        total_input_tokens=total_usage.input_tokens,
                        total_output_tokens=total_usage.output_tokens,
                        total_cache_read_tokens=total_usage.cache_read_tokens,
                        total_cache_write_tokens=total_usage.cache_write_tokens,
                    )
                )
        except Exception as e:
            logger.error("failed to record agent trace: %s", e)

    @staticmethod
    def _utility_helper(role: str) -> Optional[str]:
        """Attribute existing utility writers that do not pass helper_id yet.

        Keeping this fallback here preserves the thin recorder API used by the
        curator and reconciler while normal persona calls pass identity
        explicitly from AgentService.
        """
        return {
            "curator": "curator",
            "curation": "curator",
            "reconciler": "reconciler",
            "reconciliation": "reconciler",
        }.get(role)
