"""DuckDuckGo web search - no API key, no cost, matches this project's ₹0 design.

Deliberately not wired as a live mid-conversation tool call: doing that
correctly means real function-calling across three providers plus a replay-safe
recorder/player pair for tool results (mirroring backend/replay/replay.py's
LoggedCompleter), which is real engine work. Instead this runs once, at
compile time (Orchestrator) or just before streaming starts (Chat), and the
results are folded into the static prompt text. That is inherently
replay-safe with zero extra code: whatever text a graph/message list was built
from is what gets stored and replayed, exactly like any other prompt content.
"""

import logging
import re

log = logging.getLogger("aco.websearch")

_WORD = re.compile(r"[A-Za-z0-9']+")

# Recency/current-events cues. A goal or question without any of these gets no
# search at all - most questions are timeless, and a network round trip is not
# free. Deliberately the same "cheap heuristic gate before paying for expensive
# machinery" pattern backend/chat/planner.py already uses for obviously_simple.
_FRESH_MARKERS = (
    "today", "latest", "current", "currently", "this year", "this week",
    "right now", "recent", "recently", "news", "price of", "stock",
    "who is the current", "score", "release date", "just released",
    "as of", "up to date", "up-to-date",
)
_YEAR = re.compile(r"\b20(2[5-9]|[3-9]\d)\b")


def needs_fresh_data(text: str) -> bool:
    """Cheap, free, no network - decides whether a search round trip is worth paying for."""
    low = f" {text.lower()} "
    return any(m in low for m in _FRESH_MARKERS) or bool(_YEAR.search(low))


async def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Bounded DuckDuckGo results as {"title","url","snippet"} rows.

    The ddgs client is sync, run off the event loop via asyncio.to_thread so it
    does not block the rest of a concurrent council/fanout.
    """
    import asyncio

    def _run() -> list[dict]:
        from ddgs import DDGS

        with DDGS() as ddgs:
            rows = ddgs.text(query[:400], max_results=max_results)
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("href") or r.get("url", ""),
                    "snippet": r.get("body", ""),
                }
                for r in rows
            ]

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        log.warning("web search failed for %r: %s", query[:80], e)
        return []


def render_results(results: list[dict]) -> str:
    if not results:
        return ""
    lines = ["Web search results (use only if relevant, cite the URL when you rely on one):"]
    for r in results:
        lines.append(f"- {r['title']} ({r['url']}): {r['snippet']}")
    return "\n".join(lines)
