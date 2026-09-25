"""The benchmark shapes, the plugin paths, and the integrity guards."""
import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from omegaeval.checks import conform, replay
from omegaeval.config import AgentSpec, Config
from omegaeval.store import Store, read_jsonl
from omegaeval.templates import render


@pytest.mark.parametrize("env_id", ["keydoor", "qa-sample", "memory-sample"])
def test_builtin_benchmarks_conform(env_id):
    cfg = Config(path=None, store=Path("unused"))
    failed = [(n, d) for n, ok, d in conform(cfg.env_factory(env_id), [1, 2, 3]) if not ok]
    assert not failed, failed


@pytest.mark.parametrize("kind", ["interactive", "qa", "memory"])
def test_scaffolds_conform_and_are_discovered(tmp_path, kind):
    folder = tmp_path / "benchmarks"
    folder.mkdir()
    name = f"my-{kind}-bench"
    (folder / f"my_{kind}_bench.py").write_text(render(name, kind), encoding="utf-8")
    cfg = Config(path=None, store=tmp_path / "s", benchmark_dirs=[folder])
    assert name in cfg.env_ids()
    failed = [(n, d) for n, ok, d in conform(cfg.env_factory(name), [1, 2]) if not ok]
    assert not failed, failed


def test_config_options_make_a_second_benchmark_from_one_adapter(tmp_path):
    qfile = tmp_path / "q.jsonl"
    qfile.write_text("\n".join(json.dumps(r) for r in [
        {"id": "a", "question": "What is 2 + 2?", "answer": "4", "answer_type": "exactMatch"},
        {"id": "b", "question": "Pick the even number.", "choices": ["3", "4"], "answer": "B"},
        {"id": "c", "question": "Describe this image.", "answer": "x", "image": "data:image/png;base64,AAAA"},
    ]), encoding="utf-8")
    cfg = Config(path=None, store=tmp_path / "s", environments={
        "hle-like": {"adapter": "omegaeval.envs.qa:QADataset", "options": {"path": str(qfile), "forbid_tools": ["websearch"]}}})
    env = cfg.make_env("hle-like")
    d = env.describe()
    assert d["id"] == "hle-like" and d["questions"] == 2 and d["skipped_images"] == 1
    assert d["tool_policy"]["forbid"] == ["websearch"]


def test_qa_sample_floor_and_ceiling(rig):
    cfg, store, gw, runner, baseline = rig
    orc, rnd = baseline("oracle"), baseline("random")
    oracle = [runner.run(orc, "qa-sample", "all", s) for s in range(1, 9)]
    rand = [runner.run(rnd, "qa-sample", "all", s) for s in range(1, 9)]
    assert all(r["success"] for r in oracle)
    assert sum(1 for r in rand if r["success"]) < len(rand)
    res = json.loads(Path(oracle[0]["path"], "result.json").read_text())
    assert res["outcome"]["metrics"]["gold"] and res["regret"]["extra_steps"] == 0


def test_judge_needed_answers_are_held_not_failed(rig, tmp_path):
    cfg, store, gw, runner, baseline = rig
    qfile = tmp_path / "q.json"
    qfile.write_text(json.dumps([{"id": "a", "question": "Name the largest moon of Saturn.", "answer": "Titan"}]))
    cfg.environments["judged"] = {"adapter": "omegaeval.envs.qa:QADataset", "options": {"path": str(qfile)}}
    rnd = baseline("random")                                   # answers "I don't know"
    out = runner.run(rnd, "judged", "all", 1)
    res = json.loads(Path(out["path"], "result.json").read_text())
    assert res["outcome"]["needs_judge"] is True and res["outcome"]["success"] is None
    assert out["needs_judge"]


def test_memory_sample_delivers_then_asks(rig):
    cfg, store, gw, runner, baseline = rig
    orc = baseline("oracle")
    for task in ("recall", "update"):
        out = runner.run(orc, "memory-sample", task, 3)
        assert out["success"], out
        p = Path(out["path"])
        events = read_jsonl(p / "env.jsonl")
        delivered = [e for e in events if e["kind"] == "delivered"]
        assert len(delivered) == 6 and all(e["confirmed_by"] == "paced" for e in delivered)
        assert replay(cfg.make_env("memory-sample"), json.loads((p / "manifest.json").read_text()), events)["ok"]
        term = events[-1] if events[-1]["kind"] == "terminal" else [e for e in events if e["kind"] == "terminal"][0]
        assert term["score"]["metrics"]["evidence_sessions"]


def test_long_messages_are_split_under_the_frame_limit(rig, tmp_path):
    cfg, store, gw, runner, baseline = rig
    case = {"id": "big", "sessions": [[{"role": "user", "content": "My locker code is 4417. " + "filler " * 30000}]],
            "question": "What is my locker code?", "answer": "4417"}
    f = tmp_path / "big.json"
    f.write_text(json.dumps([case]))
    cfg.environments["big-memory"] = {"adapter": "omegaeval.envs.chatmemory:ChatMemory", "options": {"path": str(f)}}
    orc = baseline("oracle")
    out = runner.run(orc, "big-memory", "all", 1)
    assert out["success"]
    sent = [t for _, t, _ in gw.links[orc].sent]
    assert all(len(json.dumps({"type": "user_message", "seq": 1, "text": t}).encode()) < 64 * 1024 for t in sent)
    assert any(" part 1/" in t for t in sent)


