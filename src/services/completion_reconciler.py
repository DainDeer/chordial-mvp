"""the completion reconciler: mark tasks done when the user mentions doing them.

the persona model reliably marks a task complete when that's the whole message
("that's done!!"), but misses completions mentioned in passing inside a longer,
emotional message ("i walked outside AND practiced piano :3") - two jobs (be
warm / notice the buried action) competing in one call, and warmth wins.

so this is a second, narrow pass run after the companion replies: one cheap
utility-model call whose ONLY job is to match the user's message against their
open tasks and return the ones they reported doing. a deterministic validated
executor then marks those Done - and only ids that are actually open, so a
hallucinated id is a rejected op, never a bad write. silent bookkeeping, like a
memory save; the Done action is recorded as an event, so the companion sees it
next turn and won't re-ask.

the completion bar lives HERE and nowhere else (it's the info the owner said
should only reach the reconciler): many of their tasks are lightweight "just do
X" nudges with no finish line, so for a generic activity ANY amount done counts;
only tasks that name a concrete deliverable need full completion.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from src.providers.ai.base import BaseAIProvider
from src.providers.ai.types import AIRequest, ChatTurn, SystemBlock, ToolCall, ProviderError
from src.services.agent_service import ExecutedAction
from src.services.tools import ToolRegistry
from src.services.usage_recorder import UsageRecorder

logger = logging.getLogger(__name__)


RECONCILER_SYSTEM = """you check whether a user just reported finishing any of their open tasks.

you're given their latest message (with a little recent context) and a list of their currently-open tasks. return the tasks they've indicated they did.

how to judge "done" for THIS user - important:
- many of their tasks are lightweight nudges with no real finish line: "practice piano", "go for a walk", "look into the vr fitness schedule". these are generic activities. if the user mentions doing that activity AT ALL - even a little, even in passing - it counts as done. "practiced some chords in c" completes "practice piano"; "walked outside" completes "go for a walk".
- only require real completion when the task's title names a specific, measurable finish line: "write 500 words", "finish chapter 3", "do all c major scales". for those, a mention of partial progress does NOT complete it.

be conservative about MATCHING: only pick a task the user clearly referred to. if you're unsure the message is even about a given task, leave it open. but once you're confident they touched a generic activity, mark it done per the rule above.

