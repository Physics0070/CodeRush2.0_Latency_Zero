"""Block 7 verification: print real rows from both sources.

Run: python -m backend.benchmarks.show
"""

import json

from backend.benchmarks import bfcl, swebench


def main() -> None:
    print("=" * 74)
    print(f"SWE-bench Verified  ({swebench.DATASET})")
    print(f"licence: {swebench.LICENCE}")
    print("=" * 74)
    for t in swebench.fetch_rows(length=3):
        print(f"  instance_id : {t.instance_id}")
        print(f"  repo        : {t.repo}")
        print(f"  base_commit : {t.base_commit}")
        print(f"  pinned      : {t.pinned}")
        print(f"  FAIL_TO_PASS: {t.fail_to_pass[:2]}{' ...' if len(t.fail_to_pass) > 2 else ''}")
        print(f"  problem     : {t.problem_statement[:110].replace(chr(10), ' ')}...")
        print()

    print("=" * 74)
    print(f"BFCL  ({bfcl.REPO} @ {bfcl.REF})   licence: {bfcl.LICENCE}")
    print("NOTE: read as JSONL - load_dataset() does not work on this source.")
    print("=" * 74)
    for e in bfcl.fetch(limit=3):
        q = json.dumps(e.question)[:100]
        print(f"  id       : {e.id}")
        print(f"  tools    : {e.tool_names}")
        print(f"  question : {q}...")
        print()


if __name__ == "__main__":
    main()
