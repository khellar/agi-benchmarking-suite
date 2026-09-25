"""omegaeval command line."""
from __future__ import annotations

import argparse
import json
import logging
import secrets
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .config import load

TEMPLATE = """# omegaeval.toml: the agents and hives you benchmark, and where their evidence lives.
# Keep this file private: the tokens let a process act as an agent. A token written as "${NAME}"
# is read from the environment variable NAME instead, for CI and containers.

[harness]
store = "./omegaeval-store"          # append-only history of every episode; keep it on a local disk

[harness.gateway]                    # OmegaClaw agents dial this with commchannel=websocket
host = "0.0.0.0"
port = 8770
agent_url = "ws://host.docker.internal:8770"   # the address as seen from inside the agent's container

[harness.console]
host = "127.0.0.1"
port = 8780

[harness.episodes]
step_timeout_s = 600                 # no action for this long ends the episode as agent_timeout
wall_s = 10800
grace_s = 15                         # keep reading the agent's log this long after the episode ends
require_tick = true                  # replies must name the episode and step: ep=3f9a1c1 t=12 move N
predictions = false                  # true asks the agent for p=<0..1> with each action

# One block per agent. logs and memory are read-only.
[agents.oma-1]
token = "{token1}"
label = "Omega v0.1.19, Anthropic, reasoning medium"
logs = { docker = "omega" }                    # or { file = "/srv/agents/oma-1/omega.log" }
memory = { docker = "omega" }                  # or { home = "/srv/agents/oma-1/PeTTa" }
config = { provider = "Anthropic", reasoningMode = "medium", maxOutputToken = 6000 }   # recorded in every manifest

# [agents.oma-2]
# token = "{token2}"
# logs = { docker = "omega-2", host = "ssh://user@gpu-box" }
# memory = { docker = "omega-2", host = "ssh://user@gpu-box" }

# Agents other than OmegaClaw: the harness reaches them, so they need no token.
# [agents.claude]
# label = "Claude Opus 5.5, no tools"
# connect = { python = "claude:Claude", cwd = "examples/agents" }
#
# [agents.my-service]
# connect = { http = "http://127.0.0.1:9100/" }
#
# [agents.my-script]
# connect = { command = ["python3", "my_agent.py"] }

# A hive is a set of agents that play one episode together. The actor takes the
# actions; the others advise, and their messages are relayed to the whole hive.
# [hives.alpha]
# members = ["oma-1", "oma-2"]
# actor = "oma-1"
# relay = true

# Benchmarks beyond the built-in samples. A .py file in ./benchmarks is found on its own.
# A block here points an adapter at a data file. Relative paths resolve against this file.
# [environments.agi_maze]
# adapter = "agi_maze_omegaeval:AGIMaze"
#
# [environments.hle]
# adapter = "omegaeval.envs.qa:QADataset"
# [environments.hle.options]
# path = "/data/hle.jsonl"
# name = "Humanity's Last Exam (text-only)"
# judge_all = true
# allow_tools = ["send", "remember", "query", "pin", "episodes", "metta"]
#
# [environments.longmemeval]
# adapter = "omegaeval.envs.chatmemory:ChatMemory"
# [environments.longmemeval.options]
# path = "/data/longmemeval_s_cleaned.json"
# name = "LongMemEval (S, cleaned)"
# judge_all = true

# Optional token prices in USD per million tokens, by model name as logged by Omega.
# [prices."<model>"]
# input = 0.0
# cached_input = 0.0
# output = 0.0
"""


SELECT_HELP = ("attempts: run_id/attempt, a run id or its start, a benchmark id, or key=value filters "
               "such as subject=oma-1")


