"""`omegaeval serve`: the gateway, the episode queue and the console, in one process.

The console is the same HTML file that can be opened on its own. Served from here
it finds /api/state and switches to local mode: live runs, starting episodes, and
reading full prompts from the raw logs.
"""
from __future__ import annotations

import gzip
import json
import logging
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlparse

from . import __version__
from .baselines import BaselineAgent, spec_for
from .config import Config
from .connectors import preflight, transport_of
from .episode import EpisodeRunner
from .gateway import Gateway
from .omega_trace import TraceBuilder
from .store import Store

log = logging.getLogger("omegaeval.server")

# The HTTP API the console uses, which scripts can use too. docs/REFERENCE.md is generated from
# this list, and a test calls every GET route.
API_ROUTES = [
    ("GET", "/", "the console page"),
    ("GET", "/api/state", "harness version and links, store, ledger_rev, run counts per benchmark, agents, hives, "
                          "installed benchmarks with labels, the job queue"),
    ("GET", "/api/runs", "attempts, newest first: {total, limit, offset, rows}. Query: any filter key, q=text, "
                         "limit (default 500), offset"),
    ("GET", "/api/results", "compact Result Records of finished attempts. Query: any filter key, variant_is=, limit"),
    ("GET", "/api/progress", "solve counts per set of runs and per agent version. Query: any filter key"),
    ("GET", "/api/runs/<run_id>/<attempt>", "everything about one attempt: manifest, result, messages, parsed traces"),
    ("GET", "/api/runs/<run_id>/<attempt>/raw/<agent>/<line>", "one line of an agent's raw log"),
    ("POST", "/api/episodes", "queue episodes. Body: {subject, env, task, seeds, variant, memory_mode, note}"),
    ("POST", "/api/import-log", "parse an OmegaClaw log sent as the body into a console bundle. Query: agent="),
]


