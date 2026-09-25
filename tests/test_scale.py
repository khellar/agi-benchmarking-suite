"""Human-chosen benchmark names, the query index, combining stores, compressed logs, the paged API
and datasets too large to load whole."""
import gzip
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from omegaeval.cli import _select, main
from omegaeval.config import AgentSpec, Config, check_id, load
from omegaeval.server import Harness, make_handler
from omegaeval.store import Store

from test_benchmarks import ToolUser

QA = [{"id": "a", "question": "What is 2 + 2?", "answer": "4"},
      {"id": "b", "question": "What is 3 + 3?", "answer": "6"}]


def _config(tmp_path, extra: str = "") -> Path:
    (tmp_path / "q.jsonl").write_text("\n".join(json.dumps(r) for r in QA) + "\n")
    cfg = tmp_path / "omegaeval.toml"
    cfg.write_text(f'''[harness]
store = "store"

[environments.hle]
adapter = "omegaeval.envs.qa:QADataset"
label = "HLE"
name = "Humanity's Last Exam (sample)"

[environments.hle.options]
path = "q.jsonl"
{extra}''')
    return cfg


def test_benchmark_ids_are_human_names_in_run_ids_and_selection(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _config(tmp_path)
    assert main(["baseline", "oracle", "HLE", "--seeds", "1-2"]) == 0          # the label works as a name
    assert main(["baseline", "random", "qa-sample", "--seeds", "1"]) == 0
    out = capsys.readouterr().out
    assert "hle-" in out and "qa-sample-" in out
    store = Store(tmp_path / "store")
    rows = store.attempts()
    assert {r["env"] for r in rows} == {"hle", "qa-sample"}
    assert all(r["run_id"].startswith(r["env"] + "-") for r in rows)
    assert len(_select(store, ["hle"])) == 2                                     # a benchmark id
    assert len(_select(store, ["subject=baseline-oracle"])) == 2                # a filter
    assert len(_select(store, ["hle", "seed=2"])) == 1                           # both
    first = rows[0]["run_id"]
    assert _select(store, [first[:len(first) - 4]])[0]["run_id"] == first      # the start of a run id
    with pytest.raises(SystemExit):
        _select(store, ["colour=blue"])
    assert main(["benchmarks"]) == 0
    listing = capsys.readouterr().out
    assert "HLE" in listing and "Humanity's Last Exam (sample)" in listing


def test_bad_ids_and_unknown_keys_are_reported(tmp_path, monkeypatch, capsys):
    assert check_id("hle") is None and check_id("arc-agi-3") is None and check_id("free_civ") is None
    assert check_id("HLE") and check_id("my bench") and check_id("a" * 41) and check_id("")
    monkeypatch.chdir(tmp_path)
    _config(tmp_path, extra='\n[environments.FreeCiv]\nadapter = "x:Y"\n\n[agents.oma-1]\ntoken = "' + "t" * 32
            + '"\nlogs = { file = "omega.log" }\nlabl = "typo"\n')
    cfg = load(tmp_path / "omegaeval.toml")
    assert "agents.oma-1.labl" in cfg.unknown
    assert cfg.describe("FreeCiv")["error"].startswith("benchmark id 'FreeCiv'")
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "config key agents.oma-1.labl" in out and "benchmark FreeCiv" in out


def test_new_benchmark_files_appear_without_a_restart(tmp_path):
    folder = tmp_path / "benchmarks"
    folder.mkdir()
    cfg = Config(path=None, store=tmp_path / "s", benchmark_dirs=[folder])
    assert "later" not in cfg.env_ids()
    (folder / "later.py").write_text("from omegaeval.envs.qa import QADataset\n\n\nclass Later(QADataset):\n"
                                     "    id = 'later'\n    label = 'Later'\n"
                                     "    RECORDS = [{'id': 'x', 'question': 'Q?', 'answer': 'A'}]\n")
    assert "later" in cfg.env_ids() and cfg.describe("later")["label"] == "Later"


def test_listing_benchmarks_does_not_reload_unchanged_data(tmp_path, monkeypatch):
    import omegaeval.envs.qa as qa

    _config(tmp_path)
    cfg = load(tmp_path / "omegaeval.toml")
    calls = []
    real = qa.QADataset.records
    monkeypatch.setattr(qa.QADataset, "records", lambda self: calls.append(1) or real(self))
    cfg.describe("hle")
    n = len(calls)
    for _ in range(5):
        cfg.describe("hle")
    assert len(calls) == n                      # cached while the file is unchanged
    time.sleep(0.01)
    (tmp_path / "q.jsonl").write_text(json.dumps(QA[0]) + "\n")
    assert cfg.describe("hle")["questions"] == 1


def test_the_index_follows_the_ledger_and_rebuilds_from_files(rig):
    cfg, store, gw, runner, baseline = rig
    orc = baseline("oracle")
    for seed in (1, 2, 3):
        runner.run(orc, "qa-sample", "all", seed)
    runner.run(baseline("random"), "keydoor", "small", 1)
    assert store.count() == 4 and store.count(env="qa-sample") == 3
    assert [r["seed"] for r in store.attempts(env="qa-sample", limit=2)] == [3, 2]
    res = store.results(env="qa-sample")
    assert len(res) == 3 and all(r["result"]["outcome"]["success"] for r in res)
    assert len(json.dumps(res[0])) < 2500                        # compact: only what comparisons read
    prog = {(p["env"], p["subject"]): p for p in store.progress()}
    assert prog[("qa-sample", "baseline-oracle")]["solved"] == 3
    other = Store(store.root)                                    # another process appends to the ledger
    other.ledger({"event": "replayed", "run_id": res[0]["run_id"], "attempt": res[0]["attempt"], "exact": True})
    assert store.attempts(exact=True)[0]["run_id"] == res[0]["run_id"]
    before = [(r["run_id"], r["attempt"], r["status"], r["success"]) for r in store.attempts()]
    store.db_path.unlink()
    fresh = Store(store.root)
    assert [(r["run_id"], r["attempt"], r["status"], r["success"]) for r in fresh.attempts()] == before
    assert fresh.reindex() > 0 and fresh.count() == 4


def test_stores_from_several_machines_merge_without_losing_anything(rig, tmp_path):
    cfg, store, gw, runner, baseline = rig
    orc = baseline("oracle")
    runner.run(orc, "qa-sample", "all", 1)
    runner.run(orc, "qa-sample", "all", 2)
    import dataclasses

    from omegaeval.episode import EpisodeRunner
    elsewhere = Store(tmp_path / "other")                        # same run id and attempt number, other episode
    runner2 = EpisodeRunner(dataclasses.replace(cfg, store=elsewhere.root), elsewhere, gw)
    runner2.hooks[orc] = runner.hooks[orc]
    runner2.run(orc, "qa-sample", "all", 1)
    runner2.run(orc, "qa-sample", "all", 3)
    counts = store.merge(elsewhere.root)
    assert counts == {"added": 2, "renumbered": 1, "skipped": 0, "running": 0}
    assert store.merge(elsewhere.root)["skipped"] == 2           # merging again changes nothing
    seed1 = store.attempts(env="qa-sample", seed=1)
    assert sorted(r["attempt"] for r in seed1) == [1, 2]
    moved = json.loads((store.path(seed1[0]["run_id"], 2) / "manifest.json").read_text())
    assert moved["attempt"] == 2 and moved["merged_from"]["attempt"] == 1
    assert store.verify() == [] and store.count() == 4
    with pytest.raises(ValueError):
        store.merge(store.root)


def test_raw_logs_are_compressed_and_still_readable(rig, tmp_path):
    cfg, store, gw, runner, baseline = rig
    qfile = tmp_path / "q.json"
    qfile.write_text(json.dumps([{"id": "a", "question": "Name the largest moon of Saturn.", "answer": "Titan"}]))
    cfg.environments["moons"] = {"adapter": "omegaeval.envs.qa:QADataset", "options": {"path": str(qfile)}}
    log_path = tmp_path / "omega.log"
    cfg.agents["agent"] = AgentSpec(id="agent", token="tok-agent", kind="omega", logs={"file": str(log_path)})
    gw.add("agent", "tok-agent")
    agent = ToolUser(f"ws://127.0.0.1:{gw.port}/agents/agent", "tok-agent", log_path)
    agent.start()
    agent.ready.wait(5)
    time.sleep(0.2)
    out = runner.run("agent", "moons", "all", 1)
    agent.stop_flag.set()
    adir = Path(out["path"]) / "agents" / "agent"
    assert (adir / "omega.log.gz").exists() and not (adir / "omega.log").exists()
    with gzip.open(adir / "omega.log.gz", "rt") as fh:
        assert "HUMAN-MSG" in fh.read()
    assert "iteration" in store.raw_line(out["run_id"], out["attempt"], "agent", 0)
    b = store.bundle(out["run_id"], out["attempt"])
    assert b["agents"]["agent"]["raw_lines"] > 0 and b["agents"]["agent"]["iterations"]
    assert store.verify() == []


def test_memory_benchmarks_in_json_lines_load_one_case_at_a_time(tmp_path):
    from omegaeval.envs.chatmemory import ChatMemory

    lines = []
    for i in range(20):
        lines.append(json.dumps({"question_id": f"q{i}" + ("_abs" if i == 19 else ""), "question_type": "multi-session",
                                 "question": f"What is item {i}?", "answer": f"item-{i}",
                                 "haystack_session_ids": ["s0"], "haystack_dates": ["2023/05/20"],
                                 "haystack_sessions": [[{"role": "user", "content": f"Remember item-{i}.",
                                                         "has_answer": True}]], "answer_session_ids": ["s0"]}))
    f = tmp_path / "cases.jsonl"
    f.write_text("\n".join(lines) + "\n")
    env = ChatMemory(path=str(f))
    assert all("sessions" not in c for c in env.cases())                     # only an index is kept
    d = env.describe()
    assert d["cases"] == 20 and "abstention" in d["tasks"]
    obs = env.reset("all", 8)
    assert "Remember item-7." in obs.text and "has_answer" not in obs.text
    assert env.case["evidence_sessions"] == [0]


def test_the_console_api_pages_and_filters(rig):
    cfg, store, gw, runner, baseline = rig
    orc = baseline("oracle")
    for seed in range(1, 6):
        runner.run(orc, "qa-sample", "all", seed)
    runner.run(orc, "keydoor", "small", 1)
    h = Harness.__new__(Harness)
    h.cfg, h.store, h.gateway, h.runner = cfg, store, gw, runner
    h.jobs, h._jobs_lock = [], threading.Lock()
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(h))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    get = lambda p: json.loads(urllib.request.urlopen(base + p, timeout=10).read())
    try:
        page = get("/api/runs?env=qa-sample&limit=2")
        assert page["total"] == 5 and len(page["rows"]) == 2
        assert get("/api/runs?q=keydoor")["total"] == 1
        assert len(get("/api/results?env=qa-sample")) == 5 and get("/api/results?env=keydoor")[0]["identity"]["env"] == "keydoor"
        prog = get("/api/progress?env=qa-sample")
        assert prog[0]["n"] == 5 and prog[0]["solved"] == 5
        state = get("/api/state")
        assert state["run_counts"]["qa-sample"]["runs"] == 5 and state["ledger_rev"] > 0
        assert state["environments"]["qa-sample"]["label"] == "QA sample"
        with pytest.raises(urllib.error.HTTPError) as err:
            get("/api/runs?colour=blue")
        assert err.value.code == 400
        refused = []
        for path, headers, data in (("/api/state", {"Host": "evil.example:8780"}, None),
                                    ("/api/episodes", {"Origin": "http://evil.example", "Content-Type": "application/json"},
                                     b'{"subject": "x", "env": "keydoor"}'),
                                    ("/api/episodes", {"Content-Type": "text/plain"}, b'{"subject": "x", "env": "keydoor"}')):
            req = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if data else "GET")
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req, timeout=10)
            refused.append(err.value.code)
        assert refused == [403, 403, 415]
    finally:
        httpd.shutdown()
