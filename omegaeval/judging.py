"""Verdicts for answers a benchmark held for a judge.

A verdict is evidence. It is appended to judgments.jsonl in the attempt with who or what
gave it, and the Result Record is recomputed from the files. Only an answer the benchmark
held for a judge can be judged, and an attempt frozen in a release is never changed. The
latest verdict decides; earlier ones stay in the file and are counted.

An external judge is any command that reads one case as JSON on stdin and prints one
verdict as JSON on stdout, for example a script that sends a benchmark's judge prompt to
a model:

    in   {"benchmark", "benchmark_version", "task", "seed", "run_id", "attempt",
          "question_id", "question", "gold", "answer", "answer_type", "pregrade", "grading"}
    out  {"verdict": "correct" | "wrong", "reason": "...", ...anything else is kept as detail}
"""
from __future__ import annotations

import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import result as result_mod
from .store import Store, read_jsonl

VERDICTS = ("correct", "wrong")
_ALIASES = {"correct": "correct", "yes": "correct", "true": "correct", "right": "correct",
            "wrong": "wrong", "no": "wrong", "false": "wrong", "incorrect": "wrong"}


def recompute(store: Store, cfg, path: Path) -> dict:
    """Recompute result.json from the attempt's files as they are now, and reseal it."""
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    agents = path / "agents"
    traces = {a.name: read_jsonl(a / "trace.jsonl") for a in sorted(agents.iterdir()) if a.is_dir()} \
        if agents.exists() else {}
    tap = (manifest.get("provenance") or {}).get("log_taps")
    res = result_mod.compute(manifest, read_jsonl(path / "env.jsonl"), traces, getattr(cfg, "prices", {}), tap,
                             read_jsonl(path / "judgments.jsonl"))
    store.write_json(path / "result.json", res)
    store.seal(path)
    return res


def case(store: Store, run_id: str, attempt: int | str, res: dict | None = None) -> dict:
    """What a judge needs: the question, the gold answer and the agent's answer."""
    p = store.path(run_id, attempt)
    manifest = json.loads((p / "manifest.json").read_text(encoding="utf-8"))
    if res is None:
        res = json.loads((p / "result.json").read_text(encoding="utf-8"))
    m = (res.get("outcome") or {}).get("metrics") or {}
    ident = manifest.get("identity") or {}
    return {"benchmark": ident.get("env"), "benchmark_version": ident.get("env_version"),
            "task": ident.get("task"), "seed": ident.get("seed"), "run_id": run_id, "attempt": int(attempt),
            "question_id": m.get("question_id") or m.get("case_id"), "question": m.get("question"),
            "gold": m.get("gold"), "answer": m.get("answer"),
            "answer_type": m.get("answer_type") or m.get("case_type"),
            "pregrade": m.get("pregrade"), "grading": m.get("grading")}


def held(store: Store, **filters) -> list[dict]:
    """Every finished attempt whose answer is waiting for a judge, oldest first."""
    out = []
    for rec in reversed(store.attempts(status="finished", needs_judge=True, **filters)):
        try:
            p = store.path(rec["run_id"], rec["attempt"])
            res = json.loads((p / "result.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        if (res.get("outcome") or {}).get("needs_judge"):
            out.append(case(store, rec["run_id"], rec["attempt"], res))
    return out


def record(store: Store, cfg, run_id: str, attempt: int | str, verdict: str, by: str, reason: str = "",
           via: str = "person", detail: dict | None = None) -> dict:
    verdict = _ALIASES.get(str(verdict).strip().lower(), "")
    if verdict not in VERDICTS:
        raise ValueError("a verdict is correct or wrong")
    if not by or not str(by).strip():
        raise ValueError("name the judge: a person, or the model and prompt behind a judge command")
    frozen = store.released_in(run_id, attempt)
    if frozen:
        raise PermissionError(f"frozen in release {', '.join(frozen)}; a released attempt is never changed")
    p = store.path(run_id, attempt)
    res = json.loads((p / "result.json").read_text(encoding="utf-8"))
    oc = res.get("outcome") or {}
    if not oc.get("needs_judge") and not oc.get("judged"):
        raise ValueError("the benchmark did not hold this answer for a judge" +
                         (" (the episode is void)" if oc.get("void") else ""))
    entry = {"t": datetime.now(timezone.utc).isoformat(), "by": str(by).strip(), "verdict": verdict,
             "reason": str(reason or "")[:4000], "via": via}
    if detail:
        entry["detail"] = detail
    store.append_event(p / "judgments.jsonl", entry)
    res = recompute(store, cfg, p)
    oc = res.get("outcome") or {}
    store.ledger({"event": "judged", "run_id": run_id, "attempt": int(attempt), "verdict": verdict,
                  "by": entry["by"], "via": via, "success": oc.get("success"),
                  "needs_judge": bool(oc.get("needs_judge")), "terminal": oc.get("terminal")})
    return res


def ask(command: str, payload: dict, timeout: float = 300) -> dict:
    """Run an external judge command on one case."""
    proc = subprocess.run(shlex.split(command), input=json.dumps(payload, ensure_ascii=False),
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"judge command exited with {proc.returncode}: {proc.stderr.strip()[-300:]}")
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    try:
        out = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        out = None
    if not isinstance(out, dict):
        raise RuntimeError(f"judge command printed no JSON verdict: {proc.stdout.strip()[-200:]!r}")
    verdict = _ALIASES.get(str(out.get("verdict", "")).strip().lower())
    if verdict is None:
        raise RuntimeError(f"judge command gave no verdict of correct or wrong: {out}")
    detail = {k: v for k, v in out.items() if k not in ("verdict", "reason")}
    return {"verdict": verdict, "reason": str(out.get("reason", "")), "detail": detail or None}
