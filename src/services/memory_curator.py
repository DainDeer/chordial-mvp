"""the memory curator: one agent, one job - keep the memory table clean.

the write-time upsert (MemoriesManager.upsert_memory) only catches obvious
lexical duplicates. the curator handles the judgment calls a few minutes later:
merging near-duplicates that don't share vocabulary, rewording stale notes,
expiring dead episodic context, and promoting recurring facts to core.

it is NOT a tool-loop agent. it's a single utility-model planner call followed
by a deterministic, validated executor: the model *proposes* operations over the
user's memory table, and this code *disposes* - every op is checked against the
user's real active rows, core memories are protected, weights are capped. a
hallucinated id is a rejected op, never a corrupted table.

triggered off the scheduler loop, debounced so it runs about once per
conversation-burst rather than mid-chat.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Optional

from src.database.database import get_db
from src.database.models import Memory
from src.providers.ai.base import BaseAIProvider
from src.providers.ai.types import AIRequest, ChatTurn, SystemBlock, ProviderError
from src.services.usage_recorder import UsageRecorder
from src.utils.timezone_utils import utc_now

logger = logging.getLogger(__name__)


_MAX_WEIGHT = 10.0      # non-core cap, matches upsert reinforcement
_CORE_WEIGHT = 999.0


CURATOR_SYSTEM = """you are the memory curator for a personal companion app. you maintain the memory database for ONE user: a table of short notes the assistant saved about them.

your job is to keep it clean and useful. you propose operations; a separate system validates and applies them. be conservative - when in doubt, leave a memory alone.

operations you can propose:
- merge: two or more rows are the same fact. give the canonical id to keep, the ids to absorb, and the blended instruction/keywords to write onto the canonical row. the absorbed rows are retired.
- update: reword a stale or awkward instruction, or adjust its keywords. optionally nudge its importance with weight_delta (small integers, e.g. +1 or -1).
- expire: a row is dead context - a passing "this week" note that's clearly past, or something no longer true. it gets deactivated.
- promote: a recurring, identity-level fact that should always be front-of-mind. it becomes a core memory.

rules:
- NEVER merge, expire, or demote a memory marked "core: true". you may only leave it or (rarely) it is already core.
- do NOT merge two rows that are merely similar in topic but are actually distinct facts (e.g. "likes tea" vs "likes coffee"). only merge genuine duplicates.
- prefer the fewest operations. an empty operations list is a perfectly good answer if the table is already clean.

respond with ONLY a json object, no prose, no code fences:
{"operations": [ ... ]}

