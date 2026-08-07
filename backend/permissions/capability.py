"""Capability enforcement.

Every tool call goes through the guard. The agent's declared `tools` list is the
whole allow-list; anything else is blocked and surfaced, with the attempted tool
name and the agent id, in red in the trace viewer.

The check is orchestrator-side on purpose. Asking a model not to call a tool is
a prompt; refusing to execute it is a control.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from backend.events import EventStore, EventType

log = logging.getLogger("aco.permissions")

ToolFn = Callable[..., Awaitable[Any]]


class ToolBlocked(PermissionError):
    def __init__(self, agent_id: str, tool: str, declared: list[str]):
        super().__init__(
            f"{agent_id} attempted '{tool}' - outside declared capabilities {declared}"
        )
        self.agent_id = agent_id
        self.tool = tool
        self.declared = declared


class ToolResult(BaseModel):
    tool: str
    ok: bool
    output: Any = None
    blocked: bool = False
    error: str | None = None


class CapabilityGuard:
    """Holds the registry of real tools and enforces the per-agent allow-list."""

    def __init__(self, store: EventStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id
        self._registry: dict[str, ToolFn] = {}

    def register(self, name: str, fn: ToolFn) -> None:
        self._registry[name] = fn

    async def call(
        self,
        *,
        agent_id: str,
        declared_tools: list[str],
        tool: str,
        arguments: dict | None = None,
        node_id: str | None = None,
    ) -> ToolResult:
        arguments = arguments or {}

        if tool not in declared_tools:
            await self.store.append(
                self.run_id,
                EventType.TOOL_BLOCKED,
                node_id=node_id,
                agent_id=agent_id,
                payload={
                    "attempted_tool": tool,
                    "agent_id": agent_id,
                    "declared_tools": declared_tools,
                    "arguments": arguments,
                    "reason": "outside declared capabilities",
                },
            )
            log.warning("TOOL_BLOCKED %s attempted %s", agent_id, tool)
            return ToolResult(tool=tool, ok=False, blocked=True,
                              error=f"'{tool}' is outside declared capabilities")

        if tool not in self._registry:
            # Declared but not implemented. Still refused - a declaration is not
            # an implementation.
            await self.store.append(
                self.run_id, EventType.TOOL_BLOCKED, node_id=node_id, agent_id=agent_id,
                payload={"attempted_tool": tool, "agent_id": agent_id,
                         "reason": "declared but not registered"},
            )
            return ToolResult(tool=tool, ok=False, blocked=True, error="tool not registered")

        await self.store.append(
            self.run_id, EventType.TOOL_CALL, node_id=node_id, agent_id=agent_id,
            payload={"tool": tool, "arguments": arguments},
        )
        try:
            output = await self._registry[tool](**arguments)
        except Exception as e:
            await self.store.append(
                self.run_id, EventType.TOOL_RESULT, node_id=node_id, agent_id=agent_id,
                payload={"tool": tool, "ok": False, "error": str(e)},
            )
            return ToolResult(tool=tool, ok=False, error=str(e))

        # Recorded so replay serves this from the log instead of re-executing.
        await self.store.append(
            self.run_id, EventType.TOOL_RESULT, node_id=node_id, agent_id=agent_id,
            payload={"tool": tool, "ok": True, "output": output},
        )
        return ToolResult(tool=tool, ok=True, output=output)
