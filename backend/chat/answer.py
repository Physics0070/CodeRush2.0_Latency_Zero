"""Produce an answer a person can read.

The orchestrator used to end at a findings array with severity ranks, which is
the right output for an audit and the wrong output for a question. Both paths
here end in prose:

- `stream_direct` is the fast path. One model, tokens straight to the browser.
- `stream_synthesis` is the council path. Specialists work in parallel, and
  their contributions are merged into a single answer rather than shown raw -
  nobody wants to read four agents arguing.

Contributions are still typed handoffs, because the schema is what lets the
synthesizer trust its input. The schema just describes a contribution to an
answer now instead of a security finding.
"""

import logging
from collections.abc import AsyncIterator

from backend.providers import registry
from backend.providers.registry import adapter_for

log = logging.getLogger("aco.answer")

# The non-audit handoff shape. `support` is what keeps the synthesizer honest:
# a claim with no support is dropped rather than merged in.
CONTRIBUTION_SCHEMA = {
    "type": "object",
    "required": ["points"],
    "properties": {
        "points": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": {
                "type": "object",
                "required": ["claim", "support"],
                "properties": {
                    "claim": {"type": "string", "minLength": 3},
                    "support": {"type": "string", "minLength": 3},
                    "confidence": {"enum": ["low", "medium", "high"]},
                },
            },
        }
    },
}

SYSTEM = (
    "You are a helpful, direct assistant. Answer the question that was asked. "
    "Use markdown when it helps - code fences for code, lists for lists - and "
    "plain prose otherwise. Do not narrate your process, do not mention agents, "
    "councils or orchestration. If you are unsure, say so briefly and answer "
    "with what you do know."
)

SYNTHESIS_SYSTEM = (
    "You are writing the final answer to a user's question. Several specialists "
    "each examined one aspect and returned notes. Merge them into one coherent "
    "answer in your own voice.\n"
    "Rules:\n"
    "- Answer the user's question directly. Lead with the answer, not a preamble.\n"
    "- Drop any note that is unsupported, off-topic or duplicated.\n"
    "- Never mention the specialists, their names, agents, or that notes exist. "
    "The user asked a question and expects an answer.\n"
    "- Use markdown where it helps. Be complete but do not pad."
)


def _history_messages(history: list[dict], limit: int = 8) -> list[dict]:
    """Recent turns only. A long transcript costs tokens on every turn and the
    older turns rarely change the answer."""
    out = []
    for m in history[-limit:]:
        role = m.get("role")
        content = str(m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content[:4000]})
    return out


async def stream_direct(
    question: str,
    model: str,
    *,
    history: list[dict] | None = None,
    temperature: float = 0.3,
    fallback_model: str | None = None,
) -> AsyncIterator[str]:
    """Fast path: straight to the model, tokens as they arrive.

    A provider failure (429, 5xx, a dropped connection) before any token has
    reached the client is retried once against `fallback_model`, transparently -
    the user sees an answer, not "the answer stream failed". Once even one
    token has been yielded, a failure can only be reported: silently
    restarting mid-answer would splice two partial answers together.
    """
    adapter, name = adapter_for(model)
    messages = [
        {"role": "system", "content": SYSTEM},
        *_history_messages(history or []),
        {"role": "user", "content": question},
    ]
    yielded = False
    try:
        async for piece in adapter.stream(messages, name, temperature=temperature):
            yielded = True
            yield piece
    except Exception as e:
        if yielded or not fallback_model:
            raise
        log.warning("answer model %s failed before any output (%s), falling back to %s",
                    model, e, fallback_model)
        fb_adapter, fb_name = adapter_for(fallback_model)
        async for piece in fb_adapter.stream(messages, fb_name, temperature=temperature):
            yield piece


def _format_contributions(contributions: list[dict]) -> str:
    """Flatten specialist output into something a model can read.

    Authorship is kept here on purpose - unlike peer ranking, the synthesizer is
    not judging the work, and knowing which aspect a note came from helps it
    organise the answer.
    """
    blocks = []
    for c in contributions:
        points = c.get("points") or []
        if not points:
            continue
        lines = []
        for p in points:
            if not isinstance(p, dict):
                continue
            claim = str(p.get("claim") or "").strip()
            support = str(p.get("support") or "").strip()
            if claim:
                lines.append(f"- {claim}" + (f"\n  ({support})" if support else ""))
        if lines:
            blocks.append(f"## Notes on {c.get('aspect', 'the question')}\n" + "\n".join(lines))
    return "\n\n".join(blocks)