def parse_seeds(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def cmd_init(args) -> int:
    path = Path(args.dir) / "omegaeval.toml"
    if path.exists():
        print(f"{path} already exists; nothing written.")
        return 1
    text = TEMPLATE.replace("{token1}", secrets.token_hex(16)).replace("{token2}", secrets.token_hex(16))
    path.write_text(text, encoding="utf-8")
    print(f"Wrote {path}. Edit the [agents] blocks, then run: omegaeval serve -c {path}")
    return 0


def cmd_connect(args) -> int:
    from .connectors import transport_of

    cfg = load(args.config)
    if args.agent not in cfg.agents:
        raise SystemExit(f"no agent {args.agent!r} in {args.config}; agents: {', '.join(cfg.agents) or 'none'}")
    a = cfg.agents[args.agent]
    transport = transport_of(a)
    if transport != "websocket":
        cmd = a.connect.get("command")
        cmd = " ".join(cmd) if isinstance(cmd, list) else cmd
        how = {"http": f"POSTs each message to {a.connect.get('http')}. Start your agent's server there.",
               "command": f"starts `{cmd}` for each episode and speaks JSON Lines with it.",
               "python": f"loads {a.connect.get('python')} in its own process for each episode."}[transport]
        print(f"# Agent {a.id}: the harness {how}")
        print("# Nothing else to connect. Messages, replies and trace events are described in docs/REFERENCE.md.")
        return 0
    url = f"{cfg.agent_url.rstrip('/')}/agents/{a.id}"
    print(f"# Agent {a.id}: start OmegaClaw on its websocket channel, pointed at this harness.\n")
    print("# Docker, with the launcher in the Omega repository (scripts/omega):")
    print(f"env WS_URL={url} WS_TOKEN={a.token} ./scripts/omega start -t websocket -p <provider> -d <image>\n")
    print("# Standalone, from the PeTTa root:")
    host_url = url.replace("host.docker.internal", "localhost")
    print(f"sh run.sh run.metta commchannel=websocket WS_URL={host_url} WS_TOKEN={a.token} 2>> omega.log\n")
    print("# The agent can only use one channel: while it points here it is not on Telegram, IRC or Slack.")
    return 0


def _api(cfg, path: str, body: dict | None = None):
    url = f"http://{cfg.console_host}:{cfg.console_port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def cmd_serve(args) -> int:
    from .server import serve

    cfg = load(args.config)
    h, httpd = serve(cfg, with_baselines=not args.no_baselines)
    print(f"omegaeval {__version__}")
    print(f"  store     {cfg.store}")
    print(f"  gateway   ws://{cfg.gateway_host}:{h.gateway.port}   (agents dial {cfg.agent_url}/agents/<id>)")
    print(f"  console   http://{cfg.console_host}:{cfg.console_port}")
    for a in cfg.agents.values():
        if a.kind == "omega":
            print(f"  agent     {a.id:<12} run `omegaeval connect {a.id} -c {args.config}` for its start command")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("stopped. Episodes in progress are marked interrupted, and not graded, when the harness next starts.",
              file=sys.stderr)
    return 0


def cmd_run(args) -> int:
    cfg = load(args.config)
    if args.local:
        return _run_here(cfg, args)
    body = {"subject": args.subject, "env": _env(cfg, args.env), "task": args.task, "seeds": parse_seeds(args.seeds),
            "variant": args.variant, "memory_mode": args.memory, "note": args.note}
    try:
        out = _api(cfg, "/api/episodes", body)
    except urllib.error.URLError as exc:
        print(f"No harness at http://{cfg.console_host}:{cfg.console_port} ({exc.reason}). Start one with: "
              f"omegaeval serve -c {args.config}", file=sys.stderr)
        return 2
    for job in out["queued"]:
        print(f"queued {job['id']}: {job['subject']} on {job['env']}/{job['task']} seed {job['seed']}")
    return 0


def _run_here(cfg, args) -> int:
    """Play episodes in this process, one after another, and exit: no server needed."""
    from .server import Harness

    from .connectors import preflight, transport_of

    env = _env(cfg, args.env)
    try:
        subject = cfg.subject(args.subject) if not args.subject.startswith("baseline-") else None
    except KeyError as exc:
        raise SystemExit(str(exc).strip("'\""))
    for m in subject.members if subject is not None else []:
        if transport_of(m) != "websocket":
            ok, detail = preflight(m)
            if not ok:
                raise SystemExit(f"agent {m.id}: {detail}")
    if subject is not None and all(transport_of(m) != "websocket" for m in subject.members):
        cfg.gateway_port = 0                      # nobody dials in, so any free port will do
    try:
        h = Harness(cfg).start(with_baselines=args.subject.startswith("baseline-"))
    except RuntimeError as exc:
        raise SystemExit(f"{exc}. If omegaeval serve is running, queue the episodes on it instead: "
                         f"leave out --local.")
    errors = 0
    try:
        for seed in parse_seeds(args.seeds):
            r = h.runner.run(args.subject, env, args.task, seed, variant=args.variant, memory_mode=args.memory,
                             note=args.note)
            verdict = "void" if r["void"] else "held for a judge" if r["needs_judge"] else \
                "solved" if r["success"] else r["status"].replace("_", " ")
            print(f"{args.subject} {env} seed {seed}: {verdict}, {r['steps']} steps  ->  {r['run_id']}/{r['attempt']}")
            errors += r["status"] == "error"
    finally:
        h.gateway.stop()
    return 1 if errors else 0


def cmd_baseline(args) -> int:
    from .server import Harness

    cfg = load(args.config, allow_missing=True)
    cfg.gateway_port = 0
    cfg.grace_s = 0
    h = Harness(cfg).start(with_baselines=True)
    subject = f"baseline-{args.policy}"
    env = _env(cfg, args.env)
    for seed in parse_seeds(args.seeds):
        r = h.runner.run(subject, env, args.task, seed, note=args.note)
        print(f"{subject} {env} seed {seed}: {r['status']}, {r['steps']} steps  ->  "
              f"{r['run_id']}/{r['attempt']}")
    return 0


def cmd_status(args) -> int:
    cfg = load(args.config, allow_missing=True)
    try:
        st = _api(cfg, "/api/state")
        for a in st["agents"]:
            print(f"{a['id']:<18} {'connected' if a.get('connected') else 'offline':<10} "
                  f"{a.get('version') or '':<32} {a.get('active') or ''}")
        busy = [j for j in st["jobs"] if j["state"] in ("queued", "running")]
        print(f"{len(busy)} job(s) queued or running")
    except urllib.error.URLError:
        from .store import Store

        store = Store(cfg.store)
        counts = store.env_counts()
        print(f"harness not running; {sum(c['runs'] for c in counts.values())} attempt(s) in {cfg.store}")
        for env, c in sorted(counts.items()):
            print(f"  {env:<24} {c['runs']:>7} attempt(s), last {str(c['last'] or '')[:19]}")
    return 0


def cmd_import_log(args) -> int:
    from .server import import_log_bundle

    text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    bundle = import_log_bundle(text, args.agent)
    out = Path(args.out or (Path(args.file).stem + ".bundle.json"))
    out.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    s = bundle["agents"][args.agent]["meta"]["summary"]
    print(f"{s['iterations']} iterations, {s['llm_calls']} LLM calls from {s['lines_parsed']} log lines -> {out}")
    return 0


def _env(cfg, name: str) -> str:
    """A benchmark id from what a person typed: the id itself, or its label in any case."""
    ids = cfg.env_ids()
    if name in ids:
        return name
    low = name.lower()
    hits = [i for i in ids if i.lower() == low] or \
           [i for i in ids if (cfg.registry()[i].get("label") or "").lower() == low]
    if len(hits) == 1:
        return hits[0]
    raise SystemExit(f"unknown benchmark {name!r}; installed: {', '.join(ids)}")


def _select(store, refs: list[str], where: list[str] | None = None) -> list[dict]:
    """The attempts a person named. Each ref is run_id/attempt, a run id or the start of one, a
    benchmark id such as hle, or key=value (env=hle subject=oma-1 variant=... status=finished).
    Names add up; key=value filters apply to all of them."""
    filters, names = {}, []
    for ref in list(refs or []) + list(where or []):
        if "=" in ref:
            k, _, v = ref.partition("=")
            filters[k.strip()] = v.strip()
        else:
            names.append(ref)
    try:
        rows = store.attempts(**filters)
    except ValueError as exc:
        raise SystemExit(str(exc))
    if not names:
        return rows
    envs = {r.get("env") for r in rows}
    out, seen = [], set()
    for ref in names:
        rid, _, att = ref.partition("/")
        if att:
            hits = [r for r in rows if r["run_id"] == rid and str(r["attempt"]) == att]
        elif rid in envs:
            hits = [r for r in rows if r.get("env") == rid]
        else:
            hits = [r for r in rows if r["run_id"] == rid] or [r for r in rows if r["run_id"].startswith(rid)]
        if not hits:
            raise SystemExit(f"nothing matches {ref}" + (f" with {filters}" if filters else ""))
        for r in hits:
            if (r["run_id"], r["attempt"]) not in seen:
                seen.add((r["run_id"], r["attempt"]))
                out.append(r)
    return out


def cmd_export(args) -> int:
    from .store import Store

    store = Store(load(args.config, allow_missing=True).store)
    if not args.out and not args.html:
        args.out = "omegaeval-export.json"
    rows = [r for r in _select(store, args.runs) if r.get("status") != "running"]
    if len(rows) > args.max:
        raise SystemExit(f"{len(rows)} attempts match. A page with that many runs is too heavy to open; name a "
                         f"benchmark or add filters such as subject=... or seed=..., or raise --max.")
    bundles = [store.bundle(r["run_id"], r["attempt"], with_prompts=not args.redact_prompts) for r in rows]
    from .server import project_links

    doc = {"schema": "omegaeval.export/0", "harness": __version__, "links": project_links(), "runs": bundles}
    if args.html:
        from importlib.resources import files

        page = files("omegaeval").joinpath("console/index.html").read_text(encoding="utf-8")
        marker = '<script type="application/json" id="embedded-runs">{"schema":"omegaeval.export/0","runs":[]}</script>'
        if marker not in page:
            raise SystemExit("console template has no embedded-runs slot")
        payload = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        page = page.replace(marker, f'<script type="application/json" id="embedded-runs">{payload}</script>')
        Path(args.html).write_text(page, encoding="utf-8")
        print(f"{len(bundles)} attempt(s) -> {args.html} (self-contained console)")
    if args.out:
        Path(args.out).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        print(f"{len(bundles)} attempt(s) -> {args.out}{' (prompts removed)' if args.redact_prompts else ''}")
    return 0


def cmd_rebuild(args) -> int:
    from .judging import recompute
    from .omega_trace import TraceBuilder
    from .store import Store, open_log, raw_log

    cfg = load(args.config, allow_missing=True)
    store = Store(cfg.store)
    for r in _select(store, args.runs):
        if r.get("status") == "running":
            continue
        frozen = store.released_in(r["run_id"], r["attempt"])
        if frozen:
            print(f"skipped {r['run_id']}/{r['attempt']}: frozen in release {', '.join(frozen)}")
            continue
        p = store.path(r["run_id"], r["attempt"])
        for adir in (p / "agents").iterdir():
            raw = raw_log(adir) if adir.is_dir() else None
            if raw is not None:
                tb = TraceBuilder()
                with open_log(raw) as fh:
                    tb.feed_many(fh)
                tb.finish()
                (adir / "trace.jsonl").write_text("".join(json.dumps(i, ensure_ascii=False) + "\n"
                                                          for i in tb.iterations), encoding="utf-8")
        recompute(store, cfg, p)
        store.ledger({"event": "rebuilt", "run_id": r["run_id"], "attempt": r["attempt"]})
        print(f"rebuilt {r['run_id']}/{r['attempt']}")
    return 0


def cmd_judge(args) -> int:
    from . import judging
    from .store import Store

    cfg = load(args.config, allow_missing=True)
    store = Store(cfg.store)
    if args.verdict and args.command:
        raise SystemExit("give a --verdict or a --command, not both")
    if not args.verdict and not args.command:
        cases = judging.held(store)
        if args.runs:
            wanted = {(r["run_id"], int(r["attempt"])) for r in _select(store, args.runs)}
            cases = [c for c in cases if (c["run_id"], c["attempt"]) in wanted]
        for c in cases:
            print(f"{c['run_id']}/{c['attempt']}  {c['benchmark']}  {c['question_id'] or ''}")
            print(f"    gold    {str(c['gold'])[:160]}")
            print(f"    answer  {str(c['answer'])[:160]}")
        print(f"{len(cases)} answer(s) held for a judge")
        return 0
    if not args.by:
        raise SystemExit("name the judge with --by: a person, or the model and prompt behind the command")
    if args.command:
        held = {(c["run_id"], c["attempt"]) for c in judging.held(store)}
        targets = [(r["run_id"], int(r["attempt"])) for r in _select(store, args.runs)
                   if (r["run_id"], int(r["attempt"])) in held] if args.runs else sorted(held)
    elif args.runs:
        targets = [(r["run_id"], r["attempt"]) for r in _select(store, args.runs)]
    else:
        raise SystemExit("name the attempts to judge, e.g. hle-1a2b3c4d5e6f/1")
    failed = 0
    for run_id, att in targets:
        try:
            if args.verdict:
                res = judging.record(store, cfg, run_id, att, args.verdict, args.by, args.reason or "")
            else:
                v = judging.ask(args.command, judging.case(store, run_id, att), args.timeout)
                res = judging.record(store, cfg, run_id, att, v["verdict"], args.by, v["reason"], via="command",
                                     detail={"command": args.command, **(v["detail"] or {})})
            print(f"{run_id}/{att}: {res['outcome']['judged']['verdict']}, judged by {args.by}")
        except (ValueError, PermissionError, RuntimeError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            failed += 1
            print(f"{run_id}/{att}: not judged: {exc}")
    return 1 if failed else 0


def cmd_verify(args) -> int:
    from .store import Store

    store = Store(load(args.config, allow_missing=True).store)
    names = args.releases or []
    problems = []
    if not names:
        problems += store.verify()
        names = sorted(store.releases())
    for name in names:
        problems += [f"release {name}: {p}" if not p.startswith("release ") else p for p in store.verify(name)]
    for p in problems:
        print(p)
    what = f"{len(names)} release(s)" if args.releases else f"every attempt and {len(names)} release(s)"
    print(f"{len(problems)} problem(s) in {what}" if problems else f"all checksums match: {what}")
    return 1 if problems else 0


def cmd_release(args) -> int:
    from .store import Store

    store = Store(load(args.config, allow_missing=True).store)
    rows = [r for r in _select(store, args.runs, args.where) if r.get("status") == "finished"]
    if not rows:
        raise SystemExit("no finished attempts match; nothing released")
    out = store.release(args.name, rows, args.note)
    print(f"release {args.name}: {len(rows)} attempt(s) -> {out / 'release.json'}")
    return 0


def cmd_replay(args) -> int:
    from .checks import replay
    from .store import Store, read_jsonl

    cfg = load(args.config, allow_missing=True)
    store = Store(cfg.store)
    bad = 0
    for r in _select(store, args.runs):
        p = store.path(r["run_id"], r["attempt"])
        manifest = json.loads((p / "manifest.json").read_text())
        env = cfg.make_env(manifest["identity"]["env"])
        out = replay(env, manifest, read_jsonl(p / "env.jsonl"))
        store.ledger({"event": "replayed", "run_id": r["run_id"], "attempt": r["attempt"], "exact": out["ok"],
                      "problems": len(out["problems"])})
        print(f"{r['run_id']}/{r['attempt']}: {'exact' if out['ok'] else 'DIFFERS'} over {out['steps_replayed']} steps")
        for prob in out["problems"][:5]:
            print(f"    {prob}")
        bad += 0 if out["ok"] else 1
    return 1 if bad else 0


def cmd_conform(args) -> int:
    from .checks import conform

    cfg = load(args.config, allow_missing=True)
    args.env = _env(cfg, args.env)
    results = conform(cfg.env_factory(args.env), parse_seeds(args.seeds), args.task)
    for name, ok, detail in results:
        print(f"{'pass' if ok else 'FAIL'}  {name}  ({detail})")
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"{len(results) - failed}/{len(results)} checks passed for {args.env}")
    return 1 if failed else 0


def cmd_benchmarks(args) -> int:
    from .store import Store

    cfg = load(args.config, allow_missing=True)
    counts = Store(cfg.store).env_counts() if cfg.store.exists() else {}
    print(f"{'ID':<18} {'LABEL':<14} {'KIND':<12} {'WORLD PANEL':<12} {'RUNS':>6}  NAME  [FROM]")
    for eid in cfg.env_ids():
        d = cfg.describe(eid)
        runs = counts.get(eid, {}).get("runs", 0)
        if d.get("error"):
            print(f"{eid:<18} {d['label'][:14]:<14} {'broken':<12} {'':<12} {runs:>6}  {d['error'][:90]}  [{d['origin']}]")
        else:
            print(f"{eid:<18} {d['label'][:14]:<14} {d.get('kind', '?'):<12} {d.get('renderer') or 'transcript':<12} "
                  f"{runs:>6}  {d['name']}  [{d['origin']}]")
    return 0


def cmd_bundle(args) -> int:
    import hashlib
    import tarfile

    from .store import Store

    store = Store(load(args.config, allow_missing=True).store)
    rel = store.root / "releases" / args.name
    if not (rel / "release.json").exists():
        raise SystemExit(f"no release {args.name!r}; make one with omegaeval release {args.name} <attempts>")
    problems = store.verify(args.name)
    if problems:
        raise SystemExit("the release does not verify, so it cannot be bundled:\n  " + "\n  ".join(problems[:10]))
    doc = json.loads((rel / "release.json").read_text(encoding="utf-8"))
    withheld = ["omega.log.gz", "omega.log"] if args.withhold_logs else []
    out = Path(args.out or f"{args.name}.tar.gz")
    info = {"schema": "omegaeval.bundle/1", "release": args.name, "harness": __version__, "withheld": withheld}
    with tarfile.open(out, "w:gz") as tar:
        def add_bytes(name: str, data: bytes) -> None:
            import io

            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))

        add_bytes("bundle.json", json.dumps(info, indent=1).encode())
        tar.add(store.root / "store.json", arcname="store.json")
        for f in ("release.json", "release.sha256"):
            tar.add(rel / f, arcname=f"releases/{args.name}/{f}")
        for a in doc.get("attempts", []):
            base = store.root / a["path"]
            for f in sorted(base.rglob("*")):
                if f.is_file() and f.name not in withheld:
                    tar.add(f, arcname=f"{a['path']}/{f.relative_to(base)}")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"{out}  {len(doc.get('attempts', []))} attempt(s), {out.stat().st_size / 1e6:.1f} MB")
    print(f"sha256 {digest}")
    if not withheld:
        print("It holds the agents' raw logs, whose prompts can carry memory from other conversations. "
              "Read them before publishing, or bundle again with --withhold-logs.")
    return 0