class Harness:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.store)
        self.gateway = Gateway(cfg.gateway_host, cfg.gateway_port)
        for a in cfg.agents.values():
            if transport_of(a) == "websocket":
                self.gateway.add(a.id, a.token)
        self.runner = EpisodeRunner(cfg, self.store, self.gateway)
        self.jobs: list[dict] = []
        self._jobs_lock = threading.Lock()
        self._workers: dict[str, threading.Thread] = {}
        self.baselines: dict[str, BaselineAgent] = {}

    def start(self, with_baselines: bool = True) -> "Harness":
        self.recovered = self.store.recover_interrupted()
        self.gateway.start()
        if with_baselines:
            for pol in ("random", "oracle"):
                spec, policy = spec_for(pol)
                self.cfg.agents[spec.id] = spec
                self.gateway.add(spec.id, spec.token)
                self.runner.hooks[spec.id] = policy.prepare
                agent = BaselineAgent(spec.id, f"ws://127.0.0.1:{self.gateway.port}/agents/{spec.id}",
                                      spec.token, policy)
                agent.start()
                agent.ready.wait(10)
                self.baselines[spec.id] = agent
        return self

    # -- config ---------------------------------------------------------------------------------
    def reload_config(self) -> bool:
        """Reread omegaeval.toml when it changes. New benchmarks, agents and hives take effect at
        once; the store, ports and existing agents' tokens need a restart."""
        from .config import load

        path = self.cfg.path
        if path is None:
            return False
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return False
        if getattr(self, "_config_mtime", None) in (None, mtime):
            self._config_mtime = mtime
            return False
        self._config_mtime = mtime
        try:
            new = load(path)
        except Exception as exc:                  # a half-saved file: keep the running config
            log.warning("omegaeval.toml not reloaded: %s", exc)
            return False
        self.cfg.environments, self.cfg.benchmark_dirs = new.environments, new.benchmark_dirs
        self.cfg.prices, self.cfg.unknown = new.prices, new.unknown
        for aid, spec in new.agents.items():
            if aid not in self.cfg.agents:
                try:
                    if transport_of(spec) == "websocket":
                        self.gateway.add(aid, spec.token)
                except ValueError as exc:
                    log.warning("agent %s not added: %s", aid, exc)
                    continue
                self.cfg.agents[aid] = spec
        self.cfg.hives.update(new.hives)
        log.info("reloaded %s", path)
        return True

    # -- jobs -----------------------------------------------------------------------------------
    def submit(self, subject: str, env: str, task: str | None, seeds: list[int], variant: str = "",
               memory_mode: str = "carry", note: str = "") -> list[dict]:
        self.reload_config()
        for m in self.cfg.subject(subject).members:      # validates the name, and that each agent can be reached
            if transport_of(m) != "websocket":
                ok, detail = preflight(m)
                if not ok:
                    raise ValueError(f"agent {m.id}: {detail}")
        probe = self.cfg.make_env(env)     # validates the benchmark and its options
        task = task or self.cfg.default_task(probe)
        new = []
        with self._jobs_lock:
            for seed in seeds:
                job = {"id": f"j{len(self.jobs) + 1}", "subject": subject, "env": env, "task": task, "seed": seed,
                       "variant": variant, "memory_mode": memory_mode, "note": note, "state": "queued",
                       "queued_at": time.time()}
                self.jobs.append(job)
                new.append(job)
            if subject not in self._workers or not self._workers[subject].is_alive():
                t = threading.Thread(target=self._work, args=(subject,), daemon=True, name=f"jobs-{subject}")
                self._workers[subject] = t
                t.start()
        return new

    def _work(self, subject: str) -> None:
        while True:
            with self._jobs_lock:
                job = next((j for j in self.jobs if j["subject"] == subject and j["state"] == "queued"), None)
                if job is None:
                    return
                job["state"] = "running"
            try:
                out = self.runner.run(subject, job["env"], job["task"], job["seed"], variant=job["variant"],
                                      memory_mode=job["memory_mode"], note=job["note"])
                job.update(state="done", result=out)
            except Exception as exc:
                job.update(state="failed", error=str(exc)[:500])

    def state(self) -> dict:
        self.reload_config()
        envs = {eid: self.cfg.describe(eid) for eid in self.cfg.env_ids()}
        agents = []
        for a in self.cfg.agents.values():
            try:
                conn = self.runner.connector(a).status()
            except KeyError:
                conn = {"connected": False}
            agents.append({"id": a.id, "kind": a.kind, "label": a.label, "logs": a.logs, "memory": a.memory,
                           "config": a.config, "transport": transport_of(a), "active": self.runner.active.get(a.id),
                           **conn})
        hives = [{"id": h.id, "members": h.members, "actor": h.actor, "relay": h.relay, "label": h.label}
                 for h in self.cfg.hives.values()]
        with self._jobs_lock:
            jobs = [dict(j) for j in self.jobs[-200:]]
        return {"mode": "local", "version": __version__, "config": str(self.cfg.path) if self.cfg.path else None,
                "store": str(self.cfg.store), "store_id": self.store.meta.get("store_id"),
                "ledger_rev": self.store.ledger_rev(), "run_counts": self.store.env_counts(),
                "links": project_links(), "gateway_port": self.gateway.port, "agent_url": self.cfg.agent_url,
                "agents": agents, "hives": hives, "environments": envs, "jobs": jobs,
                "protocol": {"require_tick": self.cfg.require_tick, "predictions": self.cfg.predictions,
                             "step_timeout_s": self.cfg.step_timeout_s}}


def project_links() -> dict:
    """The project's URLs from the installed package metadata: repository, documentation, demo."""
    try:
        from importlib.metadata import metadata

        out = {}
        for entry in metadata("omegaeval").get_all("Project-URL") or []:
            label, _, url = entry.partition(",")
            out[label.strip().lower()] = url.strip()
        return out
    except Exception:
        return {}


def import_log_bundle(text: str, agent: str = "imported") -> dict:
    """An Omega log file on its own, as a console bundle with no environment."""
    tb = TraceBuilder()
    tb.feed_many(text.splitlines())
    tb.finish()
    return {"schema": "omegaeval.run/0", "run_id": "import", "attempt": 0, "kind": "log-import",
            "manifest": {"status": "imported", "identity": {"subject": agent, "members": [agent], "env": None},
                         "members": {agent: {"kind": "omega", "label": agent}}},
            "result": None, "env": [],
            "agents": {agent: {"iterations": tb.iterations,
                               "meta": {"startup": tb.startup, "summary": tb.summary()}, "raw_lines": tb.lines_read}}}