each operation is one of:
{"op": "merge", "canonical_id": <int>, "absorb_ids": [<int>, ...], "instruction": "<blended text>", "keywords": ["...", ...]}
{"op": "update", "id": <int>, "instruction": "<new text>", "keywords": ["...", ...], "weight_delta": <int>}
{"op": "expire", "id": <int>}
{"op": "promote", "id": <int>}
for update, instruction/keywords/weight_delta are all optional - include only what changes."""


@dataclass
class CurationResult:
    user_uuid: str
    reviewed: int = 0
    applied: List[dict] = field(default_factory=list)   # ops actually executed
    rejected: List[dict] = field(default_factory=list)  # ops that failed validation
    error: Optional[str] = None


class MemoryCuratorService:
    def __init__(
        self,
        provider: BaseAIProvider,
        provider_name: str,
        usage_recorder: Optional[UsageRecorder] = None,
        debounce_minutes: int = 10,
        max_tokens: int = 1024,
    ):
        self.provider = provider
        self.provider_name = provider_name
        self.usage = usage_recorder or UsageRecorder()
        self.debounce = timedelta(minutes=debounce_minutes)
        self.max_tokens = max_tokens

    # --- discovery ---------------------------------------------------------

    async def find_users_needing_curation(self) -> List[str]:
        """users with active, un-reviewed memories whose most recent memory
        activity has settled (older than the debounce window) - so we curate
        once a conversation-burst is over, not in the middle of it."""
        cutoff = utc_now() - self.debounce
        with get_db() as db:
            rows = db.query(Memory).filter(
                Memory.is_active == True,
                Memory.curated_at.is_(None),
            ).all()

            newest_pending: dict[str, object] = {}
            for m in rows:
                stamp = m.last_reinforced_at or m.created_at
                cur = newest_pending.get(m.user_uuid)
                if cur is None or (stamp and stamp > cur):
                    newest_pending[m.user_uuid] = stamp

        return [
            uuid for uuid, newest in newest_pending.items()
            if newest is None or newest <= cutoff
        ]

    # --- main entry --------------------------------------------------------

    async def curate_user(self, user_uuid: str) -> CurationResult:
        """review one user's memory table and apply the model's cleanup plan."""
        memories = self._load_active(user_uuid)
        pending_ids = [m["id"] for m in memories if m["curated"] is False]
        if not pending_ids:
            return CurationResult(user_uuid=user_uuid, reviewed=0)

        result = CurationResult(user_uuid=user_uuid, reviewed=len(pending_ids))

        try:
            request = self._build_request(memories)
            response = await self.provider.create_message(request)
            self.usage.record_call(
                user_uuid=user_uuid, platform=None,
                provider=self.provider_name, model=self.provider.model,
                role="curator", usage=response.usage,
            )
            operations = self._parse_operations(response.text)
        except ProviderError as e:
            logger.error("curator provider error for user %s: %s", user_uuid, e)
            # leave rows pending; the debounce prevents a tight retry loop
            result.error = str(e)
            return result
        except Exception as e:
            logger.error("curator failed for user %s: %s", user_uuid, e)
            result.error = str(e)
            return result

        self._apply(user_uuid, memories, operations, result)

        # stamp everything we reviewed so it isn't re-curated next cycle
        self._mark_reviewed(pending_ids)

        self.usage.record_trace(
            user_uuid=user_uuid, platform=None, turn_kind="curation",
            iterations=1, hit_iteration_cap=False,
            tool_trace=result.applied, final_text_length=0,
            stop_reason=None, total_usage=response.usage,
        )
        if result.applied or result.rejected:
            logger.info(
                "curated user %s: %d applied, %d rejected (%d reviewed)",
                user_uuid, len(result.applied), len(result.rejected), result.reviewed,
            )
        return result

    # --- helpers -----------------------------------------------------------

    def _load_active(self, user_uuid: str) -> List[dict]:
        """active memories as detached-safe dicts (read while session is open)."""
        with get_db() as db:
            rows = db.query(Memory).filter(
                Memory.user_uuid == user_uuid,
                Memory.is_active == True,
            ).order_by(Memory.id).all()
            return [{
                "id": m.id,
                "type": m.memory_type,
                "instruction": m.ai_instruction,
                "keywords": m.keywords or "",
                "weight": m.weighting or 1.0,
                "core": bool(m.core),
                "reinforced_count": m.reinforced_count or 0,
                "curated": m.curated_at is not None,
            } for m in rows]

    def _build_request(self, memories: List[dict]) -> AIRequest:
        payload = [{
            "id": m["id"],
            "type": m["type"],
            "core": m["core"],
            "weight": round(m["weight"], 1),
            "times_seen": m["reinforced_count"] + 1,
            "instruction": m["instruction"],
            "keywords": m["keywords"],
        } for m in memories]

        user_msg = (
            "here is the user's full memory table. review it and return your "
            "operations json.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )
        return AIRequest(
            system=[SystemBlock(text=CURATOR_SYSTEM)],
            messages=[ChatTurn(role="user", content=user_msg)],
            tools=[],
            max_tokens=self.max_tokens,
            # no effort: the utility model (haiku) doesn't support output_config
            # effort, and this is a cheap structured task that doesn't need it.
            effort=None,
        )

    @staticmethod
    def _parse_operations(text: Optional[str]) -> List[dict]:
        """pull the operations list out of the model's reply, tolerating stray
        prose or ```json fences."""
        if not text:
            return []
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
        # find the outermost json object
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return []
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            logger.warning("curator returned unparseable json")
            return []
        ops = data.get("operations", [])
        return ops if isinstance(ops, list) else []

    def _apply(self, user_uuid: str, memories: List[dict], operations: List[dict],
               result: CurationResult) -> None:
        """execute validated operations. the model proposed; we verify every id
        belongs to this user's active set and protect core rows."""
        by_id = {m["id"]: m for m in memories}
        core_ids = {m["id"] for m in memories if m["core"]}

        with get_db() as db:
            for op in operations:
                kind = op.get("op")
                try:
                    if kind == "merge":
                        self._do_merge(db, op, by_id, core_ids, result)
                    elif kind == "update":
                        self._do_update(db, op, by_id, core_ids, result)
                    elif kind == "expire":
                        self._do_expire(db, op, by_id, core_ids, result)
                    elif kind == "promote":
                        self._do_promote(db, op, by_id, core_ids, result)
                    else:
                        result.rejected.append({"op": op, "reason": f"unknown op '{kind}'"})
                except Exception as e:
                    logger.error("curator op failed (%s): %s", op, e)
                    result.rejected.append({"op": op, "reason": str(e)})
            db.commit()

    def _valid_target(self, mid, by_id, core_ids, *, allow_core: bool) -> bool:
        return mid in by_id and (allow_core or mid not in core_ids)

    def _do_merge(self, db, op, by_id, core_ids, result):
        canonical_id = op.get("canonical_id")
        absorb_ids = [i for i in (op.get("absorb_ids") or []) if i != canonical_id]
        if not self._valid_target(canonical_id, by_id, core_ids, allow_core=False):
            result.rejected.append({"op": op, "reason": "bad canonical_id"})
            return
        if not absorb_ids or any(
            not self._valid_target(i, by_id, core_ids, allow_core=False) for i in absorb_ids
        ):
            result.rejected.append({"op": op, "reason": "bad absorb_ids"})
            return

        canonical = db.query(Memory).filter(Memory.id == canonical_id).first()
        absorbed = db.query(Memory).filter(Memory.id.in_(absorb_ids)).all()
        if canonical is None or len(absorbed) != len(absorb_ids):
            result.rejected.append({"op": op, "reason": "rows not found"})
            return

        if op.get("instruction"):
            canonical.ai_instruction = op["instruction"].strip()
        keywords = _merge_keywords(op.get("keywords"), canonical, absorbed)
        canonical.keywords = keywords
        # weight of a merged memory = sum of the parts, capped
        total_weight = (canonical.weighting or 1.0) + sum(a.weighting or 1.0 for a in absorbed)
        canonical.weighting = min(_MAX_WEIGHT, total_weight)
        canonical.curated_at = utc_now()

        now = utc_now()
        for a in absorbed:
            a.is_active = False
            a.merged_into = canonical_id
            a.curated_at = now
        result.applied.append({
            "op": "merge", "canonical_id": canonical_id, "absorbed": absorb_ids,
        })

    def _do_update(self, db, op, by_id, core_ids, result):
        mid = op.get("id")
        if not self._valid_target(mid, by_id, core_ids, allow_core=False):
            result.rejected.append({"op": op, "reason": "bad id / core protected"})
            return
        m = db.query(Memory).filter(Memory.id == mid).first()
        if m is None:
            result.rejected.append({"op": op, "reason": "not found"})
            return
        if op.get("instruction"):
            m.ai_instruction = op["instruction"].strip()
        if op.get("keywords"):
            m.keywords = _clean_keywords(op["keywords"])
        delta = op.get("weight_delta")
        if isinstance(delta, (int, float)) and delta:
            m.weighting = max(0.1, min(_MAX_WEIGHT, (m.weighting or 1.0) + float(delta)))
        m.curated_at = utc_now()
        result.applied.append({"op": "update", "id": mid})

    def _do_expire(self, db, op, by_id, core_ids, result):
        mid = op.get("id")
        if not self._valid_target(mid, by_id, core_ids, allow_core=False):
            result.rejected.append({"op": op, "reason": "bad id / core protected"})
            return
        m = db.query(Memory).filter(Memory.id == mid).first()
        if m is None:
            result.rejected.append({"op": op, "reason": "not found"})
            return
        m.is_active = False
        m.curated_at = utc_now()
        result.applied.append({"op": "expire", "id": mid})

    def _do_promote(self, db, op, by_id, core_ids, result):
        mid = op.get("id")
        # promote is the one op allowed to touch a (soon-to-be) core row; but a
        # row that's already core is a no-op we just skip.
        if mid not in by_id or mid in core_ids:
            result.rejected.append({"op": op, "reason": "bad id / already core"})
            return
        m = db.query(Memory).filter(Memory.id == mid).first()
        if m is None:
            result.rejected.append({"op": op, "reason": "not found"})
            return
        m.core = True
        m.weighting = _CORE_WEIGHT
        m.curated_at = utc_now()
        result.applied.append({"op": "promote", "id": mid})
        logger.info("curator promoted memory %s to core", mid)

    def _mark_reviewed(self, pending_ids: List[int]) -> None:
        """stamp curated_at on rows still pending after the plan ran (untouched
        ones), so a clean table isn't re-reviewed every cycle."""
        now = utc_now()
        with get_db() as db:
            rows = db.query(Memory).filter(
                Memory.id.in_(pending_ids),
                Memory.curated_at.is_(None),
            ).all()
            for m in rows:
                m.curated_at = now
            db.commit()


def _clean_keywords(keywords) -> str:
    if isinstance(keywords, str):
        parts = [k.strip() for k in keywords.split(",")]
    else:
        parts = [str(k).strip() for k in (keywords or [])]
    seen = {k.lower(): k for k in parts if k}
    return ",".join(sorted(seen.values(), key=str.lower))


def _merge_keywords(provided, canonical, absorbed) -> str:
    if provided:
        return _clean_keywords(provided)
    # no explicit keywords - union what's already on the rows
    combined = set()
    for m in [canonical, *absorbed]:
        combined |= {k.strip() for k in (m.keywords or "").split(",") if k.strip()}
    return ",".join(sorted(combined, key=str.lower))
