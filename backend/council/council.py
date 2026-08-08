"""The council: N models propose, peer-review each other anonymously, chairman merges.

This is a node type inside the orchestrator, not the product. The same machinery
serves the graph compiler, the verification node and the conflict arbiter.

Anonymity is load-bearing. If a reviewer sees "proposal by llama3.2:3b" it rates
the label, not the content. Authorship is stripped before ranking and only
re-attached after the verdict, which is also what makes the ranking auditable.
"""

import asyncio
import json
import logging
import random

from pydantic import BaseModel, Field

from backend.events import EventStore, EventType
from backend.handoff import extract_json
from backend.providers import complete

log = logging.getLogger("aco.council")

# Members are independent, so proposing/ranking used to be a sequential loop of
# N model round trips - the same "await in a for-loop" latency bug the chat
# surface was fixed for. Now every member runs concurrently, each bounded so
# one slow member cannot stall the whole council (mirrors
# backend/chat/session.py's SPECIALIST_TIMEOUT_S).
COUNCIL_MEMBER_TIMEOUT_S = 45.0


class Proposal(BaseModel):
    author_model: str
    content: dict | list | str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    # Assigned at ranking time; reviewers only ever see this.
    label: str = ""


class Ranking(BaseModel):
    ranker_model: str
    # Ordered best-first, by anonymous label.
    order: list[str] = Field(default_factory=list)
    reasons: dict[str, str] = Field(default_factory=dict)


class Verdict(BaseModel):
    content: dict | list | str
    chairman: str
    borda: dict[str, float] = Field(default_factory=dict)
    winner_label: str = ""
    winner_author: str = ""
    disagreement: float = 0.0
    escalated: bool = False
    proposals: list[Proposal] = Field(default_factory=list)
    rankings: list[Ranking] = Field(default_factory=list)


PROPOSE_SYS = "You are one member of a design council. Output JSON only, no prose."
RANK_SYS = (
    "You are reviewing anonymous proposals from peers. You do not know who wrote any of "
    "them. Judge only on merit. Output JSON only."
)


async def _propose_one(
    model: str, task: str, schema_hint: str, *, store, run_id, seed
) -> Proposal | None:
    try:
        async with asyncio.timeout(COUNCIL_MEMBER_TIMEOUT_S):
            c = await complete(
                model,
                [
                    {"role": "system", "content": PROPOSE_SYS},
                    {"role": "user", "content": f"{task}\n\n{schema_hint}"},
                ],
                seed=seed, temperature=0.0, max_tokens=1200,
            )
    except TimeoutError:
        log.warning(
            "council member %s timed out after %ss proposing", model, COUNCIL_MEMBER_TIMEOUT_S
        )
        return None
    except Exception as e:
        log.warning("council member %s failed to propose: %s", model, e)
        return None

    value, err = extract_json(c.text)
    p = Proposal(
        author_model=model,
        content=value if not err else c.text,
        tokens_in=c.tokens_in, tokens_out=c.tokens_out,
        cost_usd=c.cost_usd, latency_ms=c.latency_ms,
    )
    if store and run_id:
        await store.append(
            run_id, EventType.COUNCIL_PROPOSAL, agent_id=model,
            payload={"model": model, "parsed": not err, "content": _clip(p.content)},
            tokens_in=c.tokens_in, tokens_out=c.tokens_out,
            cost_usd=c.cost_usd, latency_ms=c.latency_ms,
        )
    return p


async def propose(
    task: str, members: list[str], *, schema_hint: str = "", store=None, run_id=None, seed=42
) -> list[Proposal]:
    """Each member answers the same task independently, concurrently."""
    results = await asyncio.gather(*[
        _propose_one(model, task, schema_hint, store=store, run_id=run_id, seed=seed)
        for model in members
    ])
    return [p for p in results if p is not None]


async def _rank_one(
    model: str, ask: str, labels: list[str], *, store, run_id, seed
) -> Ranking | None:
    try:
        async with asyncio.timeout(COUNCIL_MEMBER_TIMEOUT_S):
            c = await complete(
                model,
                [{"role": "system", "content": RANK_SYS}, {"role": "user", "content": ask}],
                seed=seed, temperature=0.0, max_tokens=600,
            )
    except TimeoutError:
        log.warning(
            "council member %s timed out after %ss ranking", model, COUNCIL_MEMBER_TIMEOUT_S
        )
        return None
    except Exception as e:
        log.warning("council member %s failed to rank: %s", model, e)
        return None

    value, err = extract_json(c.text)
    order = [x for x in (value or {}).get("order", []) if x in labels] if not err else []
    # A member that returns garbage abstains rather than corrupting the tally.
    if not order:
        log.warning("council member %s produced no usable ranking", model)
        return None
    r = Ranking(
        ranker_model=model,
        order=order,
        reasons=(value or {}).get("reasons", {}) if isinstance(value, dict) else {},
    )
    if store and run_id:
        await store.append(
            run_id, EventType.COUNCIL_RANKING, agent_id=model,
            payload={"ranker": model, "order": order, "anonymous": True},
            tokens_in=c.tokens_in, tokens_out=c.tokens_out, cost_usd=c.cost_usd,
        )
    return r


