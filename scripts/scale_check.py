"""Build a synthetic store of N attempts from real reference runs, then time what the console asks for.

    python scripts/scale_check.py 20000

It plays a few real baseline episodes, copies them N times under new run ids with matching ledger
lines, and measures the query index: building it, a page of runs, a count, one benchmark's results,
the progress summary, and reopening. Nothing outside a temporary folder is touched.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from omegaeval.cli import main as cli
from omegaeval.store import Store


def timed(fn):
    t = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t


def main(n: int) -> None:
    root = Path(tempfile.mkdtemp(prefix="omegaeval-scale-"))
    here = os.getcwd()
    try:
        os.chdir(root)
        for policy in ("random", "oracle"):
            cli(["baseline", policy, "qa-sample", "--seeds", "1-3"])
            cli(["baseline", policy, "keydoor", "--task", "small", "--seeds", "1-2"])
        seed = Store(root / "omegaeval-store")
        rows = seed.attempts()
        big = root / "big"
        (big / "runs").mkdir(parents=True)
        t0 = time.perf_counter()
        with open(big / "index.jsonl", "w") as ledger:
            for i in range(n):
                r = rows[i % len(rows)]
                rid = f"{r['env']}-{i:012x}"
                shutil.copytree(seed.path(r["run_id"], r["attempt"]), big / "runs" / rid / "1")
                started = {k: r[k] for k in ("env", "task", "subject", "label", "variant", "started_at") if r.get(k)}
                ledger.write(json.dumps({"event": "started", "run_id": rid, "attempt": 1, "seed": i,
                                         "status": "running", "path": f"runs/{rid}/1", **started}) + "\n")
                ledger.write(json.dumps({"event": "finished", "run_id": rid, "attempt": 1, "status": "finished",
                                         "terminal": r.get("terminal"), "success": r.get("success"),
                                         "steps": r.get("steps")}) + "\n")
        built = time.perf_counter() - t0
        store = Store(big)
        events, t_index = timed(store.sync)
        page, t_page = timed(lambda: store.attempts(env="qa-sample", limit=500))
        total, t_count = timed(lambda: store.count(env="qa-sample"))
        results, t_results = timed(lambda: store.results(env="qa-sample"))
        progress, t_progress = timed(store.progress)
        _, t_reopen = timed(lambda: Store(big).attempts(limit=500))
        print(f"{n} attempts copied in {built:.1f} s\n")
        print(f"{'query':<34} {'time':>10} {'size':>12}")
        for name, secs, obj in (("build the index", t_index, f"{events} events"),
                                ("page of 500 runs", t_page, page), ("count one benchmark", t_count, total),
                                ("results of one benchmark", t_results, results), ("progress summary", t_progress, progress),
                                ("reopen and read a page", t_reopen, None)):
            size = obj if isinstance(obj, str) else f"{obj} runs" if isinstance(obj, int) else \
                f"{len(json.dumps(obj)) / 1e3:.0f} KB" if obj is not None else ""
            print(f"{name:<34} {secs * 1000:>8.0f} ms {size:>12}")
    finally:
        os.chdir(here)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20000)