respond with ONLY a json object, no prose, no code fences:
{"completed": [{"id": "<task id>", "why": "<a few words>"}]}
an empty list is the correct, common answer."""


@dataclass
class ReconcileResult:
    user_uuid: str
    considered: int = 0                                   # open tasks weighed
    actions: List[ExecutedAction] = field(default_factory=list)  # Done marks executed
    rejected: List[dict] = field(default_factory=list)    # ops that failed validation
    error: Optional[str] = None


class CompletionReconcilerService:
    def __init__(
        self,
        provider: BaseAIProvider,
        provider_name: str,
        agenda_service,
        tool_registry: ToolRegistry,
        usage_recorder: Optional[UsageRecorder] = None,
        max_tokens: int = 512,
        recent_context: int = 4,
    ):
        self.provider = provider
        self.provider_name = provider_name
        self.agenda = agenda_service
        self.registry = tool_registry
        self.usage = usage_recorder or UsageRecorder()
        self.max_tokens = max_tokens
        self.recent_context = recent_context

    async def reconcile(
        self,
        user_uuid: str,
        platform: Optional[str],
        message_text: str,
        recent: Optional[List] = None,
    ) -> ReconcileResult:
        """look at one user message against their open tasks and mark done the
        ones they reported doing. returns the executed Done actions (empty when
        nothing matched, or when there's nothing to reconcile against)."""
        result = ReconcileResult(user_uuid=user_uuid)

        open_tasks = self._open_tasks(user_uuid)
        result.considered = len(open_tasks)
        # nothing open, no update_task tool, or an empty message -> no llm call
        has_update = any(d.name == "update_task" for d in self.registry.definitions())
        if not open_tasks or not has_update or not (message_text or "").strip():
            return result

        try:
            request = self._build_request(message_text, open_tasks, recent)
            response = await self.provider.create_message(request)
            self.usage.record_call(
                user_uuid=user_uuid, platform=platform,
                provider=self.provider_name, model=self.provider.model,
                role="reconciler", usage=response.usage,
            )
            completed_ids = self._parse_completed(response.text)
        except ProviderError as e:
            logger.error("reconciler provider error for user %s: %s", user_uuid, e)
            result.error = str(e)
            return result
        except Exception as e:
            logger.error("reconciler failed for user %s: %s", user_uuid, e)
            result.error = str(e)
            return result

        await self._apply(user_uuid, open_tasks, completed_ids, result)
        if result.actions:
            logger.info("reconciler marked %d task(s) done for user %s",
                        len(result.actions), user_uuid)
        return result

    # --- helpers -----------------------------------------------------------

    def _open_tasks(self, user_uuid: str) -> List[dict]:
        """the user's open tasks from the cached agenda snapshot (today +
        overdue + in-progress), deduped by id. a pure db read - the snapshot is
        kept fresh by the scheduler, never fetched from notion on this path."""
        try:
            payload = self.agenda.get_payload(user_uuid)
        except Exception:
            logger.exception("reconciler could not read agenda payload")
            return []
        if not payload:
            return []
        seen, tasks = set(), []
        for bucket in ("tasks_today", "tasks_overdue", "tasks_in_progress"):
            for t in payload.get(bucket) or []:
                tid = t.get("id")
                if tid and tid not in seen:
                    seen.add(tid)
                    tasks.append({"id": tid, "title": t.get("title", ""),
                                  "status": t.get("status", "")})
        return tasks

    def _build_request(self, message_text, open_tasks, recent) -> AIRequest:
        parts = [f'their latest message:\n"{message_text.strip()}"']
        context = self._format_recent(recent)
        if context:
            parts.append("recent context (older -> newer):\n" + context)
        parts.append("their open tasks:\n" + json.dumps(open_tasks, ensure_ascii=False))
        parts.append("which of these open tasks did they just report doing? return the json.")
        return AIRequest(
            system=[SystemBlock(text=RECONCILER_SYSTEM)],
            messages=[ChatTurn(role="user", content="\n\n".join(parts))],
            tools=[],
            max_tokens=self.max_tokens,
            # utility model (haiku) rejects output_config effort; this is a
            # cheap structured task that doesn't need it anyway
            effort=None,
        )

    def _format_recent(self, recent) -> str:
        """render a short tail of prior message events for pronoun/context
        disambiguation. actions and the current message are excluded by the
        caller; we just label speakers."""
        if not recent:
            return ""
        lines = []
        for ev in recent[-self.recent_context:]:
            speaker = "user" if getattr(ev, "role", "user") == "user" else "chordial"
            lines.append(f"[{speaker}] {ev.content}")
        return "\n".join(lines)

    @staticmethod
    def _parse_completed(text: Optional[str]) -> List[str]:
        """pull the completed-task ids out of the reply, tolerating stray prose
        or ```json fences."""
        if not text:
            return []
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return []
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            logger.warning("reconciler returned unparseable json")
            return []
        ids = []
        for item in data.get("completed", []) or []:
            tid = item.get("id") if isinstance(item, dict) else item
            if isinstance(tid, str) and tid:
                ids.append(tid)
        return ids

    async def _apply(self, user_uuid, open_tasks, completed_ids, result) -> None:
        """mark each proposed id Done - but only if it's genuinely one of this
        user's open tasks. the model proposed; we verify against the real open
        set (a hallucinated or already-closed id is rejected, never written)."""
        open_by_id = {t["id"]: t for t in open_tasks}
        # dedupe while preserving order
        for tid in dict.fromkeys(completed_ids):
            if tid not in open_by_id:
                result.rejected.append({"id": tid, "reason": "not an open task"})
                continue
            call = ToolCall(id=f"reconcile-{tid[:8]}", name="update_task",
                            input={"task": tid, "status": "Done"})
            tool_result = await self.registry.execute(call, user_uuid)
            result.actions.append(ExecutedAction(
                name="update_task", input=call.input,
                result_content=tool_result.content, is_error=tool_result.is_error,
                terminal=self.registry.is_terminal("update_task"),
            ))