async def stream_synthesis(
    question: str,
    contributions: list[dict],
    model: str,
    *,
    history: list[dict] | None = None,
    fallback_model: str | None = None,
) -> AsyncIterator[str]:
    """Council path: merge the specialists' notes into one answer.

    Falls back to answering directly when the specialists produced nothing
    usable, because an empty council is not a reason to give the user nothing.
    Also falls back to `fallback_model` on a provider failure before any token
    has been sent - see stream_direct's docstring for why the cutoff is there.
    """
    notes = _format_contributions(contributions)
    if not notes.strip():
        log.warning("no usable contributions, answering directly")
        async for piece in stream_direct(question, model, history=history,
                                          fallback_model=fallback_model):
            yield piece
        return

    adapter, name = adapter_for(model)
    messages = [
        {"role": "system", "content": SYNTHESIS_SYSTEM},
        *_history_messages(history or []),
        {"role": "user", "content": f"Question:\n{question}\n\nSpecialist notes:\n{notes}"},
    ]
    yielded = False
    try:
        async for piece in adapter.stream(messages, name, temperature=0.3):
            yielded = True
            yield piece
    except Exception as e:
        if yielded or not fallback_model:
            raise
        log.warning("synthesis model %s failed before any output (%s), falling back to %s",
                    model, e, fallback_model)
        fb_adapter, fb_name = adapter_for(fallback_model)
        async for piece in fb_adapter.stream(messages, fb_name, temperature=0.3):
            yield piece


async def contribute(
    question: str,
    aspect_id: str,
    role: str,
    model: str,
    *,
    seed: int | None = None,
    fallback_model: str | None = None,
) -> dict:
    """One specialist's pass. Returns a contribution dict, never raises.

    A failed branch degrades the answer; it must not kill the turn. The engine
    has compensation nodes for the same reason.
    """
    prompt = (
        f"You are examining one aspect of a user's question: {role}\n\n"
        f"The question is:\n{question}\n\n"
        "Return JSON only, no markdown fence:\n"
        '{"points":[{"claim":"...","support":"why this is true","confidence":"high"}]}\n'
        "Between 1 and 4 points. Cover only your aspect - other specialists "
        "cover theirs. If your aspect does not apply to this question, return "
        '{"points":[]} rather than inventing something.'
    )
    from backend.handoff import extract_json, validate

    messages = [{"role": "user", "content": prompt}]
    tokens = latency = 0
    cost = 0.0
    last_err = "no attempt made"

    # One repair pass. json_mode makes the first attempt almost always parse;
    # the retry is for the case where it parses but does not match the schema,
    # and it carries the validator's exact complaint back to the model.
    for attempt in range(2):
        try:
            c = await registry.complete(
                model, messages, temperature=0.2, seed=seed,
                max_tokens=900, json_mode=True, fallback_model=fallback_model,
            )
        except Exception as e:
            log.warning("specialist %s call failed: %s", aspect_id, e)
            return {"aspect": aspect_id, "points": [], "error": str(e)[:200]}

        tokens += c.tokens_in + c.tokens_out
        latency += c.latency_ms
        cost += c.cost_usd

        value, err = extract_json(c.text)
        if not err and isinstance(value, dict):
            result = validate(value, CONTRIBUTION_SCHEMA)
            if result.ok:
                return {
                    "aspect": aspect_id, "points": value.get("points") or [],
                    "model": c.model, "tokens": tokens,
                    "latency_ms": latency, "cost_usd": round(cost, 8),
                    "repaired": attempt > 0,
                    # Raw text and token split, so the caller can write a
                    # TOOL_RESULT row that replay can actually serve from.
                    "text": c.text, "tokens_in": c.tokens_in,
                    "tokens_out": c.tokens_out,
                    "used_fallback": c.used_fallback,
                }
            # An empty points list is a legitimate answer - the specialist is
            # saying its aspect does not apply - so accept it rather than
            # burning a repair on it.
            if isinstance(value.get("points"), list) and not value["points"]:
                return {"aspect": aspect_id, "points": [], "model": c.model,
                        "tokens": tokens, "latency_ms": latency,
                        "cost_usd": round(cost, 8), "not_applicable": True}
            last_err = result.error_text
        else:
            last_err = err or "not an object"

        if attempt == 0:
            messages = messages + [
                {"role": "assistant", "content": c.text[:2000]},
                {"role": "user", "content":
                    f"That did not validate: {last_err}\nReturn corrected JSON only."},
            ]

    log.warning("specialist %s did not validate: %s", aspect_id, last_err)
    return {"aspect": aspect_id, "points": [], "error": last_err[:200],
            "tokens": tokens, "latency_ms": latency}