def cmd_add(args) -> int:
    import tomllib
    import urllib.request

    from .registry import check_benchmark
    from .server import project_links

    src = args.registry or ((project_links().get("demo") or "").rstrip("/") + "/registry.json")
    if src == "/registry.json":
        raise SystemExit("no registry address is known; pass --registry with a URL or a registry.json file")
    try:
        if src.startswith(("https://", "http://")):
            with urllib.request.urlopen(src, timeout=30) as resp:
                data = json.loads(resp.read())
        else:
            data = json.loads(Path(src).read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"could not read the registry at {src}: {exc}")
    want = args.benchmark.lower()
    entry = next((b for b in data.get("benchmarks", []) if b["id"] == want or b.get("label", "").lower() == want), None)
    if entry is None:
        raise SystemExit(f"{args.benchmark!r} is not in the registry at {src}")
    errors = check_benchmark(entry)
    if errors:
        raise SystemExit(f"the registry entry for {entry['id']} does not pass the checks: " + "; ".join(errors))
    path = Path(args.config)
    current = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if entry["id"] in (current.get("environments") or {}):
        raise SystemExit(f"{path} already has [environments.{entry['id']}]")
    block = entry["config"].rstrip("\n") + "\n"
    head = f"[environments.{entry['id']}]\n"
    table = tomllib.loads(block)["environments"][entry["id"]]
    extra = "".join(f"{k} = {json.dumps(entry[k], ensure_ascii=False)}\n" for k in ("label", "name") if k not in table)
    block = block.replace(head, head + extra, 1)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"\n# {entry['name']}, from the registry: {entry.get('homepage', '')}\n{block}")
    print(f"added [environments.{entry['id']}] to {path}:\n\n{block}")
    if "path" in (table.get("options") or {}):
        print(f"Point options.path at your copy of the data; the registry shows {table['options']['path']}.")
    if entry.get("install"):
        print(f"Install its adapter first: {entry['install']}")
    print(f"Then check it: omegaeval conform {entry['id']}")
    return 0


