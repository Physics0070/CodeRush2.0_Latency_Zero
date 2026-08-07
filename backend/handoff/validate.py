"""Schema validation for every edge in the graph.

This is the execution contract. Agents never hand each other prose: a producer
emits a payload, it is validated against the edge's JSON Schema, and a wrong
shape is rejected with the exact validator message rather than being passed
downstream and misread.
"""

import json
import re

from jsonschema import Draft202012Validator
from pydantic import BaseModel

# Models habitually wrap JSON in a markdown fence or prepend a sentence. That is
# a formatting artefact, not a contract violation, so strip it before judging.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ValidationResult(BaseModel):
    ok: bool
    payload: dict | list | None = None
    errors: list[str] = []

    @property
    def error_text(self) -> str:
        """Exactly what gets handed back to the producing agent."""
        return "\n".join(f"- {e}" for e in self.errors)


def extract_json(raw: str | dict | list) -> tuple[dict | list | None, str | None]:
    """Best-effort parse. Returns (value, error)."""
    if isinstance(raw, dict | list):
        return raw, None

    text = (raw or "").strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    else:
        # Fall back to the outermost brace/bracket span.
        start = min(
            (i for i in (text.find("{"), text.find("[")) if i != -1),
            default=-1,
        )
        end = max(text.rfind("}"), text.rfind("]"))
        if start != -1 and end > start:
            text = text[start : end + 1]

    if not text:
        return None, "output was empty; expected a JSON object"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        return None, f"output is not valid JSON: {e.msg} (line {e.lineno}, column {e.colno})"


def validate(payload: str | dict | list, schema: dict) -> ValidationResult:
    """Validate against `schema`, collecting every error rather than the first.

    All errors at once matters: fixing one field per round trip would burn the
    repair budget on a payload with three missing keys.
    """
    value, parse_error = extract_json(payload)
    if parse_error:
        return ValidationResult(ok=False, errors=[parse_error])

    validator = Draft202012Validator(schema)
    errors = []
    for e in sorted(validator.iter_errors(value), key=lambda x: list(x.path)):
        location = "/".join(str(p) for p in e.path) or "(root)"
        errors.append(f"at `{location}`: {e.message}")

    if errors:
        return ValidationResult(ok=False, payload=value, errors=errors)
    return ValidationResult(ok=True, payload=value)
