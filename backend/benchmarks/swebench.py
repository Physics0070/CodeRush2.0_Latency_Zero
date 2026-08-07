"""SWE-bench Verified - 500 human-validated real GitHub issue/PR pairs.

Two access paths, both returning identical real rows:

- `fetch_rows` hits HuggingFace's datasets-server REST API. No download, works
  in a cold container, and is what the demo uses.
- `load_full` uses `datasets.load_dataset`, which pulls the whole split. Correct
  but slow, and pointless when we need three rows on stage.

Nothing here fabricates a record. If the API is unreachable it raises.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from pydantic import BaseModel

log = logging.getLogger("aco.benchmarks.swebench")

DATASET = "princeton-nlp/SWE-bench_Verified"
ROWS_API = "https://datasets-server.huggingface.co/rows"
# Licence per the dataset card; quoted in the README.
LICENCE = "CC BY 4.0 (see the SWE-bench Verified dataset card)"


class SweBenchTask(BaseModel):
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    # Tests that must flip from failing to passing. This is the ground truth.
    fail_to_pass: list[str] = []
    pass_to_pass: list[str] = []
    environment_setup_commit: str | None = None

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.repo}"

    @property
    def pinned(self) -> str:
        """A pinned SHA is a fixed input, which is reproducibility for free."""
        return f"{self.repo}@{self.base_commit[:12]}"


def _parse_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            return [str(v) for v in json.loads(value)]
        except json.JSONDecodeError:
            return [value] if value else []
    return []


def fetch_rows(offset: int = 0, length: int = 3, timeout: int = 60) -> list[SweBenchTask]:
    params = urllib.parse.urlencode({
        "dataset": DATASET, "config": "default", "split": "test",
        "offset": offset, "length": min(length, 100),
    })
    # noqa justified: the URL is built from module constants, never user input.
    req = urllib.request.Request(f"{ROWS_API}?{params}", headers={"User-Agent": "aco/0.1"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            data = json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"SWE-bench datasets-server unreachable: {e}") from e

    out = []
    for entry in data.get("rows", []):
        row = entry.get("row", {})
        out.append(SweBenchTask(
            instance_id=row.get("instance_id", ""),
            repo=row.get("repo", ""),
            base_commit=row.get("base_commit", ""),
            problem_statement=row.get("problem_statement", ""),
            fail_to_pass=_parse_list(row.get("FAIL_TO_PASS")),
            pass_to_pass=_parse_list(row.get("PASS_TO_PASS")),
            environment_setup_commit=row.get("environment_setup_commit"),
        ))
    return out


def load_full():
    """Full split via the datasets package. Slow; used for offline evaluation."""
    from datasets import load_dataset

    return load_dataset(DATASET, split="test")


def as_goal(task: SweBenchTask) -> str:
    """Turn a real issue into an orchestrator goal."""
    return (
        f"Analyse this issue from {task.repo} at commit {task.base_commit[:12]} and "
        f"produce a prioritized remediation report.\n\n{task.problem_statement[:1500]}"
    )
