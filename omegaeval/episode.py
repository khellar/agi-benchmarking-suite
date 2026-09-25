"""Run one episode of one benchmark against one agent or one hive.

The runner owns the benchmark, the seed, the budgets and the record. It talks to agents only
through connectors (connectors.py): a chat channel for OmegaClaw, or request and response for
agents reached over HTTP, a subprocess or a Python class. Every message sent and every reply
received is appended to the episode's event log, the source of truth for replay and scoring.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__, result as result_mod
from .config import Config
from .connectors import CHUNK_BYTES, Connector, Message, chunks, connector_for, reported_iterations, \
    strip_controls, transport_of
from .gateway import Gateway
from .omega_trace import PARSER_VERSION
from .sources import LogTap, evidence_grade, memory_fingerprint
from .store import Store, compress_log, read_jsonl, run_hash

log = logging.getLogger("omegaeval.episode")

__all__ = ["EpisodeRunner", "CHUNK_BYTES", "chunks", "strip_controls", "now"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


class EpisodeRunner:
    def __init__(self, cfg: Config, store: Store, gateway: Gateway):
        self.cfg, self.store, self.gateway = cfg, store, gateway
        self.active: dict[str, str] = {}          # agent id -> "run_id/attempt"
        self._lock = threading.Lock()
        self.hooks: dict[str, Callable] = {}      # agent id -> prepare(env) for in-process reference agents
        self.connectors: dict[str, Connector] = {}

    def connector(self, spec) -> Connector:
        """The connector for an agent, made on first use and kept, so connection state survives episodes.
        It is made again when the agent's settings change, such as its URL or token."""
        c = self.connectors.get(spec.id)
        link = self.gateway.links.get(spec.id)
        if c is None or (transport_of(spec) != c.transport) or (getattr(c, "link", link) is not link) \
                or getattr(c, "spec", spec) != spec:
            c = connector_for(spec, self.gateway, self.cfg.predictions, self.cfg.step_timeout_s)
            self.connectors[spec.id] = c
        return c

    def run(self, subject_name: str, env_id: str, task: str | None, seed: int, *, variant: str = "",
            memory_mode: str = "carry", note: str = "") -> dict:
        cfg = self.cfg
        subject = cfg.subject(subject_name)
        env = cfg.make_env(env_id)
        task = task or cfg.default_task(env)
        obs = env.reset(task, seed)
        desc = env.describe()
        max_steps = env.max_steps()
        with self._lock:
            clash = [m.id for m in subject.members if m.id in self.active]
            if clash:
                raise RuntimeError(f"agents already in an episode: {clash}")
            for m in subject.members:
                self.active[m.id] = "starting"

        identity = {
            "harness": __version__, "env": env_id, "env_version": getattr(env, "version", "?"),
            "env_options": {k: v for k, v in sorted((getattr(env, "options", None) or {}).items())
                            if k not in ("path", "name")},
            "env_data": env.fingerprint() if hasattr(env, "fingerprint") else None, "task": task,
            "seed": seed, "subject": subject.name, "subject_kind": subject.kind,
            "members": [m.id for m in subject.members], "actor": subject.actor,
            "label": subject.label, "variant": variant, "memory_mode": memory_mode,
            "budgets": {"max_steps": max_steps, "step_timeout_s": cfg.step_timeout_s, "wall_s": cfg.wall_s,
                        "max_llm_calls": cfg.max_llm_calls},
            "protocol": {"require_tick": cfg.require_tick, "predictions": cfg.predictions, "reply": "ep t action"},
        }
        run_id, attempt, path = self.store.new_attempt(identity)
        ep_tag = f"{run_hash(run_id)[:6]}{attempt}"
        env_log = path / "env.jsonl"
        manifest = {
            "schema": "omegaeval.manifest/0", "run_id": run_id, "attempt": attempt, "status": "running",
            "identity": identity, "note": note, "started_at": now(), "ended_at": None, "ep_tag": ep_tag,
            "environment": desc, "benchmark": self.cfg.display(env_id, desc),
            "members": {m.id: {"kind": m.kind, "label": m.label, "config": m.config, "logs": m.logs,
                               "memory": m.memory, "transport": transport_of(m), "evidence": self._evidence(m)}
                        for m in subject.members},
            "harness": {"version": __version__, "parser": PARSER_VERSION, "gateway_port": self.gateway.port,
                        "owner": self.store.owner()},
        }
        self.store.write_json(path / "manifest.json", manifest)
        self.store.ledger({"event": "started", "run_id": run_id, "attempt": attempt, "env": env_id, "task": task,
                           "seed": seed, "subject": subject.name, "label": subject.label, "variant": variant,
                           "memory_mode": memory_mode, "note": note, "harness": __version__,
                           "started_at": manifest["started_at"], "status": "running",
                           "path": str(path.relative_to(self.store.root))})
        with self._lock:
            for m in subject.members:
                self.active[m.id] = f"{run_id}/{attempt}"

        conns: dict[str, Connector] = {}
        taps: dict[str, LogTap] = {}
        fingerprints_start = {}
        status, extra = "error", {}
        try:
            for m in subject.members:
                conns[m.id] = self.connector(m)
            for m in subject.members:
                conns[m.id].begin_episode(ep_tag, path / "agents" / m.id)
                fingerprints_start[m.id] = memory_fingerprint(m.memory) if m.kind == "omega" else \
                    {"available": False, "reason": "reference agent inside the harness has no memory store"}
                if m.logs:
                    taps[m.id] = LogTap(m.id, m.logs, path / "agents" / m.id)
                    taps[m.id].start()
                else:
                    (path / "agents" / m.id).mkdir(parents=True, exist_ok=True)
                if m.id in self.hooks:
                    self.hooks[m.id](env)
            status, extra = self._play(subject, env, obs, desc, task, seed, ep_tag, env_log, conns, taps)
        except KeyboardInterrupt:
            status, extra = "interrupted", {}
            raise
        except Exception as exc:
            log.exception("episode %s/%s failed", run_id, attempt)
            status, extra = "error", {"error": str(exc)[:500]}
            self.store.append_event(env_log, {"t": now(), "kind": "harness_error", "error": str(exc)[:2000]})
        finally:
            score = env.score() if hasattr(env, "steps") else {"success": False, "utility": 0.0, "steps": 0}
            end = Message(ep_tag, int(score.get("steps") or 0), "end",
                          text=f"Episode over: {status.replace('_', ' ')}. Stop acting on this episode.")
            for m in subject.members:
                try:
                    if m.id in conns:
                        conns[m.id].deliver(end)
                except Exception as exc:
                    log.warning("could not tell %s the episode ended: %s", m.id, exc)
            self.store.append_event(env_log, {"t": now(), "kind": "terminal", "status": status, "score": score,
                                              "floor": env.floor(), "ceiling": env.ceiling(),
                                              "max_steps": max_steps, **extra})
            time.sleep(self.cfg.grace_s if taps else 0)
            tap_status = {}
            events = read_jsonl(env_log)
            for m in subject.members:
                try:
                    if m.id in conns:
                        conns[m.id].end_episode()
                except Exception as exc:                  # the agent's own code: it must not break the record
                    log.warning("could not end the episode for %s: %s", m.id, exc)
                tap = taps.get(m.id)
                reported = [] if tap else reported_iterations(events, m.id)
                if tap:
                    tap.stop()
                    compress_log(path / "agents" / m.id)
                    tap_status[m.id] = {"configured": True, "error": tap.error, "lines": tap.lines,
                                        "llm_calls": tap.llm_calls, "source": "log"}
                elif reported:
                    self._write_reported(path / "agents" / m.id, m, reported)
                    tap_status[m.id] = {"configured": True, "error": None, "source": "reported",
                                        "llm_calls": sum(len(it["llm"]) for it in reported)}
                else:
                    tap_status[m.id] = {"configured": False,
                                        "error": "no log source configured" if m.kind == "omega"
                                        else "reference agent inside the harness: no trace" if m.kind == "baseline"
                                        else "the agent reported no trace events"}
            not_started = {"available": False, "reason": "the episode ended before this agent started"}
            fingerprints_start = {m.id: fingerprints_start.get(m.id, not_started) for m in subject.members}
            fingerprints_end = {m.id: (memory_fingerprint(m.memory) if m.kind == "omega" else
                                       fingerprints_start[m.id]) for m in subject.members}
            traces = {m.id: read_jsonl(path / "agents" / m.id / "trace.jsonl") for m in subject.members}
            manifest.update(status=status if status in ("error", "interrupted") else "finished", ended_at=now(),
                            provenance={
                                "agent_versions": {k: v.version for k, v in conns.items()},
                                "models_seen": sorted({f"{u.get('provider')}/{u.get('model')}"
                                                       for it in sum(traces.values(), []) for u in it.get("llm", [])
                                                       if u.get("model")}),
                                "memory_start": fingerprints_start, "memory_end": fingerprints_end,
                                "log_taps": tap_status})
            self.store.write_json(path / "manifest.json", manifest)
            res = result_mod.compute(manifest, read_jsonl(env_log), traces, self.cfg.prices, tap_status)
            self.store.write_json(path / "result.json", res)
            self.store.seal(path)
            outcome = res.get("outcome", {})
            self.store.ledger({"event": "finished", "run_id": run_id, "attempt": attempt, "status": manifest["status"],
                               "terminal": status if not outcome.get("void") else "void",
                               "success": outcome.get("success"),
                               "needs_judge": bool(outcome.get("needs_judge")), "void": bool(outcome.get("void")),
                               "steps": score.get("steps"), "ended_at": manifest["ended_at"]})
            with self._lock:
                for m in subject.members:
                    self.active.pop(m.id, None)
        return {"run_id": run_id, "attempt": attempt, "status": status, "success": res["outcome"].get("success"),
                "needs_judge": bool(res["outcome"].get("needs_judge")), "void": bool(res["outcome"].get("void")),
                "steps": score.get("steps"), "path": str(path)}

    # -- messages -------------------------------------------------------------------------
    @staticmethod
    def _evidence(m) -> str:
        transport = transport_of(m)
        if m.kind == "baseline":
            return "reference agent inside the harness"
        if m.kind == "omega" and m.logs:
            return evidence_grade(m.logs)
        return f"reported by the agent over {transport}"

    @staticmethod
    def _write_reported(folder: Path, m, iterations: list[dict]) -> None:
        import json

        folder.mkdir(parents=True, exist_ok=True)
        (folder / "trace.jsonl").write_text("".join(json.dumps(it, ensure_ascii=False) + "\n" for it in iterations),
                                            encoding="utf-8")
        meta = {"agent": m.id, "source": "reported", "transport": transport_of(m),
                "summary": {"iterations": len(iterations), "llm_calls": sum(len(it["llm"]) for it in iterations)}}
        (folder / "trace-meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def _rules(self, desc: dict) -> str:
        pol = desc.get("tool_policy") or {}
        allow, forbid = pol.get("allow"), pol.get("forbid") or []
        then = " and voids the episode" if pol.get("void_on_violation", True) else ""
        if allow:
            return (f"Rules: during this episode use only these skills: {', '.join(allow)}. "
                    f"Using any other skill is recorded{then}.")
        if forbid:
            return (f"Rules: do not use these skills during this episode: {', '.join(forbid)}. "
                    f"Their use is recorded{then}.")
        return ""

    def _message(self, desc, task, seed, ep_tag, tick, obs, role, actor, first=False) -> Message:
        return Message(episode=ep_tag, step=tick, kind="brief" if first else ("observation" if obs.expects_action
                                                                               else "info"),
                       role=role, text=obs.text, actions=list(obs.actions), expects_reply=obs.expects_action,
                       free_text=obs.free_text, predictions=self.cfg.predictions,
                       instructions="\n".join(x for x in (desc.get("brief", ""), self._rules(desc)) if x) if first else "",
                       benchmark=desc.get("name", desc.get("id", "")), task=task, seed=seed, actor=actor)

    # -- the step loop -----------------------------------------------------------------------
    def _play(self, subject, env, obs, desc, task, seed, ep_tag, env_log: Path, conns, taps):
        ev = lambda e: self.store.append_event(env_log, {"t": now(), **e})
        ev({"kind": "episode_start", "env": desc.get("id"), "task": task, "seed": seed, "ep_tag": ep_tag,
            "subject": subject.name, "actor": subject.actor})
        tick = 0
        reported_llm: dict[str, int] = {}

        def deliver(m, message: Message) -> None:
            seqs, sent = conns[m.id].deliver(message)
            ev({"kind": "observation", "tick": message.step, "to": m.id, "seq": seqs, "text": sent,
                "public": current.public, "actions": current.actions, "info": not current.expects_action,
                "free_text": current.free_text})

        current = obs
        for m in subject.members:
            role = "actor" if m.id == subject.actor else "advisor"
            deliver(m, self._message(desc, task, seed, ep_tag, 0, obs, role, subject.actor, first=True))
        frame = env.render()
        if frame:
            ev({"kind": "render", "tick": 0, "frame": frame})
        started = time.time()
        deadline = time.time() + self.cfg.step_timeout_s
        done = False
        pending: list = []

        def broadcast() -> None:
            for mm in subject.members:
                role = "actor" if mm.id == subject.actor else "advisor"
                deliver(mm, self._message(desc, task, seed, ep_tag, tick, current, role, subject.actor))

        def hear() -> bool:
            """Record every reply and reported trace that arrived; relay advice. True if anything came."""
            got = False
            for m in subject.members:
                for t_recv, frame_ in conns[m.id].drain_traces():
                    got = True
                    reported_llm[m.id] = reported_llm.get(m.id, 0) + sum(
                        1 for e in frame_.get("events") or [] if isinstance(e, dict) and e.get("kind") == "llm")
                    ev({"kind": "agent_trace", "agent": m.id, "trace": frame_, "received": _iso(t_recv)})
                for inc in conns[m.id].drain():
                    got = True
                    ev({"kind": "agent_message", "agent": m.id, "text": inc.text, "client_seq": inc.client_seq,
                        "received": _iso(inc.received), "tick_claim": inc.step, "ep_claim": inc.episode})
                    if inc.note != "announcement":
                        pending.append((m, inc))
            return got

        def llm_calls() -> int:
            return sum(t.llm_calls for t in taps.values()) + sum(reported_llm.values())

        while not done:
            if time.time() - started > self.cfg.wall_s:
                return "wall_budget", {}
            if self.cfg.max_llm_calls and llm_calls() > self.cfg.max_llm_calls:
                return "llm_budget", {"llm_calls": llm_calls()}

            if not current.expects_action:
                outcome = self._await_read(subject, taps, conns, ep_tag, tick, hear)
                pending.clear()                   # nothing is an action while information is being read
                if outcome == "timeout":
                    return "agent_timeout", {"waited_for_read_s": self.cfg.read_timeout_s, "tick": tick}
                receipt = env.delivered()
                ev({"kind": "delivered", "tick": tick, "effect": receipt.effect, "done": receipt.done,
                    "info": receipt.info, "confirmed_by": outcome})
                tick += 1
                current = receipt.observation
                frame = env.render()
                if frame:
                    ev({"kind": "render", "tick": tick, "frame": frame})
                if receipt.done:
                    break
                broadcast()
                deadline = time.time() + self.cfg.step_timeout_s
                continue

            if time.time() > deadline:
                return "agent_timeout", {"waited_s": self.cfg.step_timeout_s, "tick": tick}
            got = hear()
            while pending and not done:
                m, inc = pending.pop(0)
                if m.id != subject.actor:
                    if subject.relay:
                        for other in subject.members:
                            if other.id != m.id:
                                conns[other.id].deliver(Message(ep_tag, tick, "hive", text=inc.text, sender=m.id))
                    continue
                if inc.step is None and self.cfg.require_tick:
                    continue                      # chatter, not a reply to a step
                if inc.episode is not None and inc.episode != ep_tag:
                    ev({"kind": "stale_action", "agent": m.id, "tick": tick, "claimed_ep": inc.episode, "text": inc.text})
                    continue
                if inc.step is not None and inc.step != tick:
                    ev({"kind": "stale_action", "agent": m.id, "tick": tick, "claimed": inc.step, "text": inc.text})
                    continue
                action = env.parse_action(inc.action_text)
                receipt = env.step(action)
                ev({"kind": "action", "tick": tick, "agent": m.id, "text": inc.text, "parsed": inc.action_text,
                    "action": action, "valid": receipt.valid,
                    "prediction": inc.prediction if self.cfg.predictions else None})
                ev({"kind": "receipt", "tick": tick, "effect": receipt.effect, "done": receipt.done,
                    "info": receipt.info})
                tick += 1
                current = receipt.observation
                frame = env.render()
                if frame:
                    ev({"kind": "render", "tick": tick, "frame": frame})
                if receipt.done:
                    done = True
                    break
                broadcast()
                deadline = time.time() + self.cfg.step_timeout_s
                pending.clear()                   # replies queued before this observation are stale
                if not current.expects_action:
                    break
            if not got:
                time.sleep(0.05)
        hear()                                    # record anything that arrived with the last reply
        return env.score()["terminal"], {}

    def _await_read(self, subject, taps, conns, ep_tag, tick, hear) -> str:
        """Wait until every member that can show it has read the information message at this tick:
        through its log for OmegaClaw, through its answer for request-and-response agents."""
        by_log = [taps[m.id] for m in subject.members if m.id in taps]
        by_ack = [conns[m.id] for m in subject.members if m.id not in taps and conns[m.id].confirms_reads]
        if not by_log and not by_ack:
            time.sleep(self.cfg.pace_s)
            hear()
            return "paced"
        deadline = time.time() + self.cfg.read_timeout_s
        while time.time() < deadline:
            if all(t.has_read(ep_tag, tick) for t in by_log) and all(c.has_read(ep_tag, tick) for c in by_ack):
                hear()
                return "read" if not by_ack else ("acknowledged" if not by_log else "read and acknowledged")
            if not hear():
                time.sleep(0.05)
        return "timeout"