def cmd_merge(args) -> int:
    from .store import Store

    store = Store(load(args.config, allow_missing=True).store)
    total = 0
    for other in args.stores:
        if not Path(other, "index.jsonl").exists():
            raise SystemExit(f"{other} is not an omegaeval store (no index.jsonl)")
        c = store.merge(other)
        total += c["added"]
        print(f"{other}: {c['added']} added ({c['renumbered']} renumbered), {c['skipped']} already here, "
              f"{c['running']} still running and left out")
    print(f"{total} attempt(s) merged into {store.root}")
    return 0


def cmd_reindex(args) -> int:
    from .store import Store

    store = Store(load(args.config, allow_missing=True).store)
    n = store.reindex()
    print(f"index rebuilt from {n} ledger event(s): {store.count()} attempt(s) in {store.db_path}")
    return 0


def cmd_new_benchmark(args) -> int:
    from .templates import render

    folder = Path(args.dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{args.name.replace('-', '_')}.py"
    if path.exists():
        print(f"{path} already exists; nothing written.")
        return 1
    path.write_text(render(args.name, args.kind), encoding="utf-8")
    print(f"Wrote {path}. It already passes conformance; replace the TODO parts with your benchmark.")
    print(f"  omegaeval conform {args.name}")
    print(f"  omegaeval baseline oracle {args.name} --seeds 1-3")
    print("Benchmarks in ./benchmarks are found automatically; elsewhere, list the folder in "
          "[harness] benchmark_dirs.")
    return 0


def cmd_doctor(args) -> int:
    import shutil
    import socket
    import subprocess

    from .checks import conform
    from .connectors import preflight

    problems = 0

    def say(ok: bool | None, what: str, detail: str = "") -> None:
        nonlocal problems
        mark = {True: "ok  ", False: "FAIL", None: "warn"}[ok]
        problems += 1 if ok is False else 0
        print(f"{mark}  {what}{'  ' + detail if detail else ''}")

    from .store import _synced_folder

    cfg = load(args.config, allow_missing=True)
    say(True if cfg.path else None, "config", str(cfg.path) if cfg.path else
        f"{args.config} not found: reference runs work, agents need one (omegaeval init)")
    for key in cfg.unknown:
        say(None, f"config key {key}", "is not read by omegaeval; check the spelling against docs/REFERENCE.md")
    if _synced_folder(cfg.store):
        say(None, "store location", f"{cfg.store} is in a cloud-synced folder. Keep the store on a local disk; "
            "sync can split the ledger. Combine stores from several machines with omegaeval merge.")
    try:
        cfg.store.mkdir(parents=True, exist_ok=True)
        probe = cfg.store / ".doctor"
        probe.write_text("ok")
        probe.unlink()
        say(True, "store is writable", str(cfg.store))
    except OSError as exc:
        say(False, "store is writable", str(exc))
    try:
        st = _api(cfg, "/api/state")
        say(True, "a harness is already running", f"gateway port {st['gateway_port']}")
    except Exception:
        with socket.socket() as sock:
            try:
                sock.bind((cfg.gateway_host, cfg.gateway_port))
                say(True, "gateway port is free", str(cfg.gateway_port))
            except OSError as exc:
                say(False, "gateway port is free", f"{cfg.gateway_port}: {exc}")
    docker = shutil.which("docker")
    from .connectors import transport_of

    for a in cfg.agents.values():
        transport = transport_of(a)
        if transport != "websocket":
            ok, detail = preflight(a)
            say(ok, f"agent {a.id}: {transport}", detail)
            continue
        say(bool(a.token) and "replace-with" not in a.token and len(a.token) >= 16, f"agent {a.id}: token",
            "set" if a.token and "replace-with" not in a.token else "replace the placeholder with a long random token")
        if a.kind != "omega":
            say(None, f"agent {a.id}: cognition", "comes from the trace frames the agent sends, if it sends them")
            continue
        for label, spec in (("logs", a.logs), ("memory", a.memory)):
            if not spec:
                say(None if label == "memory" else False, f"agent {a.id}: {label}", "not configured")
            elif "docker" in spec:
                if not docker:
                    say(False, f"agent {a.id}: {label}", "docker is not installed on this machine")
                    continue
                cmd = ["docker"] + (["-H", spec["host"]] if spec.get("host") else []) + \
                      ["inspect", "-f", "{{.State.Running}}", spec["docker"]]
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
                running = out.returncode == 0 and out.stdout.strip() == "true"
                say(running, f"agent {a.id}: {label}", f"container {spec['docker']} "
                    + ("is running" if running else (out.stderr.strip()[:120] or "is not running")))
            elif "file" in spec:
                f = Path(spec["file"])
                say(f.exists() or f.parent.is_dir(), f"agent {a.id}: {label}",
                    f"{f} {'exists' if f.exists() else 'will be created by the agent' if f.parent.is_dir() else 'has no parent folder'}")
                if label == "logs":
                    say(None, f"agent {a.id}: evidence grade", "file logs can be written by the agent; "
                        "docker logs cannot")
            elif "home" in spec:
                h = Path(spec["home"])
                hist = h / "repos/Omega/memory/history.metta"
                say(hist.exists(), f"agent {a.id}: {label}", f"{hist} {'found' if hist.exists() else 'not found'}")
    for eid in cfg.env_ids():
        try:
            res = conform(cfg.env_factory(eid), [1, 2])
            failed = [n for n, ok, _ in res if not ok]
            say(not failed, f"benchmark {eid}", "conforms" if not failed else "fails: " + "; ".join(failed))
        except Exception as exc:
            say(False, f"benchmark {eid}", str(exc)[:160])
    print(f"{problems} problem(s)" if problems else "all checks passed")
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    """The command line. docs/REFERENCE.md is generated from it (python -m omegaeval.docgen)."""
    p = argparse.ArgumentParser(prog="omegaeval", description="Benchmark OmegaClaw agents and hives across games.")
    p.add_argument("--version", action="version", version=f"omegaeval {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        if name != "init" and name != "import-log":
            sp.add_argument("-c", "--config", default="omegaeval.toml", help="the config file")
        return sp

    s = add("init", cmd_init, "write an omegaeval.toml template")
    s.add_argument("--dir", default=".", help="folder to write it in")
    s = add("connect", cmd_connect, "print the command that points an agent at the harness")
    s.add_argument("agent", help="an agent id from omegaeval.toml")
    s = add("serve", cmd_serve, "run the gateway, episode queue and console")
    s.add_argument("--no-baselines", action="store_true", help="do not start the random and oracle reference agents")
    s = add("run", cmd_run, "queue episodes for an agent or hive on a running harness")
    s.add_argument("subject", metavar="agent-or-hive", help="an agent or hive id from omegaeval.toml")
    s.add_argument("env", metavar="benchmark", help="a benchmark id, or its label")
    s.add_argument("--task", default=None, help="default: the benchmark's first task")
    s.add_argument("--seeds", default="1", help="seeds to play, e.g. 1-5 or 1,3,7")
    s.add_argument("--variant", default="", help="free text naming the one thing changed, e.g. reasoningMode=low")
    s.add_argument("--memory", default="carry", choices=["carry", "fresh"],
                   help="keep the agent's memory from earlier episodes, or record that it was reset")
    s.add_argument("--note", default="", help="free text kept in the manifest")
    s.add_argument("--local", action="store_true",
                   help="play the episodes in this process and wait for them, instead of queuing them on a harness")
    s = add("baseline", cmd_baseline, "run a reference policy without a server")
    s.add_argument("policy", choices=["random", "oracle"], help="random is the floor, oracle the ceiling")
    s.add_argument("env", metavar="benchmark", help="a benchmark id, or its label")
    s.add_argument("--task", default=None, help="default: the benchmark's first task")
    s.add_argument("--seeds", default="1", help="seeds to play, e.g. 1-5 or 1,3,7")
    s.add_argument("--note", default="", help="free text kept in the manifest")
    add("status", cmd_status, "agents, connections and queue")
    s = add("import-log", cmd_import_log, "turn an Omega log file into a console bundle")
    s.add_argument("file", metavar="log-file", help="an OmegaClaw log file")
    s.add_argument("--agent", default="imported", help="the agent name to show")
    s.add_argument("--out", help="where to write the bundle; default <log-file>.bundle.json")
    s = add("export", cmd_export, "write attempts to a JSON file, or into a self-contained console page")
    s.add_argument("runs", nargs="*", metavar="attempts", help=SELECT_HELP + "; default all")
    s.add_argument("--max", type=int, default=1000, help="refuse to export more attempts than this")
    s.add_argument("--out", help="a JSON file the console can open")
    s.add_argument("--html", help="a self-contained console page with the runs embedded")
    s.add_argument("--redact-prompts", action="store_true", help="drop prompt text and raw completions")
    s = add("rebuild", cmd_rebuild, "re-parse raw logs with this parser version; evidence is untouched")
    s.add_argument("runs", nargs="*", metavar="attempts", help=SELECT_HELP + "; default all")
    s = add("replay", cmd_replay, "re-run the environment with the recorded actions and check every receipt")
    s.add_argument("runs", nargs="*", metavar="attempts", help=SELECT_HELP + "; default all")
    s = add("conform", cmd_conform, "the checks a benchmark adapter must pass")
    s.add_argument("env", metavar="benchmark", help="a benchmark id, or its label")
    s.add_argument("--seeds", default="1-3", help="seeds to check")
    s.add_argument("--task", help="default: the benchmark's first task")
    s = add("new-benchmark", cmd_new_benchmark, "write a working adapter template to start from")
    s.add_argument("name", help="the benchmark id: lowercase letters, digits, - or _")
    s.add_argument("--kind", choices=["interactive", "qa", "memory"], default="interactive", help="the benchmark shape")
    s.add_argument("--dir", default="benchmarks", help="folder to write the file in")
    add("doctor", cmd_doctor, "check the config, agents, log sources, memory paths and benchmarks")
    add("benchmarks", cmd_benchmarks, "list installed benchmarks and where each comes from")
    s = add("judge", cmd_judge, "list answers held for a judge, or record a verdict from a person or a command")
    s.add_argument("runs", nargs="*", metavar="attempts", help=SELECT_HELP + "; default every held answer")
    s.add_argument("--verdict", choices=["correct", "wrong"], help="a person's verdict on the named attempts")
    s.add_argument("--command", help="an external judge: reads the case as JSON on stdin, prints a JSON verdict")
    s.add_argument("--by", help="who judged: a person, or the model and prompt behind the command")
    s.add_argument("--reason", default="", help="why, kept with the verdict")
    s.add_argument("--timeout", type=float, default=300, help="seconds the judge command may take per answer")
    s = add("verify", cmd_verify, "check every file against its checksums, or the named releases")
    s.add_argument("releases", nargs="*", metavar="release", help="release names; default every attempt and release")
    s = add("merge", cmd_merge, "copy the finished attempts of other stores into this one")
    s.add_argument("stores", nargs="+", metavar="store", help="paths of other omegaeval stores")
    add("reindex", cmd_reindex, "rebuild the query index from the ledger and result files")
    s = add("bundle", cmd_bundle, "pack a release into one file anyone can verify, for the registry")
    s.add_argument("name", help="the release name")
    s.add_argument("--out", help="the file to write; default <name>.tar.gz")
    s.add_argument("--withhold-logs", action="store_true",
                   help="leave out raw agent logs, whose prompts can carry memory from other conversations")
    s = add("add", cmd_add, "add a registered benchmark's config block to omegaeval.toml")
    s.add_argument("benchmark", help="the benchmark id or label in the registry")
    s.add_argument("--registry", help="a registry.json URL or file; default the project's site")
    s = add("release", cmd_release, "freeze a set of finished attempts with checksums, for citation")
    s.add_argument("name", help="the release name; a release is never overwritten")
    s.add_argument("runs", nargs="*", metavar="attempts", help=SELECT_HELP + "; default all")
    s.add_argument("--where", action="append", default=[], help="a key=value filter, e.g. env=hle; repeatable")
    s.add_argument("--note", default="", help="free text kept in the release")

    return p


def _sigterm(signum, frame):
    raise KeyboardInterrupt


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    previous = None
    try:
        previous = signal.signal(signal.SIGTERM, _sigterm)   # docker stop, a cancelled CI job: stop as Ctrl-C does
    except ValueError:
        pass                                                 # not the main thread, as when embedded
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("stopped. An episode in progress is recorded as interrupted and not graded.", file=sys.stderr)
        return 130
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
