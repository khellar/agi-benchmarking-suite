"""Agents other than OmegaClaw: HTTP, subprocess and in-process connectors, reported traces, and the
model-only Claude example against a stand-in for the Anthropic API."""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omegaeval.config import AgentSpec, load
from omegaeval.store import read_jsonl


AGENTS = Path(__file__).resolve().parents[1] / "examples" / "agents"


def _agent(cfg, aid: str, **connect) -> str:
    cfg.agents[aid] = AgentSpec(id=aid, token="", kind="other", connect=connect)
    return aid


def _result(out) -> dict:
    return json.loads(Path(out["path"], "result.json").read_text())


def test_an_in_process_python_agent_plays_and_reports_its_reasoning(rig):
    cfg, store, gw, runner, baseline = rig
    aid = _agent(cfg, "notebook", python="notebook:Notebook", cwd=str(AGENTS))
    outs = [runner.run(aid, "qa-sample", "all", seed) for seed in range(1, 5)]
    assert all(o["success"] for o in outs), outs
    res = _result(outs[0])
    assert res["cognition"][aid]["reasoning"].get("notebook", 0) >= 1
    manifest = json.loads(Path(outs[0]["path"], "manifest.json").read_text())
    assert manifest["members"][aid]["transport"] == "python"
    assert manifest["members"][aid]["evidence"] == "reported by the agent over python"
    trace = read_jsonl(Path(outs[0]["path"]) / "agents" / aid / "trace.jsonl")
    ep = manifest["ep_tag"]
    assert trace and trace[0]["source"] == "reported" and trace[0]["ep_ticks"] == [{"ep": ep, "t": 0}]
    assert trace[0]["calls"][0]["premises"][0]["term"].startswith("What is")
    events = read_jsonl(Path(outs[0]["path"]) / "env.jsonl")
    sent = next(e for e in events if e["kind"] == "observation")
    assert json.loads(sent["text"])["protocol"] == "omegaeval.agent/1"      # structured, no tags to repeat


def test_a_subprocess_agent_confirms_every_message_it_reads(rig):
    cfg, store, gw, runner, baseline = rig
    aid = _agent(cfg, "notebook-cli", command=[sys.executable, str(AGENTS / "notebook.py")])
    for task in ("recall", "update"):
        out = runner.run(aid, "memory-sample", task, 2)
        assert out["success"], out
        events = read_jsonl(Path(out["path"]) / "env.jsonl")
        delivered = [e for e in events if e["kind"] == "delivered"]
        assert delivered and all(e["confirmed_by"] == "acknowledged" for e in delivered)
    res = _result(out)
    assert res["cognition"][aid]["memory"]["notebook"]["add"] == 6


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_an_http_agent_plays(rig):
    cfg, store, gw, runner, baseline = rig
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(AGENTS / "notebook.py"), "--http", str(port)])
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        aid = _agent(cfg, "notebook-http", http=f"http://127.0.0.1:{port}/")
        out = runner.run(aid, "qa-sample", "all", 2)
        assert out["success"], out
    finally:
        proc.terminate()
        proc.wait(timeout=5)