class ToolUser(threading.Thread):
    """Test double for an OmegaClaw agent that uses websearch while answering (writes Omega-format logs)."""

    def __init__(self, url, token, log_path: Path):
        super().__init__(daemon=True)
        self.url, self.token, self.log_path = url, token, log_path
        self.ready = threading.Event()
        self.stop_flag = threading.Event()
        self.k = 0

    def log(self, msg):
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | INFO     | loop | {msg}\n")

    def run(self):
        import re
        from websockets.sync.client import connect

        self.log_path.touch()
        with connect(self.url, additional_headers={"Authorization": f"Bearer {self.token}"}) as ws:
            ws.send(json.dumps({"type": "resume", "last_seen_seq": None}))
            self.ready.set()
            while not self.stop_flag.is_set():
                try:
                    frame = json.loads(ws.recv(timeout=0.5))
                except TimeoutError:
                    continue
                except Exception:
                    return
                text = frame.get("text", "")
                self.k += 1
                self.log(f"(---------iteration {self.k})")
                self.log("(HUMAN-MSG: " + text.replace("\n", "_newline_") + ")")
                m = re.search(r"^Reply: (ep=\S+ t=\d+) ", text, re.M)
                if not m:
                    continue
                self.log('(RESPONSE: ((websearch "saturn largest moon") (send "' + m.group(1) + ' Titan")))')
                ws.send(json.dumps({"type": "agent_message", "client_seq": uuid.uuid4().hex,
                                    "text": f"{m.group(1)} Titan"}))


def test_forbidden_tool_use_voids_the_episode(rig, tmp_path):
    cfg, store, gw, runner, baseline = rig
    qfile = tmp_path / "q.json"
    qfile.write_text(json.dumps([{"id": "a", "question": "Name the largest moon of Saturn.", "answer": "Titan"}]))
    cfg.environments["no-web"] = {"adapter": "omegaeval.envs.qa:QADataset",
                                  "options": {"path": str(qfile), "forbid_tools": ["websearch"]}}
    log_path = tmp_path / "omega.log"
    cfg.agents["cheater"] = AgentSpec(id="cheater", token="tok-cheat", kind="omega", logs={"file": str(log_path)})
    gw.add("cheater", "tok-cheat")
    agent = ToolUser(f"ws://127.0.0.1:{gw.port}/agents/cheater", "tok-cheat", log_path)
    agent.start()
    agent.ready.wait(5)
    time.sleep(0.2)
    out = runner.run("cheater", "no-web", "all", 1)
    agent.stop_flag.set()
    res = json.loads(Path(out["path"], "result.json").read_text())
    assert res["tool_policy"]["violations"] >= 1
    assert res["outcome"]["void"] is True and res["outcome"]["success"] is False
    assert res["outcome"]["success_raw"] is True
    manifest = json.loads(Path(out["path"], "manifest.json").read_text())
    assert manifest["members"]["cheater"]["evidence"].startswith("file:")


def test_agent_reported_concepts_fill_the_block(rig):
    cfg, store, gw, runner, baseline = rig
    spec = AgentSpec(id="symbolic", token="tok-sym", kind="other")
    cfg.agents[spec.id] = spec
    gw.add(spec.id, spec.token)
    plan = {}
    runner.hooks[spec.id] = lambda env: plan.update(p=env.solution())

    def agent():
        import re
        from websockets.sync.client import connect

        with connect(f"ws://127.0.0.1:{gw.port}/agents/symbolic",
                     additional_headers={"Authorization": "Bearer tok-sym"}) as ws:
            ws.send(json.dumps({"type": "resume", "last_seen_seq": None}))
            k = 0
            while True:
                try:
                    frame = json.loads(ws.recv(timeout=5))
                except Exception:
                    return
                m = re.search(r"^Reply: (ep=\S+ t=(\d+)) ", frame.get("text", ""), re.M)
                if not m:
                    if "END]" in frame.get("text", ""):
                        return
                    continue
                ws.send(json.dumps({"type": "trace", "kind": "concepts", "step": int(m.group(2)),
                                    "concepts": [{"id": "wall", "label": "wall"}, {"id": f"cell-{k % 3}"}]}))
                ws.send(json.dumps({"type": "agent_message", "client_seq": uuid.uuid4().hex,
                                    "text": f"{m.group(1)} {plan['p'][k]}"}))
                k += 1

    t = threading.Thread(target=agent, daemon=True)
    t.start()
    time.sleep(0.3)
    out = runner.run("symbolic", "keydoor", "small", 1)
    assert out["success"]
    res = json.loads(Path(out["path"], "result.json").read_text())
    assert res["concepts"]["available"] and res["concepts"]["distinct"] == 4 and res["concepts"]["reidentified"] >= 1


def test_interrupted_attempts_are_recovered(tmp_path):
    store = Store(tmp_path / "store")
    rid, att, path = store.new_attempt({"x": 1})
    store.write_json(path / "manifest.json", {"status": "running", "harness": {"owner": {"pid": 2 ** 22 + 7,
                                                                                         "host": "elsewhere"}}})
    store.ledger({"event": "started", "run_id": rid, "attempt": att, "status": "running", "started_at": "now"})
    fixed = store.recover_interrupted()
    assert len(fixed) == 1
    assert json.loads((path / "manifest.json").read_text())["status"] == "interrupted"
    assert store.attempts()[0]["status"] == "interrupted"


def test_cli_new_benchmark_and_doctor(tmp_path, monkeypatch, capsys):
    from omegaeval.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["new-benchmark", "tiny-maze", "--kind", "interactive"]) == 0
    assert main(["conform", "tiny-maze"]) == 0
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "benchmark tiny-maze  conforms" in out and "all checks passed" in out
