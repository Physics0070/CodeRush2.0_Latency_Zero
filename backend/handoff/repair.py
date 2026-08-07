"""The repair loop.

A rejected handoff is not a dead branch. The producing agent gets the exact
validator output back and is asked to correct it, up to MAX_REPAIR_ATTEMPTS.
Every step lands in the event log, so Handoff Validity in the metrics panel is
counted from real rows rather than asserted.
"""

import logging
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from backend.config import settings
from backend.events import EventStore, EventType
from backend.handoff.validate import ValidationResult, validate

log = logging.getLogger("aco.handoff")

# Called with None on the first attempt, then with the validator's error text.
Producer = Callable[[str | None], Awaitable[str | dict | list]]

REPAIR_INSTRUCTION = (
    "Your previous output failed schema validation and was rejected.\n"
    "Validation errors:\n{errors}\n\n"
    "Required JSON Schema:\n{schema}\n\n"
    "Return ONLY corrected JSON that satisfies the schema. No prose, no markdown fence."
)


class HandoffOutcome(BaseModel):
    ok: bool
    payload: dict | list | None = None
    attempts: int = 0
    errors: list[str] = []
    first_seq: int | None = None
    final_seq: int | None = None


async def repair_loop(
    store: EventStore,
    run_id: str,
    *,
    produce: Producer,
    schema: dict,
    agent_id: str,
    node_id: str | None = None,
    max_attempts: int | None = None,
) -> HandoffOutcome:
    """Emit -> validate -> on failure return the errors and retry.

    `max_attempts` counts repairs, not total calls: the default of 2 means one
    initial emission plus two corrections.
    """
    budget = settings.max_repair_attempts if max_attempts is None else max_attempts
    errors: list[str] = []
    first_seq: int | None = None
    feedback: str | None = None

    for attempt in range(budget + 1):
        raw = await produce(feedback)

        seq = await store.append(
            run_id,
            EventType.HANDOFF_EMITTED,
            node_id=node_id,
            agent_id=agent_id,
            payload={"attempt": attempt, "raw": _clip(raw)},
        )
        first_seq = first_seq or seq

        result: ValidationResult = validate(raw, schema)

        if result.ok:
            final = await store.append(
                run_id,
                EventType.HANDOFF_VALIDATED,
                node_id=node_id,
                agent_id=agent_id,
                payload={"attempt": attempt, "payload": result.payload},
            )
            return HandoffOutcome(
                ok=True,
                payload=result.payload,
                attempts=attempt,
                first_seq=first_seq,
                final_seq=final,
            )

        errors = result.errors
        await store.append(
            run_id,
            EventType.HANDOFF_REJECTED,
            node_id=node_id,
            agent_id=agent_id,
            payload={"attempt": attempt, "errors": errors},
        )

        if attempt == budget:
            break

        # The exact validator text goes back to the producer - not a paraphrase.
        feedback = REPAIR_INSTRUCTION.format(errors=result.error_text, schema=_schema_text(schema))
        await store.append(
            run_id,
            EventType.REPAIR_ATTEMPT,
            node_id=node_id,
            agent_id=agent_id,
            payload={"attempt": attempt + 1, "of": budget, "errors_returned": errors},
        )
        log.info("repair %s/%s for agent=%s: %s", attempt + 1, budget, agent_id, errors)

    final = await store.append(
        run_id,
        EventType.BRANCH_FAILED,
        node_id=node_id,
        agent_id=agent_id,
        payload={"reason": "handoff_unrepairable", "attempts": budget + 1, "errors": errors},
    )
    return HandoffOutcome(
        ok=False, attempts=budget + 1, errors=errors, first_seq=first_seq, final_seq=final
    )


def _schema_text(schema: dict) -> str:
    import json

    return json.dumps(schema, indent=2)[:1500]


def _clip(raw: object, limit: int = 4000) -> str:
    """Event payloads stay bounded; the artifact store holds the full value."""
    text = raw if isinstance(raw, str) else repr(raw)
    return text[:limit]