class FakeAnthropic(BaseHTTPRequestHandler):
    """Answers /v1/messages like the Anthropic API: multiplies what the last question asks."""
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAnthropic.seen.append({"key": self.headers.get("x-api-key"), "body": body})
        last = body["messages"][-1]["content"]
        m = re.search(r"What is (\d+) × (\d+)", last)
        answer = str(int(m.group(1)) * int(m.group(2))) if m else "I don't know"
        data = json.dumps({"content": [{"type": "text", "text": answer}], "stop_reason": "end_turn",
                           "usage": {"input_tokens": 120, "output_tokens": 4, "cache_read_input_tokens": 0}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def test_the_claude_example_reports_tokens_and_cost(rig, monkeypatch):
    cfg, store, gw, runner, baseline = rig
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeAnthropic)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-for-the-fake-server")
    cfg.prices = {"claude-opus-5-5": {"input": 15.0, "output": 75.0}}
    try:
        aid = _agent(cfg, "claude", python="claude:Claude", cwd=str(AGENTS))
        out = runner.run(aid, "qa-sample", "all", 1)                  # seed 1 is an arithmetic question
    finally:
        httpd.shutdown()
    assert out["success"], out
    res = _result(out)
    assert res["cognition"][aid]["llm_calls"] == 1 and res["cognition"][aid]["tokens"]["prompt_tokens"] == 120
    assert res["cost"]["available"] and res["cost"]["model"] == "claude-opus-5-5"
    request = FakeAnthropic.seen[-1]
    assert request["key"] == "test-key-for-the-fake-server" and request["body"]["model"] == "claude-opus-5-5"
    assert "Answer the question" in request["body"]["system"]


class Cheater:
    """Reports a web search it made, and two LLM calls per step."""

    def __init__(self, **options):
        pass

    def act(self, message):
        if not message.get("expects_reply"):
            return {}
        return {"reply": "Titan", "trace": [{"kind": "tool", "name": "websearch", "args": "saturn moons"},
                                            {"kind": "llm", "model": "m", "input_tokens": 1, "output_tokens": 1},
                                            {"kind": "llm", "model": "m", "input_tokens": 1, "output_tokens": 1}]}


def test_reported_tool_use_is_held_to_the_tool_rules(rig, tmp_path, monkeypatch):
    cfg, store, gw, runner, baseline = rig
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    qfile = tmp_path / "q.json"
    qfile.write_text(json.dumps([{"id": "a", "question": "Name the largest moon of Saturn.", "answer": "Titan"}]))
    cfg.environments["exam"] = {"adapter": "omegaeval.envs.qa:QADataset",
                                "options": {"path": str(qfile), "forbid_tools": ["websearch"]}}
    aid = _agent(cfg, "cheater", python="test_connectors:Cheater")
    res = _result(runner.run(aid, "exam", "all", 1))
    assert res["outcome"]["void"] is True and res["tool_policy"]["violations"] == 1
    cfg.max_llm_calls = 1
    out = runner.run(aid, "keydoor", "small", 1)
    assert out["status"] == "llm_budget"


class Broken:
    def __init__(self, **options):
        pass

    def act(self, message):
        raise ValueError("the model is down")


def test_an_agent_that_fails_ends_the_episode_with_the_error_recorded(rig, monkeypatch):
    cfg, store, gw, runner, baseline = rig
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    cfg.step_timeout_s = 1
    aid = _agent(cfg, "broken", python="test_connectors:Broken")
    out = runner.run(aid, "qa-sample", "all", 1)
    assert out["status"] == "agent_timeout"
    res = _result(out)
    assert res["cognition"][aid]["errors"].get("error", 0) >= 1


@pytest.mark.parametrize("block,problem", [
    ('[agents.a]\nconnect = { http = "http://x", python = "m:C" }\n', "exactly one"),
    ('[agents.a]\nconnect = { http = "ftp://x" }\n', "http or https"),
    ('[agents.a]\nconnect = { python = "module" }\n', "module:Class"),
    ('[agents.a]\nconnect = { command = ["x"], colour = "blue" }\n', "not a connect key"),
    ('[agents.a]\nlabel = "no token and no connect"\n', "needs a token"),
])
def test_agent_connections_are_validated(tmp_path, block, problem):
    f = tmp_path / "omegaeval.toml"
    f.write_text(block)
    with pytest.raises(SystemExit, match=problem):
        load(f)


class Slow:
    """Takes a second to answer in episode e1, and answers at once in any other."""

    def __init__(self, **options):
        pass

    def act(self, message):
        if message["episode"] == "e1":
            time.sleep(1.0)
        return {"reply": f"answer for {message['episode']}"}


def test_an_agent_still_answering_is_left_behind_and_cannot_leak_into_the_next_episode(monkeypatch):
    from omegaeval import connectors
    monkeypatch.setattr(connectors, "END_GRACE_S", 0.2)
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    spec = AgentSpec(id="slow", token="", kind="other", connect={"python": "test_connectors:Slow"})
    c = connectors.connector_for(spec, timeout=5)
    c.begin_episode("e1")
    c.deliver(connectors.Message("e1", 0, "brief", text="q", expects_reply=True))
    t0 = time.monotonic()
    c.end_episode()
    assert time.monotonic() - t0 < 1.0                 # the end of an episode does not wait out the agent
    c.begin_episode("e2")
    time.sleep(1.2)                                     # e1's answer arrives now, into e1's closed queues
    assert c.drain() == []
    c.deliver(connectors.Message("e2", 0, "brief", text="q", expects_reply=True))
    got = []
    for _ in range(100):
        got += c.drain()
        if got:
            break
        time.sleep(0.02)
    c.end_episode()
    assert [(g.episode, g.text) for g in got] == [("e2", "answer for e2")]


LEAVER = """
import json, subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
print(json.dumps({"type": "trace", "step": 0, "events": [{"kind": "context", "chars": child.pid}]}), flush=True)
for line in sys.stdin:
    print("{}", flush=True)
"""


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_a_command_agent_leaves_no_process_behind(tmp_path):
    from omegaeval import connectors
    script = tmp_path / "leaver.py"
    script.write_text(LEAVER)
    spec = AgentSpec(id="leaver", token="", kind="other", connect={"command": [sys.executable, str(script)]})
    c = connectors.connector_for(spec, timeout=5)
    c.begin_episode("e1", tmp_path / "record")
    pid = None
    for _ in range(200):
        got = c.drain_traces()
        if got:
            pid = got[0][1]["events"][0]["chars"]
            break
        time.sleep(0.05)
    assert pid, "the agent never reported its child"
    c.end_episode()                                     # the agent exits cleanly, but its child would linger
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)
        pytest.fail("a process the agent started outlived the episode")


