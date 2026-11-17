"""Search quality eval (SDD 15.2): recall@3 / recall@10 over tests/search_eval.json.

Live mode, against a running backend that has indexed the fixture workspace:

    uv run python -m tests.search_eval --base http://127.0.0.1:8765 --token dev

`evaluate()` is also used in-process by tests/test_pipeline.py.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable

EVAL_FILE = Path(__file__).resolve().parent / "search_eval.json"


def load_cases() -> list[dict]:
    return json.loads(EVAL_FILE.read_text(encoding="utf-8"))["cases"]


def evaluate(search: Callable[[str], list[str]], verbose: bool = False) -> dict:
    """`search(query) -> ranked rel_paths`. Returns recall@3, recall@10 and the misses."""
    cases = load_cases()
    hit3 = hit10 = 0
    misses = []
    for c in cases:
        ranked = search(c["q"])
        rank = ranked.index(c["expect"]) + 1 if c["expect"] in ranked else None
        if rank is not None and rank <= 3:
            hit3 += 1
        if rank is not None and rank <= 10:
            hit10 += 1
        else:
            misses.append({"q": c["q"], "expect": c["expect"], "type": c.get("type"), "got": ranked[:3]})
        if verbose:
            print(f"{'OK ' if rank and rank <= 3 else ('~  ' if rank else 'MISS')} rank={rank!s:4} {c['q']!r:45} -> {c['expect']}")
    n = len(cases)
    return {"n": n, "recall@3": round(hit3 / n, 3), "recall@10": round(hit10 / n, 3), "misses": misses}


def main() -> None:
    import httpx

    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--token", default="dev")
    args = ap.parse_args()
    client = httpx.Client(base_url=f"{args.base}/api/v1", headers={"Authorization": f"Bearer {args.token}"}, timeout=30)

    def search(q: str) -> list[str]:
        r = client.post("/search", json={"query": q, "limit": 10})
        r.raise_for_status()
        return [x["rel_path"] for x in r.json()["results"]]

    res = evaluate(search, verbose=True)
    print(json.dumps({k: v for k, v in res.items() if k != "misses"}))


if __name__ == "__main__":
    main()
