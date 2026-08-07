"""Plan for the question that was actually asked.

The clarifying questions used to be a fixed list of three, and when the council
did not return usable branches the graph fell back to security/quality/docs. So
"explain how RAG works" was answered by a security audit that asked which
severity floor to report. The plan now comes from the question.

The second job here is routing. A council of four costs four model calls plus a
merge, and most messages do not earn that. Spending it on "what does TTL stand
for" makes the answer slower without making it better, so `complexity` decides
how much machinery runs, and `simple` skips the council entirely.
"""

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from backend.handoff import extract_json
from backend.providers import registry

log = logging.getLogger("aco.planner")

Intent = Literal["chat", "explain", "analyze", "compare", "audit", "build"]
Complexity = Literal["simple", "moderate", "deep"]

# Specialist counts per tier. `simple` never reaches the council.
BRANCHES_FOR: dict[str, int] = {"simple": 0, "moderate": 2, "deep": 4}

_WORD = re.compile(r"[A-Za-z0-9']+")

# Phrases that ask for breadth explicitly. Presence lifts a question to `deep`
# even when it is short - "review my auth flow" is five words and earns four
# specialists.
_DEEP_MARKERS = (
    "audit", "review", "critique", "assess", "evaluate", "trade-off", "tradeoff",
    "architect", "design a", "strategy", "in depth", "in-depth", "comprehensive",
    "pros and cons", "security of", "analyse", "analyze", "deep dive",
)
_MODERATE_MARKERS = (
    "compare", "versus", " vs ", "should i", "which is better", "difference between",
    "how do i choose", "recommend", "best way", "why would", "when should",
)


class Specialist(BaseModel):
    """One parallel branch, named for what it examines in THIS question."""

    id: str
    role: str

    @field_validator("id")
    @classmethod
    def _identifier(cls, v: str) -> str:
        v = re.sub(r"[^a-z0-9_]", "_", v.strip().lower()).strip("_")
        if not v or not v.isidentifier():
            raise ValueError(f"unusable specialist id {v!r}")
        return v[:32]


class Plan(BaseModel):
    intent: Intent = "explain"
    complexity: Complexity = "moderate"
    rationale: str = ""
    clarifying_questions: list[str] = Field(default_factory=list, max_length=2)
    specialists: list[Specialist] = Field(default_factory=list, max_length=4)
    # "model" when a planner call succeeded, "heuristic" when we fell back.
    source: str = "model"

    @property
    def needs_council(self) -> bool:
        """A council needs at least two distinct viewpoints to be a council."""
        return self.complexity != "simple" and len(self.specialists) >= 2

    def branch_dicts(self) -> list[dict]:
        return [{"id": s.id, "role": s.role, "tools": []} for s in self.specialists]


PLANNER_PROMPT = """You route questions in a multi-agent answering system. Read \
the user's message and decide how much machinery it deserves. Answer quality \
matters, but so does not wasting four model calls on a one-line question.

Return JSON only, exactly this shape:
{"intent":"chat|explain|analyze|compare|audit|build",
 "complexity":"simple|moderate|deep",
 "rationale":"one short sentence",
 "clarifying_questions":[],
 "specialists":[{"id":"lowercase_word","role":"what this one examines"}]}

complexity - be honest, this controls cost:
- "simple": one competent model answers well in a single pass. Greetings, \
definitions, factual lookups, arithmetic, short code snippets, rewording, \
follow-ups to something already answered. MOST messages are simple. For simple, \
"specialists" MUST be [] and "clarifying_questions" MUST be [].
- "moderate": genuinely improved by 2 independent viewpoints. Comparisons, \
"should I" questions, design choices, questions with two distinct parts.
- "deep": open-ended work with several separable dimensions, or the user \
explicitly asked for a review, audit, critique or deep analysis. 3-4 specialists.

specialists: derive them FROM THIS MESSAGE. Each id names what it examines in \
this specific question. Never emit generic "security"/"quality"/"docs" unless \
the user is actually asking for a code audit. They must be independent so they \
can run in parallel.

clarifying_questions: at most 2, and only where the answer would materially \
change depending on the reply. Ask about this question's real ambiguity. If \
nothing is genuinely ambiguous, return [] - a filler question is worse than no \
question.

User message:
---
{question}
---"""


