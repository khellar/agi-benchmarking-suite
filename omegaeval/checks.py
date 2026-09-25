"""Exact replay of a recorded episode, and the conformance test for environment adapters."""
from __future__ import annotations

import json
from typing import Callable

from .envs.base import Environment, Receipt

FRAME_TYPES = {"grid", "pixels", "tiles", "card", "timeline"}


def replay(env: Environment, manifest: dict, env_events: list[dict]) -> dict:
    """Re-run the environment from the recorded seed with the recorded actions and deliveries.

    Passes when every receipt and the final score match. That shows the environment is
    deterministic and the log complete. It does not re-run the agent.
    """
    ident = manifest["identity"]
    obs = env.reset(ident["task"], ident["seed"])
    problems = []
    first = next((e for e in env_events if e.get("kind") == "observation" and e.get("tick") == 0), None)
    if first is None:
        problems.append("no step-0 observation recorded")
    elif first.get("public") != json.loads(json.dumps(obs.public)):
        problems.append("step 0: observation differs from the recording")
    receipts = {e["tick"]: e for e in env_events if e.get("kind") == "receipt"}
    steps = 0
    for e in env_events:
        kind = e.get("kind")
        if kind == "delivered":
            got: Receipt = env.delivered()
            now = {"effect": got.effect, "done": got.done, "info": json.loads(json.dumps(got.info))}
            was = {"effect": e.get("effect"), "done": e.get("done"), "info": e.get("info")}
        elif kind == "action":
            got = env.step(e.get("action"))
            rec = receipts.get(e["tick"])
            if rec is None:
                problems.append(f"step {e['tick']}: no receipt recorded")
                continue
            now = {"effect": got.effect, "done": got.done, "info": json.loads(json.dumps(got.info))}
            was = {"effect": rec.get("effect"), "done": rec.get("done"), "info": rec.get("info")}
        else:
            continue
        steps += 1
        if now != was:
            problems.append(f"step {e['tick']}: recorded {was} but replay gives {now}")
    term = next((e for e in reversed(env_events) if e.get("kind") == "terminal"), None)
    score = env.score()
    if term and (term["score"].get("success") != score.get("success") or term["score"].get("steps") != score.get("steps")):
        problems.append(f"final score differs: recorded {term['score']} but replay gives {score}")
    return {"ok": not problems, "steps_replayed": steps, "problems": problems[:50]}


def conform(make: Callable[[], Environment], seeds: list[int], task: str | None = None) -> list[tuple[str, bool, str]]:
    """The adapter checks every benchmark must pass before its runs are admitted."""
    out: list[tuple[str, bool, str]] = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"raised {type(exc).__name__}: {exc}"
        out.append((name, ok, detail))

    desc: dict = {}

    def c_describe():
        nonlocal desc
        desc = make().describe()
        need = ["id", "version", "name", "kind", "tasks", "observation", "actions", "utility"]
        missing = [k for k in need if not desc.get(k)]
        util = desc.get("utility") or {}
        if not isinstance(util, dict) or not util.get("meaning") or util.get("direction") not in ("higher", "lower"):
            missing.append("utility.meaning and utility.direction (higher|lower)")
        if desc.get("kind") not in (None, "interactive", "qa", "memory"):
            missing.append("kind is interactive, qa or memory")
        if desc.get("renderer") not in (None, *FRAME_TYPES):
            missing.append(f"renderer is one of {sorted(FRAME_TYPES)} or none")
        return (not missing, "complete" if not missing else "missing: " + ", ".join(missing))

    check("describe() is complete", c_describe)
    tasks = desc.get("tasks") or ["all"]
    use_task = task or desc.get("default_task") or tasks[0]

    def reply_for(env, obs, i):
        if obs.actions:
            return obs.actions[i % len(obs.actions)]
        return "a probe answer"

    def script(env):
        obs = env.reset(use_task, seeds[0])
        trail = [(obs.text, json.dumps(obs.public, sort_keys=True), obs.expects_action)]
        for i in range(12):
            r = env.delivered() if not obs.expects_action else env.step(env.parse_action(reply_for(env, obs, i)))
            trail.append((r.effect, r.done, json.dumps(r.info, sort_keys=True, default=str)))
            obs = r.observation
            if r.done:
                break
        return trail

    check("reset, step and delivered are deterministic for a seed",
          lambda: (script(make()) == script(make()), "two fresh instances agree over up to 12 steps"))

    def c_seeds_differ():
        openings = set()
        for sd in seeds:
            e = make()
            o = e.reset(use_task, sd)
            openings.add(o.text + json.dumps(o.public, sort_keys=True) + json.dumps(e.solution(), default=str))
        return (len(openings) > 1 or len(seeds) < 2, f"{len(openings)} distinct episodes over {len(seeds)} seeds")

    check("different seeds give different episodes", c_seeds_differ)

    def reach_action(env):
        obs = env.reset(use_task, seeds[0])
        for _ in range(10_000):
            if obs.expects_action:
                return obs
            obs = env.delivered().observation
        raise RuntimeError("no observation ever asks for a reply")

    def c_invalid():
        env = make()
        reach_action(env)
        r = env.step(None)
        return (isinstance(r, Receipt) and r.valid is False, f"step(None) -> valid={getattr(r, 'valid', None)}")

    check("an unparseable reply is a typed failure, not an exception", c_invalid)

    def c_parse():
        env = make()
        obs = reach_action(env)
        if obs.free_text:
            got = env.parse_action("  Paris  ")
            return (got is not None and "Paris" in got, "free-text answers keep their content")
        bad = [a for a in obs.actions if env.parse_action(a) != a]
        return (not bad and bool(obs.actions), "every offered action parses to itself" if not bad else f"do not parse: {bad}")

    check("offered actions or free-text answers survive parse_action", c_parse)

    def c_refs():
        env = make()
        env.reset(use_task, seeds[0])
        fl, ce = env.floor(), env.ceiling()
        ok = fl is not None and (ce is None or ("utility" in ce))
        return (ok, f"floor={fl}, ceiling={ce}" + (" (no oracle declared)" if ce is None else ""))

    check("floor and ceiling references are declared", c_refs)

    def c_score():
        env = make()
        env.reset(use_task, seeds[0])
        s = env.score()
        missing = [k for k in ("success", "utility", "terminal", "steps") if k not in s]
        judged = s.get("needs_judge") and (s.get("success") is not None or s.get("utility") is not None)
        return (not missing and not judged,
                "keys present" if not missing and not judged else
                f"missing {missing}" if missing else "needs_judge must leave success and utility empty")

    check("score() reports success, utility, terminal and steps", c_score)

    def c_render():
        env = make()
        env.reset(use_task, seeds[0])
        fr = env.render()
        json.dumps(fr)
        if fr is not None and fr.get("type") not in FRAME_TYPES:
            return (False, f"unknown frame type {fr.get('type')!r}; the console knows {sorted(FRAME_TYPES)}")
        return (True, "no renderer: the console shows the transcript" if fr is None else f"frame type {fr['type']!r}")

    check("render() returns a known frame type or nothing", c_render)

    def c_privileged():
        env = make()
        obs = reach_action(env)
        sol = env.solution() or []
        fr = env.render() or {}
        leaks = [str(x) for x in sol[:1] if len(str(x)) > 3 and (str(x) in obs.text or str(x) in json.dumps(fr))]
        return (not leaks, "the solution does not appear in what the agent is shown" if not leaks
                else f"solution text visible to the agent: {leaks}")

    check("the agent's view does not contain the privileged solution", c_privileged)
    return out
