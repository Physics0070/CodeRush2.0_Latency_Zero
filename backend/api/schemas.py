"""Request models. Every endpoint validates and rejects; nothing sanitizes-and-continues."""

from pydantic import BaseModel, Field, field_validator

from backend.engine.spec import GraphSpec

MAX_GOAL = 2000


class GoalRequest(BaseModel):
    goal: str = Field(min_length=10, max_length=MAX_GOAL)
    models: list[str] = Field(default_factory=list, max_length=6)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    chairman_policy: str = Field(default="strongest", max_length=64)

    @field_validator("goal")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("goal must not be blank")
        return v.strip()

    @field_validator("models")
    @classmethod
    def _well_formed(cls, v: list[str]) -> list[str]:
        for m in v:
            if ":" not in m or len(m) > 128:
                raise ValueError(f"model must be 'provider:model', got {m!r}")
        return v


class CompileRequest(GoalRequest):
    answers: dict[str, str] = Field(default_factory=dict)
    # Set by the frontend from /api/clarify's already-computed Plan.intent, so
    # the graph is ranked/shaped for what was actually asked instead of always
    # "audit". None (an older client, or a direct API call) falls back to a
    # heuristic re-derived inside compile_graph - see backend/council/compiler.py.
    intent: str | None = Field(default=None, max_length=32)

    @field_validator("answers")
    @classmethod
    def _bounded(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 5:
            raise ValueError("at most 5 clarifying answers")
        for key, value in v.items():
            if len(key) > 200 or len(value) > 1000:
                raise ValueError("clarifying answer too long")
        return v


class ChatMessage(BaseModel):
    role: str = Field(max_length=16)
    content: str = Field(max_length=8000)

    @field_validator("role")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in ("user", "assistant"):
            raise ValueError("role must be 'user' or 'assistant'")
        return v


class ChatRequest(BaseModel):
    """A chat turn. Unlike GoalRequest the message may be short - "hi" is a
    legitimate thing to send an assistant, and the planner routes it cheaply."""

    message: str = Field(min_length=1, max_length=MAX_GOAL)
    history: list[ChatMessage] = Field(default_factory=list, max_length=40)
    models: list[str] = Field(default_factory=list, max_length=6)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message must not be blank")
        return v.strip()

    @field_validator("models")
    @classmethod
    def _well_formed(cls, v: list[str]) -> list[str]:
        for m in v:
            if ":" not in m or len(m) > 128:
                raise ValueError(f"model must be 'provider:model', got {m!r}")
        return v


class RunRequest(BaseModel):
    goal: str = Field(min_length=10, max_length=MAX_GOAL)
    graph: GraphSpec
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    approvals: list[str] = Field(default_factory=list, max_length=32)


class ApprovalRequest(BaseModel):
    node_id: str = Field(min_length=1, max_length=128)
    granted: bool = True


class MarginalValueRequest(BaseModel):
    goal: str = Field(min_length=10, max_length=MAX_GOAL)
    depths: list[int] = Field(default=[1, 2, 3, 4], max_length=4)
    models: list[str] = Field(default_factory=list, max_length=6)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)

    @field_validator("depths")
    @classmethod
    def _sane(cls, v: list[int]) -> list[int]:
        if any(d < 1 or d > 4 for d in v):
            raise ValueError("depth must be between 1 and 4")
        return v