async def peer_rank(
    proposals: list[Proposal], members: list[str], *, store=None, run_id=None, seed=42
) -> list[Ranking]:
    """Every member ranks all proposals, with authorship stripped, concurrently."""
    if len(proposals) < 2:
        return []

    labels = [chr(ord("A") + i) for i in range(len(proposals))]
    # Shuffle deterministically so label order does not track member order, but
    # the same seed reproduces the same assignment for replay.
    rng = random.Random(seed)  # noqa: S311
    shuffled = list(zip(labels, proposals, strict=True))
    rng.shuffle(shuffled)
    for label, p in shuffled:
        p.label = label

    anonymous = "\n\n".join(
        f"--- Proposal {p.label} ---\n{json.dumps(p.content, default=str)[:1800]}"
        for _, p in sorted(shuffled, key=lambda x: x[1].label)
    )
    ask = (
        f"{anonymous}\n\nRank every proposal best to worst. "
        f'Return {{"order":[{", ".join(f_(x) for x in labels)}],'
        f'"reasons":{{"A":"..."}}}} using only these labels: {labels}.'
    )

    results = await asyncio.gather(*[
        _rank_one(model, ask, labels, store=store, run_id=run_id, seed=seed)
        for model in members
    ])
    return [r for r in results if r is not None]


def f_(x: str) -> str:
    return f'"{x}"'


def borda(rankings: list[Ranking], labels: list[str]) -> dict[str, float]:
    """Borda count: position N from the top scores (len - N) points."""
    scores = dict.fromkeys(labels, 0.0)
    for r in rankings:
        n = len(r.order)
        for i, label in enumerate(r.order):
            if label in scores:
                scores[label] += n - i
    return scores


def disagreement(rankings: list[Ranking]) -> float:
    """0 = unanimous top pick, 1 = every ranker picked a different winner."""
    if len(rankings) < 2:
        return 0.0
    tops = [r.order[0] for r in rankings if r.order]
    if not tops:
        return 0.0
    return round(1 - (tops.count(max(set(tops), key=tops.count)) / len(tops)), 3)


def pick_chairman(policy: str, members: list[str]) -> str:
    """Chairman is a policy, not a constant: a cheap model for a low-stakes
    merge, the strongest available for verification."""
    if policy == "cheapest":
        return min(members, key=len)
    if policy in members:
        return policy
    remote = [m for m in members if not m.startswith("ollama:")]
    return remote[0] if remote else members[0]


async def chairman_merge(
    task: str,
    proposals: list[Proposal],
    rankings: list[Ranking],
    *,
    policy: str = "strongest",
    members: list[str] | None = None,
    store=None,
    run_id=None,
    seed: int = 42,
    escalate_above: float = 0.5,
) -> Verdict:
    members = members or [p.author_model for p in proposals]
    chairman = pick_chairman(policy, members)
    labels = [p.label or chr(ord("A") + i) for i, p in enumerate(proposals)]
    scores = borda(rankings, labels)
    disagree = disagreement(rankings)

    winner_label = max(scores, key=scores.get) if scores else labels[0]
    winner = next((p for p in proposals if p.label == winner_label), proposals[0])

    ranked_text = "\n\n".join(
        f"--- Proposal {p.label} (peer score {scores.get(p.label, 0)}) ---\n"
        f"{json.dumps(p.content, default=str)[:1500]}"
        for p in proposals
    )
    try:
        async with asyncio.timeout(COUNCIL_MEMBER_TIMEOUT_S):
            c = await complete(
                chairman,
                [
                    {"role": "system", "content":
                     "You are the council chairman. Merge the strongest elements of the "
                     "peer-ranked proposals into one final answer. Output JSON only."},
                    {"role": "user", "content": f"Task:\n{task}\n\n{ranked_text}"},
                ],
                seed=seed, temperature=0.0, max_tokens=1500,
            )
        merged, err = extract_json(c.text)
        content = merged if not err else winner.content
    except TimeoutError:
        log.warning(
            "chairman %s timed out after %ss, falling back to top-ranked proposal",
            chairman, COUNCIL_MEMBER_TIMEOUT_S,
        )
        content = winner.content
    except Exception as e:
        log.warning("chairman %s failed, falling back to top-ranked proposal: %s", chairman, e)
        content = winner.content

    verdict = Verdict(
        content=content,
        chairman=chairman,
        borda=scores,
        winner_label=winner_label,
        winner_author=winner.author_model,
        disagreement=disagree,
        escalated=disagree > escalate_above,
        proposals=proposals,
        rankings=rankings,
    )
    if store and run_id:
        await store.append(
            run_id, EventType.COUNCIL_VERDICT, agent_id=chairman,
            payload={
                "chairman": chairman,
                "borda": scores,
                "winner_label": winner_label,
                # Authorship re-attached only now, after ranking is complete.
                "winner_author": winner.author_model,
                "disagreement": disagree,
                "escalated": verdict.escalated,
                "content": _clip(content),
            },
        )
    return verdict


async def deliberate(
    task: str,
    members: list[str],
    *,
    schema_hint: str = "",
    policy: str = "strongest",
    store: EventStore | None = None,
    run_id: str | None = None,
    seed: int = 42,
) -> Verdict:
    """propose -> anonymous peer_rank -> chairman_merge."""
    proposals = await propose(
        task, members, schema_hint=schema_hint, store=store, run_id=run_id, seed=seed
    )
    if not proposals:
        raise RuntimeError("no council member produced a proposal")
    rankings = await peer_rank(proposals, members, store=store, run_id=run_id, seed=seed)
    return await chairman_merge(
        task, proposals, rankings, policy=policy, members=members,
        store=store, run_id=run_id, seed=seed,
    )


def _clip(value: object, limit: int = 4000) -> object:
    text = json.dumps(value, default=str)
    return value if len(text) <= limit else {"truncated": True, "preview": text[:limit]}
