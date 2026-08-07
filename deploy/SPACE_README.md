---
title: Agent Council Orchestrator
emoji: ⌘
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# Agent Council Orchestrator

Ask it anything and it answers. Then it shows you what the answer cost.

Simple questions go to one model and stream back in about two seconds. Harder
ones are split across specialists — named from your question, not a fixed list —
that run in parallel and are merged into one reply. Under every answer is the
measured truth about how it was produced: route taken, timings, tokens, cost,
and the semantic overlap between specialists.

A second surface takes a goal, asks clarifying questions, asks permission,
designs an agent team as a graph, executes it parallel-first, and replays any
past run with a provably identical result.

**CodeRush 2.0 · problem statement AE-03 · Team Latency Zero**

Source: https://github.com/Physics0070/CodeRush2.0_Latency_Zero

## Notes on this deployment

- **Ollama is not available on a Space.** There is no way to run `ollama serve`
  plus multi-GB model files on the free tier, so this instance answers with
  Groq. The model string is per agent, so nothing else changes.
- **Semantic metrics are off.** `torch` is not installed in the default image
  (~1GB, and `download.pytorch.org` stalls on many builders). The metrics
  endpoint reports `embeddings_available: false` and omits marginal information
  gain and duplicate pairs rather than guessing them. Everything else is live.
- **Cold start.** The first request after idle wakes the container.
