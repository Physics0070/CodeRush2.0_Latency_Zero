"""Prompt-injection screening on handoffs.

A payload produced by one agent is untrusted input to the next. It gets scanned
for instruction-like content *before* it enters the downstream context, because
once the model has read it the damage is done.

Detection is pattern-based and deliberately conservative. It is a demonstrable
control, not a claim to have solved prompt injection - a determined attacker
gets past regexes. What it does guarantee is that the attempt is blocked,
quarantined and visible in the trace, which is what the PS asks for.
"""

import json
import re

from pydantic import BaseModel

from backend.events import EventStore, EventType

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("override_instruction", re.compile(
        r"\b(ignore|disregard|forget|override)\b.{0,30}\b"
        r"(previous|prior|above|earlier|all)\b.{0,20}"
        r"(instruction|prompt|rule|direction|context)", re.I)),
    ("role_marker", re.compile(
        r"(^|\n)\s*(system|assistant|user)\s*:", re.I)),
    ("chat_template_token", re.compile(
        r"<\|(im_start|im_end|system|user|assistant|endoftext)\|>", re.I)),
    ("tool_call_syntax", re.compile(
        r"(<tool_call>|<function_call>|```tool_code|\{\s*\"tool_call\"\s*:)", re.I)),
    ("exfiltration", re.compile(
        r"\b(reveal|print|output|show|send|leak)\b.{0,30}\b"
        r"(system prompt|api[_ ]?key|secret|credential|token|password|env)", re.I)),
    ("privilege_escalation", re.compile(
        r"\b(you are now|from now on you|act as|pretend to be|new instructions?)\b", re.I)),
    ("destructive_imperative", re.compile(
        r"\b(delete|rm\s+-rf|drop\s+table|truncate|shutdown|wipe)\b.{0,25}"
        r"\b(file|table|database|repo|everything|all)\b", re.I)),
]


class ScreenResult(BaseModel):
    clean: bool
    hits: list[dict] = []

    @property
    def summary(self) -> str:
        return ", ".join(h["rule"] for h in self.hits)


def screen(payload: object) -> ScreenResult:
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    hits = []
    for rule, pattern in PATTERNS:
        m = pattern.search(text)
        if m:
            hits.append({
                "rule": rule,
                # Enough context to judge, bounded so the event stays small.
                "match": m.group(0)[:200],
                "offset": m.start(),
            })
    return ScreenResult(clean=not hits, hits=hits)


async def screen_payload(
    store: EventStore,
    run_id: str,
    payload: dict,
    *,
    node_id: str | None = None,
    agent_id: str | None = None,
) -> tuple[dict, bool]:
    """Returns (payload_to_use, was_blocked).

    Quarantine is per upstream node: one poisoned branch does not take down the
    siblings that were fine.
    """
    blocked_any = False
    clean: dict = {}
    for source, value in (payload or {}).items():
        result = screen(value)
        if result.clean:
            clean[source] = value
            continue
        blocked_any = True
        await store.append(
            run_id,
            EventType.INJECTION_BLOCKED,
            node_id=node_id,
            agent_id=agent_id,
            payload={
                "from_node": source,
                "into_node": node_id,
                "rules": [h["rule"] for h in result.hits],
                "hits": result.hits,
                "action": "quarantined",
            },
        )
    return clean, blocked_any
