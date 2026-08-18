"""assessment tools: reading edwin's filed scorecards.

the phase-6 consumption path (ROOMS_DESIGN.md §8). assessments are written
by the cycle scorer only - a background job with deterministic scores and
evidence-checked findings - so the chat surface is deliberately read-only:
no helper, edwin included, can file or edit a scorecard from conversation.
what the ledger says is what gets quoted.
"""
import logging

from dainframe.providers.types import ToolDef
from dainframe.tools.context import ToolContext
from dainframe.tools.registry import Tool

from src.services.cycle_scorer import recent_assessments
from src.services.identity import user_of_context
from src.services.scorecard import COMPONENTS

logger = logging.getLogger(__name__)


def _render_assessment(row: dict) -> str:
    when = (row["created_at"] or "")[:10] or "?"
    lines = [f"- [{when}] {row['subject_type']} {row['subject_id'] or ''}: "
             f"{row['summary']}"]
    detail = row.get("detail") or {}
    components = detail.get("components") or {}
    scores = []
    for name in COMPONENTS:
        comp = components.get(name) or {}
        score = comp.get("score")
        scores.append(f"{name} {score if score is not None else 'n/a'}")
    if scores:
        lines.append(f"    scores: {', '.join(scores)}")
    for finding in detail.get("findings") or []:
        line = f"    finding: {finding.get('claim')}"
        action = finding.get("suggested_action")
        if action:
            line += f" -> {action}"
        lines.append(line)
    return "\n".join(lines)


async def _list_assessments(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    rows = recent_assessments(
        user_uuid,
        subject_type=tool_input.get("subject_type"),
        limit=int(tool_input.get("limit") or 5),
    )
    if not rows:
        return ("no assessments filed yet - the first scorecard lands when "
                "a cycle ends.")
    rendered = "\n".join(_render_assessment(r) for r in rows)
    return f"{len(rows)} assessment(s), newest first:\n{rendered}"


LIST_ASSESSMENTS = Tool(
    definition=ToolDef(
        name="list_assessments",
        description=(
            "Read the filed scorecards - edwin's assessments of ended "
            "cycles: five component scores computed from the ledger "
            "(execution, prioritization, estimation, consistency, "
            "sustainability), a plain summary, and evidence-tied findings. "
            "Use when reviewing how a cycle actually went, or before "
            "claiming a trend across cycles. Read-only: scorecards are "
            "filed by the scorer when a cycle ends, never from "
            "conversation. Scores judge the plan and the system, never the "
            "person - quote them that way."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "subject_type": {
                    "type": "string",
                    "enum": ["cycle"],
                    "description": "Filter by subject; omit for all.",
                },
                "limit": {"type": "integer",
                          "description": "Default 5, max 50."},
            },
        },
    ),
    handler=_list_assessments,
    record_event=False,  # pure read: results go stale, nothing to dedup
)
