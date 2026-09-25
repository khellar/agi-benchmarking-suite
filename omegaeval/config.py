"""omegaeval.toml: which agents and hives exist, where their evidence lives, which benchmarks are installed.

Benchmarks are found in four places, and a later one overrides an earlier one with the same id:

    1. built in: keydoor, qa-sample, memory-sample
    2. installed packages that declare an `omegaeval.environments` entry point
    3. Python files in the folders listed in [harness] benchmark_dirs; each Environment
       subclass in them is registered under its `id`
    4. [environments.<id>] blocks: an adapter plus options, so one adapter can serve several
       benchmarks, for example QADataset with different question files

A benchmark id is a short lowercase name people choose, such as hle, arc-agi-3 or freeciv.
It names the benchmark everywhere: in run ids (hle-3f9a1c1b2c3d), on the command line and in
the console. A label ("HLE") and a name ("Humanity's Last Exam") are for display.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Callable

from .envs.base import Environment, load_adapter

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

BUILTIN_ENVS: dict[str, dict] = {
    "keydoor": {"adapter": "omegaeval.envs.keydoor:KeyDoor", "options": {}, "label": "Keydoor"},
    # The samples' answers are unambiguous numbers, letters and names, so a mismatch is simply wrong.
    "qa-sample": {"adapter": "omegaeval.envs.qa:QADataset", "options": {"sample": True, "judge_on_mismatch": False},
                  "label": "QA sample"},
    "memory-sample": {"adapter": "omegaeval.envs.chatmemory:ChatMemory",
                      "options": {"sample": True, "judge_on_mismatch": False}, "label": "Memory sample"},
}

# Every key omegaeval.toml accepts: (table, key, default, meaning). The reference docs are
# generated from this list, doctor flags keys that are not in it, and a test checks that the
# loader reads nothing else.
SCHEMA: list[tuple[str, str, object, str]] = [
    ("harness", "store", "omegaeval-store", "Folder where runs are kept. Relative paths resolve against this file."),
    ("harness", "benchmark_dirs", ["benchmarks"], "Folders searched for benchmark .py files."),
    ("harness.gateway", "host", "0.0.0.0", "Address the gateway listens on for agents."),
    ("harness.gateway", "port", 8770, "Port the gateway listens on."),
    ("harness.gateway", "agent_url", "ws://host.docker.internal:8770",
     "Address agents dial, as printed by omegaeval connect."),
    ("harness.console", "host", "127.0.0.1", "Address the console listens on."),
    ("harness.console", "port", 8780, "Port the console listens on."),
    ("harness.episodes", "step_timeout_s", 600, "Seconds an agent has to reply before the episode ends as agent_timeout."),
    ("harness.episodes", "wall_s", 10800, "Seconds an episode may run in total."),
    ("harness.episodes", "grace_s", 15, "Seconds to keep reading an agent's log after the episode ends."),
    ("harness.episodes", "read_timeout_s", 600,
     "Seconds to wait for the log to show an information-only message was read."),
    ("harness.episodes", "pace_s", 0.2, "Pause between information-only messages to agents with no log source."),
    ("harness.episodes", "max_llm_calls", 0, "End an episode when the agent's log shows more LLM calls. 0 means no limit."),
    ("harness.episodes", "require_tick", True, "Accept only replies that name the current episode and step."),
    ("harness.episodes", "predictions", False, "Ask the agent for p=<0..1> with each action."),
    ("agents.<id>", "connect", None, "How the harness reaches the agent: { http = \"URL\" }, { command = [\"python\", "
     "\"agent.py\"] } or { python = \"module:Class\" }. Without it, the agent dials the websocket gateway. cwd is the "
     "folder a command runs in and a python module is found in, this file's folder by default; options are keyword "
     "arguments for a python class."),
    ("agents.<id>", "token", None, "The WS_TOKEN a websocket agent presents to the gateway; required for those. An http "
     "agent receives it as a bearer token. \"${NAME}\" reads it from the environment variable NAME."),
    ("agents.<id>", "label", "", "Display name, recorded in every manifest."),
    ("agents.<id>", "kind", "omega", "omega for OmegaClaw, whose log and memory the harness can read, or other."),
    ("agents.<id>", "logs", None, "Where its log is: { docker = \"name\" } or { file = \"path\" }, plus host = \"ssh://...\"."),
    ("agents.<id>", "memory", None, "Where its memory is: { docker = \"name\" } or { home = \"PeTTa root\" }, plus host."),
    ("agents.<id>", "config", {}, "Its configuration, recorded verbatim in every manifest."),
    ("hives.<id>", "members", None, "Required. Agent ids in the hive."),
    ("hives.<id>", "actor", "(the first member)", "The member whose replies move the benchmark."),
    ("hives.<id>", "relay", True, "Relay advisors' messages to the whole hive."),
    ("hives.<id>", "label", "", "Display name."),
    ("environments.<id>", "adapter", None, "module:Class of the adapter. Optional when <id> is already installed."),
    ("environments.<id>", "options", {}, "Keyword arguments for the adapter, such as path, judge_all or allow_tools."),
    ("environments.<id>", "label", "", "Short display name, such as HLE. Defaults to the id."),
    ("environments.<id>", "name", "", "Full display name, such as Humanity's Last Exam."),
    ('prices."<model>"', "input", 0.0, "USD per million uncached prompt tokens."),
    ('prices."<model>"', "cached_input", "(the input price)", "USD per million cached prompt tokens."),
    ('prices."<model>"', "output", 0.0, "USD per million completion tokens."),
]


def check_id(env_id: str) -> str | None:
    """Why a benchmark id is not usable, or None."""
    if ID_RE.match(env_id or ""):
        return None
    return (f"benchmark id {env_id!r} must be 1-40 lowercase letters, digits, '-' or '_', starting with a letter "
            f"or digit, because it becomes part of every run id; put display text in label and name")


@dataclass
class AgentSpec:
    id: str
    token: str
    label: str = ""
    kind: str = "omega"                 # "omega" | "baseline" | "other"
    logs: dict | None = None            # {docker=..} | {file=..}
    memory: dict | None = None          # {docker=..} | {home=..}
    config: dict = field(default_factory=dict)   # recorded verbatim in every manifest
    connect: dict = field(default_factory=dict)  # {http=URL} | {command=[...]} | {python="module:Class"}; empty: websocket


@dataclass
class HiveSpec:
    id: str
    members: list[str]
    actor: str
    relay: bool = True
    label: str = ""


@dataclass
class Subject:
    """What an episode is run against: one agent, or one hive."""
    name: str
    kind: str                           # "agent" | "hive"
    members: list[AgentSpec]
    actor: str
    relay: bool
    label: str


@dataclass
class Config:
    path: Path | None
    store: Path
    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8770
    agent_url: str = "ws://host.docker.internal:8770"   # the address agents dial; printed in connect help
    console_host: str = "127.0.0.1"
    console_port: int = 8780
    step_timeout_s: float = 600.0
    wall_s: float = 3 * 3600.0
    grace_s: float = 15.0
    read_timeout_s: float = 600.0       # how long to wait for an agent to read an information-only message
    pace_s: float = 0.2                 # pause between information-only messages to agents with no log tap
    max_llm_calls: int = 0              # end an episode when the agent's log shows more LLM calls; 0 = no limit
    require_tick: bool = True
    predictions: bool = False
    agents: dict[str, AgentSpec] = field(default_factory=dict)
    hives: dict[str, HiveSpec] = field(default_factory=dict)
    environments: dict[str, dict] = field(default_factory=dict)
    benchmark_dirs: list[Path] = field(default_factory=list)
    prices: dict[str, dict] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)      # config keys the harness does not read
    _describe_cache: dict = field(default_factory=dict, repr=False, compare=False)

    # -- lookups -------------------------------------------------------------------------
    def subject(self, name: str) -> Subject:
        if name in self.agents:
            a = self.agents[name]
            return Subject(name, "agent", [a], a.id, False, a.label or a.id)
        if name in self.hives:
            h = self.hives[name]
            missing = [m for m in h.members if m not in self.agents]
            if missing:
                raise KeyError(f"hive {name!r} lists unknown agents: {missing}")
            if h.actor not in h.members:
                raise KeyError(f"hive {name!r}: actor {h.actor!r} is not a member")
            return Subject(name, "hive", [self.agents[m] for m in h.members], h.actor, h.relay, h.label or name)
        raise KeyError(f"no agent or hive named {name!r} in {self.path or 'config'}")

    def registry(self) -> dict[str, dict]:
        """benchmark id -> {adapter, options, origin, label, name[, error]}. Rescans benchmark folders,
        re-reading only files that changed, so a new benchmark file is picked up without a restart."""
        reg: dict[str, dict] = {k: {"adapter": v["adapter"], "options": v.get("options", {}), "origin": "built in",
                                    "label": v.get("label", ""), "name": v.get("name", "")}
                                for k, v in BUILTIN_ENVS.items()}
        for ep in entry_points(group="omegaeval.environments"):
            if ep.name not in BUILTIN_ENVS:
                reg[ep.name] = {"adapter": ep.value, "options": {}, "label": "", "name": "",
                                "origin": f"package {ep.dist.name if ep.dist else ''}".strip()}
        for d in self.benchmark_dirs:
            for cls, file in discover(d):
                reg[cls.id] = {"adapter": cls, "options": {}, "origin": f"file {file}",
                               "label": getattr(cls, "label", "") or "", "name": ""}
        for k, v in self.environments.items():
            base = reg.get(k, {})
            reg[k] = {"adapter": v.get("adapter") or base.get("adapter"), "options": v.get("options", {}),
                      "origin": f"config [environments.{k}]", "label": v.get("label") or base.get("label", ""),
                      "name": v.get("name") or base.get("name", "")}
        for k, entry in reg.items():
            err = check_id(k) or (None if entry.get("adapter") else f"[environments.{k}] needs an adapter")
            if err:
                entry["error"] = err
        return reg

    def env_factory(self, env_id: str) -> Callable[[], Environment]:
        reg = self.registry()
        if env_id not in reg:
            raise KeyError(f"unknown benchmark {env_id!r}; installed: {sorted(reg)}")
        entry = reg[env_id]
        if entry.get("error"):
            raise ValueError(entry["error"])
        cls = entry["adapter"] if isinstance(entry["adapter"], type) else load_adapter(entry["adapter"])
        options = dict(entry.get("options") or {})

        def make() -> Environment:
            env = cls(**options)
            env.id = env_id
            return env

        return make

    def make_env(self, env_id: str) -> Environment:
        return self.env_factory(env_id)()

    def env_ids(self) -> list[str]:
        return sorted(self.registry())

    def display(self, env_id: str, desc: dict | None = None) -> dict:
        """How people see a benchmark: its id, a short label and a full name."""
        entry = self.registry().get(env_id, {})
        desc = desc or {}
        return {"id": env_id, "label": entry.get("label") or desc.get("label") or env_id,
                "name": entry.get("name") or desc.get("name") or env_id}

    def describe(self, env_id: str) -> dict:
        """describe() plus display names and origin, cached until the adapter, its options or its
        data file change, so listing benchmarks never reloads a dataset that has not changed."""
        reg = self.registry()
        entry = reg.get(env_id)
        if entry is None:
            raise KeyError(f"unknown benchmark {env_id!r}")
        stamp = _stamp(entry)
        hit = self._describe_cache.get(env_id)
        if hit and hit[0] == stamp:
            return hit[1]
        base = {"origin": entry["origin"]}
        try:
            if entry.get("error"):
                raise ValueError(entry["error"])
            d = {**self.make_env(env_id).describe(), **base}
        except Exception as exc:
            d = {"id": env_id, "error": str(exc)[:300], **base}
        shown = self.display(env_id, d)
        d.update(id=env_id, label=shown["label"], name=shown["name"])
        self._describe_cache[env_id] = (stamp, d)
        return d

    @staticmethod
    def default_task(env: Environment) -> str:
        d = env.describe()
        return d.get("default_task") or (d.get("tasks") or ["all"])[0]


def _stamp(entry: dict) -> tuple:
    opts = entry.get("options") or {}
    adapter = entry.get("adapter")
    parts = [entry.get("origin"), entry.get("label"), entry.get("name"),
             f"{adapter.__module__}.{adapter.__qualname__}:{id(adapter)}" if isinstance(adapter, type) else adapter,
             json.dumps(opts, sort_keys=True, default=str)]
    path = opts.get("path")
    if isinstance(path, str):
        try:
            st = Path(path).stat()
            parts.append(f"{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append("missing")
    return tuple(parts)


_DISCOVERED: dict[str, tuple[tuple, list]] = {}


def discover(folder: Path) -> list[tuple[type[Environment], str]]:
    """Environment subclasses defined in the .py files of a folder. A file is executed again
    only when it changes."""
    out = []
    for f in sorted(Path(folder).glob("*.py")):
        if f.name.startswith("_"):
            continue
        st = f.stat()
        key, stamp = str(f.resolve()), (st.st_mtime_ns, st.st_size)
        hit = _DISCOVERED.get(key)
        if hit and hit[0] == stamp:
            out.extend(hit[1])
            continue
        name = f"omegaeval_benchmark_{f.stem}"
        spec = importlib.util.spec_from_file_location(name, f)
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        found = [(obj, str(f)) for _, obj in inspect.getmembers(mod, inspect.isclass)
                 if issubclass(obj, Environment) and obj.__module__ == name and obj.id not in ("base", "")]
        _DISCOVERED[key] = (stamp, found)
        out.extend(found)
    return out


CONNECT_KEYS = {"http": str, "command": (list, str), "python": str, "cwd": str, "options": dict}


def check_connect(connect: dict) -> str | None:
    """Why a connect table is not usable, or None."""
    ways = [k for k in ("http", "command", "python") if k in connect]
    if len(ways) != 1:
        return "give exactly one of http, command or python"
    for k, v in connect.items():
        if k not in CONNECT_KEYS:
            return f"{k} is not a connect key; use http, command, python, cwd or options"
        if not isinstance(v, CONNECT_KEYS[k]):
            return f"{k} has the wrong type"
    if "http" in connect and not str(connect["http"]).startswith(("http://", "https://")):
        return "http must be an http or https URL"
    if "python" in connect and ":" not in connect["python"]:
        return "python must be module:Class"
    return None


def unknown_keys(data: dict) -> list[str]:
    """Keys in a parsed omegaeval.toml that the harness does not read."""
    known: dict[str, set] = {}
    for table, key, _, _ in SCHEMA:
        known.setdefault(table, set()).add(key)
    out = []
    top = {"harness", "agents", "hives", "environments", "prices"}
    for k in data:
        if k not in top:
            out.append(k)
    h = data.get("harness", {}) or {}
    for k, v in h.items():
        if k in ("gateway", "console", "episodes") and isinstance(v, dict):
            out += [f"harness.{k}.{kk}" for kk in v if kk not in known[f"harness.{k}"]]
        elif k not in known["harness"]:
            out.append(f"harness.{k}")
    for table in ("agents", "hives", "environments"):
        for name, block in (data.get(table) or {}).items():
            if isinstance(block, dict):
                out += [f"{table}.{name}.{kk}" for kk in block if kk not in known[f"{table}.<id>"]]
    for model, block in (data.get("prices") or {}).items():
        if isinstance(block, dict):
            out += [f'prices."{model}".{kk}' for kk in block if kk not in known['prices."<model>"']]
    return out


ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def secret(value: str, where: str) -> str:
    """A token as written, or "${NAME}" read from the environment, so a config file need hold no secrets."""
    m = ENV_REF.match(value or "")
    if not m:
        return value
    got = os.environ.get(m.group(1), "")
    if not got:
        raise SystemExit(f"{where}: the environment variable {m.group(1)} is not set")
    return got


def load(path: str | Path | None, allow_missing: bool = False) -> Config:
    """Read omegaeval.toml. With allow_missing, a missing file gives the defaults (no Omega agents)."""
    if path is None or (allow_missing and not Path(path).exists()):
        return Config(path=None, store=Path("omegaeval-store").resolve(),
                      benchmark_dirs=[p for p in [Path("benchmarks").resolve()] if p.is_dir()])
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"{p} not found. Create one with: omegaeval init")
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    h = data.get("harness", {})
    gw, cn, ep = h.get("gateway", {}), h.get("console", {}), h.get("episodes", {})

    def rel(x) -> Path:
        q = Path(x).expanduser()
        return q if q.is_absolute() else (p.parent / q).resolve()

    def env_block(block: dict) -> dict:
        opts = dict(block.get("options", {}))
        if isinstance(opts.get("path"), str):       # data files resolve against this config file, like the store
            opts["path"] = str(rel(opts["path"]))
        return {"adapter": block.get("adapter"), "options": opts, "label": block.get("label", ""),
                "name": block.get("name", "")}

    cfg = Config(
        path=p, store=rel(h.get("store", "omegaeval-store")),
        gateway_host=gw.get("host", "0.0.0.0"), gateway_port=int(gw.get("port", 8770)),
        agent_url=gw.get("agent_url", f"ws://host.docker.internal:{int(gw.get('port', 8770))}"),
        console_host=cn.get("host", "127.0.0.1"), console_port=int(cn.get("port", 8780)),
        step_timeout_s=float(ep.get("step_timeout_s", 600)), wall_s=float(ep.get("wall_s", 3 * 3600)),
        grace_s=float(ep.get("grace_s", 15)), read_timeout_s=float(ep.get("read_timeout_s", 600)),
        pace_s=float(ep.get("pace_s", 0.2)), max_llm_calls=int(ep.get("max_llm_calls", 0)),
        require_tick=bool(ep.get("require_tick", True)), predictions=bool(ep.get("predictions", False)),
        environments={k: env_block(v) for k, v in data.get("environments", {}).items()},
        benchmark_dirs=[rel(d) for d in h.get("benchmark_dirs", ["benchmarks"]) if rel(d).is_dir()],
        prices=data.get("prices", {}),
        unknown=unknown_keys(data),
    )
    for aid, a in data.get("agents", {}).items():
        connect = dict(a.get("connect") or {})
        problem = check_connect(connect) if connect else None
        if problem:
            raise SystemExit(f"[agents.{aid}] connect: {problem}")
        if not connect and not a.get("token"):
            raise SystemExit(f"[agents.{aid}] needs a token for the websocket gateway, or a connect table for "
                             "http, command or python")
        if connect:                                  # an agent's files sit next to this config unless it says
            connect["cwd"] = str(rel(connect.get("cwd") or "."))
        cfg.agents[aid] = AgentSpec(id=aid, token=secret(a.get("token", ""), f"[agents.{aid}] token"),
                                    label=a.get("label", ""),
                                    kind=a.get("kind", "omega" if not connect else "other"), logs=a.get("logs"),
                                    memory=a.get("memory"), config=a.get("config", {}), connect=connect)
    for hid, hv in data.get("hives", {}).items():
        cfg.hives[hid] = HiveSpec(id=hid, members=list(hv["members"]), actor=hv.get("actor", hv["members"][0]),
                                  relay=bool(hv.get("relay", True)), label=hv.get("label", ""))
    return cfg