def test_changing_an_agents_settings_takes_effect_without_a_restart(rig):
    cfg, store, gw, runner, baseline = rig
    first = runner.connector(AgentSpec(id="svc", token="", kind="other", connect={"http": "http://127.0.0.1:1/"}))
    same = runner.connector(AgentSpec(id="svc", token="", kind="other", connect={"http": "http://127.0.0.1:1/"}))
    moved = runner.connector(AgentSpec(id="svc", token="", kind="other", connect={"http": "http://127.0.0.1:2/"}))
    assert same is first and moved is not first and moved.spec.connect["http"].endswith(":2/")


def test_a_token_can_come_from_the_environment(tmp_path, monkeypatch):
    f = tmp_path / "omegaeval.toml"
    f.write_text('[agents.a]\ntoken = "${OMEGAEVAL_TEST_TOKEN}"\n')
    monkeypatch.setenv("OMEGAEVAL_TEST_TOKEN", "t" * 32)
    assert load(f).agents["a"].token == "t" * 32
    monkeypatch.delenv("OMEGAEVAL_TEST_TOKEN")
    with pytest.raises(SystemExit, match="OMEGAEVAL_TEST_TOKEN is not set"):
        load(f)


class BrokenAtStart:
    def __init__(self, **options):
        raise RuntimeError("no model configured")


def test_an_agent_that_cannot_start_is_recorded_not_crashed(rig, monkeypatch):
    cfg, store, gw, runner, baseline = rig
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    aid = _agent(cfg, "unstartable", python="test_connectors:BrokenAtStart")
    out = runner.run(aid, "qa-sample", "all", 1)
    assert out["status"] == "error"
    res = _result(out)
    assert res["outcome"]["success"] is None and "no model configured" in res["outcome"]["not_graded"]
    assert store.attempts()[-1]["status"] == "error"               # finished, not left running


def test_an_agent_next_to_the_config_needs_no_cwd_and_is_checked_first(tmp_path, capsys):
    from omegaeval.cli import main
    from omegaeval.connectors import preflight
    (tmp_path / "agent_beside_config.py").write_text((AGENTS / "notebook.py").read_text())
    f = tmp_path / "omegaeval.toml"
    f.write_text('[harness]\nstore = "store"\n\n[agents.mine]\nconnect = { python = "agent_beside_config:Notebook" }\n'
                 '\n[agents.typo]\nconnect = { python = "agent_beside_config:Notbook" }\n')
    cfg = load(f)
    assert cfg.agents["mine"].connect["cwd"] == str(tmp_path) and preflight(cfg.agents["mine"])[0] is True
    assert main(["run", "mine", "qa-sample", "--seeds", "1-2", "--local", "-c", str(f)]) == 0
    assert "qa-sample seed 2: solved" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="agent typo: cannot load agent_beside_config:Notbook"):
        main(["run", "typo", "qa-sample", "--seeds", "1", "--local", "-c", str(f)])
    assert len(load(f).agents) == 2 and len(list((tmp_path / "store" / "runs").glob("*/*"))) == 2   # none claimed


def test_the_server_refuses_to_queue_episodes_for_an_agent_it_cannot_reach(rig):
    from omegaeval.server import Harness
    cfg, store, gw, runner, baseline = rig
    _agent(cfg, "gone", http=f"http://127.0.0.1:{_free_port()}/")
    with pytest.raises(ValueError, match="agent gone: nothing answers"):
        Harness(cfg).submit("gone", "qa-sample", None, [1, 2])
    assert store.attempts() == []