def heuristic_plan(question: str) -> Plan:
    """Deterministic fallback. Runs when no planner model is reachable.

    Also the floor under a bad planner response: it is better to answer with a
    reasonable shape than to fail because a 3B model emitted broken JSON.
    """
    q = question.strip()
    low = f" {q.lower()} "
    words = len(_WORD.findall(q))

    if any(m in low for m in _DEEP_MARKERS):
        complexity: Complexity = "deep"
        intent: Intent = "audit" if "audit" in low or "review" in low else "analyze"
    elif any(m in low for m in _MODERATE_MARKERS) or words > 40:
        complexity, intent = "moderate", "compare"
    else:
        complexity, intent = "simple", "chat" if words <= 4 else "explain"

    n = BRANCHES_FOR[complexity]
    # Generic names, deliberately: with no model available we cannot know what
    # this question's real dimensions are, and inventing them would be a lie.
    generic = [
        ("core", "Answer the central question directly and correctly."),
        ("context", "Supply background, prerequisites and definitions."),
        ("tradeoffs", "Give limitations, trade-offs and failure modes."),
        ("practical", "Give concrete steps, examples or code."),
    ][:n]
    return Plan(
        intent=intent,
        complexity=complexity,
        rationale=f"Heuristic route: {words} words, no planner model available.",
        clarifying_questions=[],
        specialists=[Specialist(id=i, role=r) for i, r in generic],
        source="heuristic",
    )


def _coerce(raw: dict, question: str) -> Plan:
    """Trust the model's judgement, enforce the invariants ourselves."""
    plan = Plan(
        intent=raw.get("intent") if raw.get("intent") in Intent.__args__ else "explain",
        complexity=(
            raw.get("complexity") if raw.get("complexity") in Complexity.__args__ else "moderate"
        ),
        rationale=str(raw.get("rationale") or "")[:300],
        clarifying_questions=[
            str(q).strip() for q in (raw.get("clarifying_questions") or [])[:2] if str(q).strip()
        ],
        specialists=[],
    )

    seen: set[str] = set()
    for s in (raw.get("specialists") or [])[:4]:
        if not isinstance(s, dict):
            continue
        try:
            spec = Specialist(id=str(s.get("id", "")), role=str(s.get("role") or "").strip())
        except ValueError:
            continue
        if spec.id in seen or not spec.role:
            continue
        seen.add(spec.id)
        plan.specialists.append(spec)

    # A model that says "deep" but names one specialist has contradicted itself.
    # Believe the specialist list, because that is what actually executes.
    if plan.complexity == "simple":
        plan.specialists, plan.clarifying_questions = [], []
    elif len(plan.specialists) < 2:
        fallback = heuristic_plan(question)
        if fallback.complexity == "simple":
            plan.complexity, plan.specialists = "simple", []
            plan.clarifying_questions = []
        else:
            plan.specialists = fallback.specialists[: BRANCHES_FOR[plan.complexity]]
    return plan


def obviously_simple(question: str) -> bool:
    """True when planning would cost more than it could possibly save.

    A planner call is a full model round trip. On a local 3B model that is ~18
    seconds, which is longer than answering "hi" takes - so the cheap questions
    were paying the largest relative tax. Short text with none of the breadth
    markers cannot be routed anywhere except the fast path, so we skip the call
    and route it directly.
    """
    q = question.strip()
    low = f" {q.lower()} "
    if any(m in low for m in _DEEP_MARKERS) or any(m in low for m in _MODERATE_MARKERS):
        return False
    return len(_WORD.findall(q)) <= 8 and q.count("?") <= 1


def pick_planner_model(models: list[str]) -> str | None:
    """Plan on the fastest model available, not the best one.

    Routing is a classification, not the answer - a small remote model does it
    as well as a large local one and returns in a fraction of the time.
    """
    if not models:
        return None
    for prefix in ("openrouter:", "groq:", "gemini:"):
        for m in models:
            if m.startswith(prefix):
                return m
    return models[0]


async def plan_for(question: str, model: str | None) -> Plan:
    """Ask a model how to answer this. Never raises - planning must not be the
    thing that breaks a chat turn."""
    if obviously_simple(question):
        p = heuristic_plan(question)
        p.complexity, p.specialists, p.clarifying_questions = "simple", [], []
        p.source = "short-circuit"
        p.rationale = "Short and unambiguous; routed without a planner call."
        return p
    if not model:
        return heuristic_plan(question)
    try:
        c = await registry.complete(
            model,
            [{"role": "user", "content": PLANNER_PROMPT.replace("{question}", question[:4000])}],
            temperature=0.0,
            max_tokens=400,
        )
        raw, err = extract_json(c.text)
        if err or not isinstance(raw, dict):
            log.warning("planner returned unusable JSON (%s), falling back", err)
            return heuristic_plan(question)
        return _coerce(raw, question)
    except Exception as e:
        log.warning("planner call failed (%s), falling back", e)
        return heuristic_plan(question)