def make_handler(h: Harness):
    console = files("omegaeval").joinpath("console/index.html")

    class Handler(BaseHTTPRequestHandler):
        server_version = f"omegaeval/{__version__}"

        def log_message(self, fmt, *args):
            log.debug("http " + fmt, *args)

        def _json(self, obj, status=HTTPStatus.OK):
            body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            zipped = len(body) > 2048 and "gzip" in (self.headers.get("Accept-Encoding") or "")
            if zipped:                     # result lists repeat the same keys and reasons: they shrink ~20x
                body = gzip.compress(body, compresslevel=5)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if zipped:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _refuse(self) -> bool:
            """Refuse requests a browser could be tricked into sending: a Host that is not this machine
            (DNS rebinding) and writes from another origin (cross-site requests)."""
            host = (self.headers.get("Host") or "").strip().lower()
            name = host.rsplit(":", 1)[0].strip("[]") if not host.startswith("[") else host[1:host.find("]")]
            bound = (h.cfg.console_host or "").lower()
            if bound not in ("0.0.0.0", "::", "") and name not in {"127.0.0.1", "localhost", "::1", bound}:
                self._json({"error": f"unexpected Host {host!r}"}, HTTPStatus.FORBIDDEN)
                return True
            if self.command == "POST":
                origin = self.headers.get("Origin")
                if origin and urlparse(origin).netloc.lower() != host:
                    self._json({"error": "cross-origin requests are refused"}, HTTPStatus.FORBIDDEN)
                    return True
            return False

        def do_GET(self):
            if self._refuse():
                return
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            try:
                if not parts:
                    body = console.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif parts == ["api", "state"]:
                    self._json(h.state())
                elif parts == ["api", "runs"]:
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    limit, offset = int(q.pop("limit", 500)), int(q.pop("offset", 0))
                    self._json({"total": h.store.count(**q), "limit": limit, "offset": offset,
                                "rows": h.store.attempts(limit=limit, offset=offset, **q)})
                elif parts == ["api", "results"]:
                    q = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
                    self._json(h.store.results(limit=int(q.pop("limit", 50000)), **q))
                elif parts == ["api", "progress"]:
                    self._json(h.store.progress(**{k: v[0] for k, v in parse_qs(url.query).items()}))
                elif len(parts) == 4 and parts[:2] == ["api", "runs"]:
                    self._json(h.store.bundle(parts[2], parts[3]))
                elif len(parts) == 7 and parts[:2] == ["api", "runs"] and parts[4] == "raw":
                    line = h.store.raw_line(parts[2], parts[3], parts[5], int(parts[6]))
                    self._json({"line": line})
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except FileNotFoundError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                log.exception("GET %s", self.path)
                self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self):
            if self._refuse():
                return
            url = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                if url.path == "/api/episodes":
                    if "application/json" not in (self.headers.get("Content-Type") or ""):
                        self._json({"error": "send JSON with Content-Type: application/json"},
                                   HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                        return
                    body = json.loads(raw or b"{}")
                    seeds = body.get("seeds") or [body.get("seed", 1)]
                    jobs = h.submit(body["subject"], body["env"], body.get("task") or None,
                                    [int(s) for s in seeds], body.get("variant", ""),
                                    body.get("memory_mode", "carry"), body.get("note", ""))
                    self._json({"queued": jobs})
                elif url.path == "/api/import-log":
                    q = parse_qs(url.query)
                    self._json(import_log_bundle(raw.decode("utf-8", errors="replace"),
                                                 q.get("agent", ["imported"])[0]))
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except (KeyError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                log.exception("POST %s", self.path)
                self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    return Handler


def serve(cfg: Config, with_baselines: bool = True) -> tuple[Harness, ThreadingHTTPServer]:
    h = Harness(cfg).start(with_baselines=with_baselines)
    httpd = ThreadingHTTPServer((cfg.console_host, cfg.console_port), make_handler(h))
    return h, httpd
