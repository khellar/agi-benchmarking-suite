"""The Result Record, computed only from recorded evidence.

A block that nothing in the evidence supports is written as
{"available": false, "reason": "..."} and the console greys it out. Nothing is
estimated or filled with a default.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime

from . import __version__
from .omega_trace import PARSER_VERSION

ABSENT_IN_OMEGA = {
    "world_model": "OmegaClaw exposes no world-model probe; the agent's map lives only in its prompt and memory text",
    "concepts": ("OmegaClaw keeps no persistent concept store: each metta call starts a fresh AtomSpace "
                 "(docs/reference-internals-memory-store.md). Atoms per call are in cognition.atoms."),
}


def _absent(reason: str) -> dict:
    return {"available": False, "reason": reason}


def _ts(t: str | None) -> float | None:
    if not t:
        return None
    try:
        return datetime.fromisoformat(t).timestamp()
    except ValueError:
        return None


def agent_cognition(iterations: list[dict]) -> dict:
    llm_calls = sum(len(it.get("llm", [])) for it in iterations)
    usage = [u for it in iterations for u in it.get("llm", []) if u.get("usage_logged")]
    tokens = None
    if usage:
        tokens = {k: sum((u.get(k) or 0) for u in usage)
                  for k in ("prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens")}
        tokens["calls_with_usage"] = len(usage)
    skills: Counter = Counter()
    engines: Counter = Counter()
    memory: dict[str, Counter] = {}
    errors: Counter = Counter()
    premises = conclusions = 0
    sends = 0
    for it in iterations:
        for c in it.get("calls", []):
            skills[c.get("skill", "?")] += 1
            if c.get("skill") == "send":
                sends += 1
            if c.get("skill") == "metta" or c.get("lane") == "reasoning":
                engines[c.get("engine", "metta")] += 1
                premises += len(c.get("premises", []))
                conclusions += len(c.get("conclusions", []))
            if c.get("store"):
                memory.setdefault(c["store"], Counter())[c.get("op", "?")] += 1
        for e in it.get("errors", []):
            errors[e.get("type", "?")] += 1
    models = sorted({f"{u.get('provider')}/{u.get('model')}" for u in usage if u.get("model")})
    return {
        "iterations": len(iterations),
        "idle_iterations": sum(1 for it in iterations if it.get("idle")),
        "llm_calls": llm_calls,
        "tokens": tokens if tokens else _absent("no [LLM_USAGE] lines in the log slice"),
        "models": models,
        "skills": dict(skills),
        "reasoning": {"llm": llm_calls, **dict(engines)},
        "atoms": {"premises": premises, "conclusions": conclusions},
        "memory": {k: dict(v) for k, v in memory.items()},
        "errors": dict(errors),
        "sends": sends,
        "finish_reasons": dict(Counter(u.get("finish_reason") for u in usage if u.get("finish_reason"))),
    }


def _cost(cognition: dict, prices: dict) -> dict:
    if not prices:
        return _absent("no [prices] table in omegaeval.toml")
    tok = cognition.get("tokens")
    if not isinstance(tok, dict) or tok.get("available") is False:
        return _absent("no token usage logged")
    models = cognition.get("models", [])
    if len(models) != 1:
        return _absent(f"cost needs exactly one model in the episode, found {models}")
    model = models[0].split("/", 1)[-1]
    p = prices.get(model)
    if not p:
        return _absent(f"no price entry for model {model!r}")
    cached = tok.get("cached_tokens", 0)
    uncached = max(0, tok.get("prompt_tokens", 0) - cached)
    usd = (uncached * p.get("input", 0) + cached * p.get("cached_input", p.get("input", 0))
           + tok.get("completion_tokens", 0) * p.get("output", 0)) / 1e6
    return {"available": True, "usd": round(usd, 6), "model": model,
            "note": "completion tokens include reasoning tokens where the provider bills them together"}


def _tool_policy(desc: dict, traces: dict[str, list[dict]], tap_status: dict) -> dict:
    pol = desc.get("tool_policy") or {}
    forbid = sorted(set(pol.get("forbid") or []))
    allow = sorted(set(pol["allow"]) | {"send"}) if pol.get("allow") else None
    if not forbid and allow is None:
        return {"forbid": [], "allow": None, "violations": 0, "void": False}

    def broken(skill) -> bool:
        return skill in forbid or (allow is not None and skill not in allow)

    hits, unobserved = [], []
    for agent, iters in traces.items():
        st = (tap_status or {}).get(agent, {})
        if not st.get("configured", False) and not iters:
            unobserved.append(agent)
            continue
        for it in iters:
            for c in it.get("calls", []):
                if broken(c.get("skill")):
                    hits.append({"agent": agent, "loop_turn": it.get("i"), "skill": c.get("skill"),
                                 "args": " ".join(c.get("args") or [])[:200]})
    void = bool(hits) and bool(pol.get("void_on_violation", True))
    return {"forbid": forbid, "allow": allow, "violations": len(hits), "calls": hits[:20], "void": void,
            "not_observable_for": unobserved,
            "note": "checked against the skill calls in each agent's log; agents without a log tap cannot be checked"}


def _verdict(judgments: list[dict]) -> dict | None:
    """The latest verdict decides. Earlier ones stay in the evidence and are counted."""
    valid = [j for j in judgments or [] if j.get("verdict") in ("correct", "wrong")]
    if not valid:
        return None
    last = valid[-1]
    return {"verdict": last["verdict"], "by": last.get("by"), "via": last.get("via"), "at": last.get("t"),
            "reason": last.get("reason", ""), "judgments": len(valid),
            "unanimous": len({j["verdict"] for j in valid}) == 1}


def _concepts(env_events: list[dict], members: dict) -> dict:
    frames = [e for e in env_events if e.get("kind") == "agent_trace" and (e.get("trace") or {}).get("kind") == "concepts"]
    if not frames:
        if any((m or {}).get("kind") == "omega" for m in members.values()):
            return _absent(ABSENT_IN_OMEGA["concepts"])
        return _absent("no agent reported concepts over the gateway")
    seen: dict[str, int] = {}
    by_agent: dict[str, int] = {}
    for e in frames:
        by_agent[e.get("agent", "?")] = by_agent.get(e.get("agent", "?"), 0) + 1
        for c in (e["trace"].get("concepts") or []):
            cid = str(c.get("id", ""))
            if cid:
                seen[cid] = seen.get(cid, 0) + 1
    return {"available": True, "source": "concept frames the agent reported over the gateway",
            "frames": len(frames), "distinct": len(seen), "reidentified": sum(1 for v in seen.values() if v > 1),
            "by_agent": by_agent,
            "note": "counts only; scoring against hidden entities needs the environment's entity truth"}


NOT_GRADED = {"error": "the harness failed during the episode",
              "interrupted": "the harness was stopped during the episode"}


def compute(manifest: dict, env_events: list[dict], traces: dict[str, list[dict]], prices: dict | None = None,
            tap_status: dict | None = None, judgments: list[dict] | None = None) -> dict:
    desc = manifest.get("environment") or {}
    start = next((e for e in env_events if e.get("kind") == "episode_start"), None)
    term = next((e for e in reversed(env_events) if e.get("kind") == "terminal"), None)
    actions = [e for e in env_events if e.get("kind") == "action"]
    msgs = [e for e in env_events if e.get("kind") == "agent_message"]
    out: dict = {"schema": "omegaeval.result/0", "harness": f"omegaeval {__version__}", "parser": PARSER_VERSION,
                 "run_id": manifest.get("run_id"), "attempt": manifest.get("attempt")}
    policy = _tool_policy(desc, traces, tap_status or {})
    out["tool_policy"] = policy

    if term:
        s = term["score"]
        floor, ceil = term.get("floor"), term.get("ceiling") or {}
        cu = ceil.get("utility")
        util = s.get("utility")
        norm = None
        if util is not None and floor is not None and cu is not None and cu != floor:
            norm = max(0.0, min(1.0, (util - floor) / (cu - floor)))
        success = s.get("success")
        outcome = {"success": success, "utility": util, "terminal": term["status"], "floor": floor, "ceiling": cu,
                   "normalized": norm, "metrics": s.get("metrics", {})}
        if term["status"] in NOT_GRADED:              # the harness's fault, not the agent's: counted, never graded
            success = None
            outcome.update(success=None, utility=None, normalized=None,
                           not_graded=NOT_GRADED[term["status"]] + (f": {term['error']}" if term.get("error") else ""))
        elif s.get("needs_judge"):
            v = _verdict(judgments)
            if v:
                ok = v["verdict"] == "correct"
                util = 1.0 if ok else 0.0
                norm = (max(0.0, min(1.0, (util - floor) / (cu - floor)))
                        if floor is not None and cu is not None and cu != floor else None)
                outcome.update(success=ok, utility=util, normalized=norm, terminal="judged", judged=v)
                success = ok
            else:
                outcome.update(needs_judge=True, success=None,
                               judge_reason=s.get("metrics", {}).get("grading", "only a judge can grade this answer"))
        if policy.get("void"):
            for k in ("needs_judge", "judge_reason", "judged"):
                outcome.pop(k, None)                  # a void episode is not graded at all
            outcome.update(success_raw=success, success=False, void=True,
                           void_reason="used " + ", ".join(sorted({str(h["skill"]) for h in policy["calls"]}))
                           + ", which this benchmark's tool rules do not allow")
        out["outcome"] = outcome
        steps = s.get("steps")
        wall = ((_ts(term.get("t")) or 0) - (_ts(start.get("t")) or 0)) if start else None
        out["efficiency"] = {"steps": steps, "max_steps": term.get("max_steps"),
                             "invalid_actions": s.get("metrics", {}).get("invalid_actions"),
                             "wall_seconds": round(wall, 3) if wall is not None else None,
                             "agent_messages": len(msgs), "accepted_actions": len(actions),
                             "messages_delivered": sum(1 for e in env_events if e.get("kind") == "delivered")}
        cs = ceil.get("steps")
        solved = outcome.get("success") is True
        out["regret"] = ({"oracle_steps": cs, "extra_steps": (steps - cs) if solved and steps is not None else None,
                          "note": "extra steps are reported only for solved episodes"}
                         if cs is not None else _absent("the environment supplies no oracle"))
    else:
        out["outcome"] = _absent("episode has not finished")

    preds = [a for a in actions if a.get("prediction") is not None]
    succ = (out.get("outcome") or {}).get("success")
    if preds and term and succ is not None:
        y = 1.0 if succ else 0.0
        brier = sum((a["prediction"] - y) ** 2 for a in preds) / len(preds)
        out["prediction"] = {"n": len(preds), "brier": round(brier, 4),
                             "mean_p": round(sum(a["prediction"] for a in preds) / len(preds), 4),
                             "final_p": preds[-1]["prediction"], "observed": y}
    elif preds:
        out["prediction"] = _absent("predictions were stated, but the outcome awaits a judge")
    else:
        out["prediction"] = _absent("OmegaClaw commits no predictions. Enable [harness.episodes] predictions = true "
                                    "to ask the agent for p=<0..1> with each action.")

    per_agent = {}
    for agent, iters in traces.items():
        st = (tap_status or {}).get(agent, {})
        if not st.get("configured", True):
            per_agent[agent] = _absent(st.get("error") or "no log source configured for this agent")
        elif st.get("error") and not iters:
            per_agent[agent] = _absent(f"log tap failed: {st['error']}")
        else:
            per_agent[agent] = agent_cognition(iters)
    out["cognition"] = per_agent if per_agent else _absent("no agent traces")

    omega_agents = [c for c in per_agent.values() if c.get("available", True)]
    out["cost"] = _cost(omega_agents[0], prices or {}) if len(omega_agents) == 1 else (
        _absent("cost is computed per agent; this subject has several agents") if omega_agents
        else _absent("no agent traces"))
    out["world_model"] = _absent(ABSENT_IN_OMEGA["world_model"])
    out["concepts"] = _concepts(env_events, manifest.get("members") or {})
    return out
