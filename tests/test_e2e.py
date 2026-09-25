"""End-to-end tests over real websockets.

FakeOmega is a test double, not an OmegaClaw: it speaks the wschat protocol and writes
log lines in the format src/loop.metta and providers/lib_llm_ext.py produce, so the
file tap, the parser and the step alignment can be tested without running Omega.
"""
import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from omegaeval.baselines import BaselineAgent, spec_for
from omegaeval.config import AgentSpec, Config, HiveSpec
from omegaeval.episode import EpisodeRunner
from omegaeval.gateway import Gateway
from omegaeval.store import Store


def safe(text: str) -> str:
    return text.replace('"', "_quote_").replace("\n", "_newline_").replace("'", "_apostrophe_")


class FakeOmega(threading.Thread):
    def __init__(self, url, token, log_path: Path, plan_holder: dict):
        super().__init__(daemon=True)
        self.url, self.token, self.log_path, self.plan = url, token, log_path, plan_holder
        self.k = 100
        self.ready = threading.Event()
        self.stop_flag = threading.Event()

    def log(self, logger, msg, level="INFO"):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{ts} | {level:<8} | {logger} | {msg}\n")

    def run(self):
        from websockets.sync.client import connect

        self.log_path.touch()
        with connect(self.url, additional_headers={"Authorization": f"Bearer {self.token}"}) as ws:
            ws.send(json.dumps({"type": "resume", "last_seen_seq": None}))
            ws.send(json.dumps({"type": "agent_message", "client_seq": "v", "text": "Omega version=v0.1.19-test"}))
            self.ready.set()
            while not self.stop_flag.is_set():
                try:
                    frame = json.loads(ws.recv(timeout=0.5))
                except TimeoutError:
                    continue
                except Exception:
                    return          # gateway closed at teardown
                if frame.get("type") != "user_message":
                    continue
                text = frame["text"]
                self.k += 1
                self.log("loop", f"(---------iteration {self.k})")
                self.log("loop", f"(HUMAN-MSG: {safe(text)})")
                self.log("loop", f'(CHARS_SENT: 200 "PROMPT: p SKILLS: s OUTPUT_FORMAT: o SAVE_PERMANENT_FILES_DIR: d '
                                 f'LAST_SKILL_USE_RESULTS: () HISTORY: h TIME: t :-:-:-: (HUMAN-MSG: {safe(text)[:40]})")')
                self.log("lib_llm_ext", "[LLM_USAGE] provider=Test model=fake finish_reason=stop prompt_tokens=900 "
                                        "cached_tokens=800 completion_tokens=30 reasoning_tokens=10 ")
                if "END]" in text:
                    self.log("loop", '(RESPONSE: ((pin "episode over")))')
                    self.log("loop", "(RESPONSE: (RESULTS: ((COMMAND_RETURN: ((pin \"episode over\") PIN-SUCCESS)))))")
                    continue
                import re
                t = int(re.search(r"t=(\d+)\]", text).group(1))
                action = self.plan["plan"][t]
                calls = f'(pin "step {t}") (send "t={t} {action}")'
                results = (f'(COMMAND_RETURN: ((pin "step {t}") PIN-SUCCESS)) '
                           f'(COMMAND_RETURN: ((send "t={t} {action}") None))')
                if action == "pick":
                    calls += ' (remember "picked up the key")'
                    results += ' (COMMAND_RETURN: ((remember "picked up the key") REMEMBER-SUCCESS))'
                    calls += ' (metta "(|- ((--> key held) (stv 1.0 0.9)) ((--> held open) (stv 1.0 0.9)))")'
                    results += (' (COMMAND_RETURN: ((metta "(|- ((--> key held) (stv 1.0 0.9)) ((--> held open) '
                                '(stv 1.0 0.9)))") "((--> key open) (stv 1.0 0.81))"))')
                self.log("loop", f"(RESPONSE: ({calls}))")
                self.log("loop", f"(RESPONSE: (RESULTS: ({results})))")
                ws.send(json.dumps({"type": "agent_message", "client_seq": uuid.uuid4().hex,
                                    "text": f"t={t} {action}"}))


@pytest.fixture()
def rig(tmp_path):
    cfg = Config(path=None, store=tmp_path / "store", gateway_host="127.0.0.1", gateway_port=0,
                 grace_s=1.0, step_timeout_s=20)
    store = Store(cfg.store)
    gw = Gateway("127.0.0.1", 0).start()
    runner = EpisodeRunner(cfg, store, gw)
    yield cfg, store, gw, runner
    gw.stop()


def test_baselines_bound_the_task(rig):
    cfg, store, gw, runner = rig
    for pol in ("random", "oracle"):
        spec, policy = spec_for(pol)
        cfg.agents[spec.id] = spec
        gw.add(spec.id, spec.token)
        runner.hooks[spec.id] = policy.prepare
        a = BaselineAgent(spec.id, f"ws://127.0.0.1:{gw.port}/agents/{spec.id}", spec.token, policy)
        a.start()
        a.ready.wait(5)
    time.sleep(0.2)
    rnd = runner.run("baseline-random", "keydoor", "small", 1)
    orc = runner.run("baseline-oracle", "keydoor", "small", 1)
    assert orc["success"] and not rnd["success"]
    res = json.loads(Path(orc["path"], "result.json").read_text())
    assert res["regret"]["extra_steps"] == 0
    assert res["cognition"]["baseline-oracle"]["available"] is False
    assert json.loads(Path(orc["path"], "checksums.json").read_text())["files"]["env.jsonl"]


def test_omega_log_tap_aligns_with_steps(rig, tmp_path):
    cfg, store, gw, runner = rig
    log_path = tmp_path / "omega.log"
    agent = AgentSpec(id="oma-test", token="tok-oma", kind="omega", logs={"file": str(log_path)},
                      memory=None, label="FakeOmega test double")
    cfg.agents[agent.id] = agent
    gw.add(agent.id, agent.token)
    plan = {}
    runner.hooks[agent.id] = lambda env: plan.update(plan=env.solution())
    fake = FakeOmega(f"ws://127.0.0.1:{gw.port}/agents/{agent.id}", agent.token, log_path, plan)
    fake.start()
    fake.ready.wait(5)
    time.sleep(0.3)
    out = runner.run("oma-test", "keydoor", "small", 2)
    fake.stop_flag.set()
    assert out["success"]
    bundle = store.bundle(out["run_id"], out["attempt"])
    iters = bundle["agents"]["oma-test"]["iterations"]
    steps = out["steps"]
    tagged = [i for i in iters if i["ep_ticks"]]
    assert len(tagged) == steps          # one loop turn per observation, matched by tag
    assert [i["ep_ticks"][0]["t"] for i in tagged] == list(range(steps))
    cog = bundle["result"]["cognition"]["oma-test"]
    assert cog["llm_calls"] >= steps and cog["reasoning"]["nal"] == 1
    assert cog["memory"]["chroma"]["add"] == 1 and cog["memory"]["working"]["pin"] >= steps
    assert cog["tokens"]["prompt_tokens"] == 900 * cog["tokens"]["calls_with_usage"]
    assert bundle["manifest"]["provenance"]["agent_versions"]["oma-test"] == "Omega version=v0.1.19-test"
    sends = [c for i in iters for c in i["calls"] if c["skill"] == "send"]
    assert all("tick_claim" in c for c in sends)


def test_hive_actor_and_relay(rig):
    cfg, store, gw, runner = rig
    orc_spec, orc = spec_for("oracle")
    rnd_spec, rnd = spec_for("random")
    for spec, pol in ((orc_spec, orc), (rnd_spec, rnd)):
        cfg.agents[spec.id] = spec
        gw.add(spec.id, spec.token)
        runner.hooks[spec.id] = pol.prepare
        a = BaselineAgent(spec.id, f"ws://127.0.0.1:{gw.port}/agents/{spec.id}", spec.token, pol)
        a.start()
        a.ready.wait(5)
    cfg.hives["pair"] = HiveSpec(id="pair", members=[orc_spec.id, rnd_spec.id], actor=orc_spec.id, relay=True)
    time.sleep(0.2)
    out = runner.run("pair", "keydoor", "small", 3)
    assert out["success"]
    env = store.bundle(out["run_id"], out["attempt"])["env"]
    actors = {e["agent"] for e in env if e["kind"] == "action"}
    assert actors == {orc_spec.id}       # only the actor's messages move the game
    advisor_msgs = [e for e in env if e["kind"] == "agent_message" and e["agent"] == rnd_spec.id]
    assert advisor_msgs                  # the advisor was heard and recorded


def test_exact_replay_and_conformance(rig):
    from omegaeval.checks import conform, replay
    from omegaeval.envs.keydoor import KeyDoor
    from omegaeval.store import read_jsonl

    cfg, store, gw, runner = rig
    spec, policy = spec_for("random")
    cfg.agents[spec.id] = spec
    gw.add(spec.id, spec.token)
    runner.hooks[spec.id] = policy.prepare
    a = BaselineAgent(spec.id, f"ws://127.0.0.1:{gw.port}/agents/{spec.id}", spec.token, policy)
    a.start()
    a.ready.wait(5)
    time.sleep(0.2)
    out = runner.run(spec.id, "keydoor", "small", 4)
    p = Path(out["path"])
    manifest = json.loads((p / "manifest.json").read_text())
    events = read_jsonl(p / "env.jsonl")
    assert replay(KeyDoor(), manifest, events)["ok"]
    tampered = [dict(e, effect="moved") if e.get("kind") == "receipt" and e.get("effect") != "moved" else e
                for e in events]
    assert not replay(KeyDoor(), manifest, tampered)["ok"]
    assert all(ok for _, ok, _ in conform(KeyDoor, [1, 2, 3]))


def test_release_is_frozen(rig):
    cfg, store, gw, runner = rig
    spec, policy = spec_for("oracle")
    cfg.agents[spec.id] = spec
    gw.add(spec.id, spec.token)
    runner.hooks[spec.id] = policy.prepare
    a = BaselineAgent(spec.id, f"ws://127.0.0.1:{gw.port}/agents/{spec.id}", spec.token, policy)
    a.start()
    a.ready.wait(5)
    time.sleep(0.2)
    runner.run(spec.id, "keydoor", "small", 1)
    rows = [r for r in store.attempts() if r.get("status") == "finished"]
    rel = store.release("paper-v1", rows, "test")
    doc = json.loads((rel / "release.json").read_text())
    assert doc["attempts"][0]["checksums"]["env.jsonl"]
    with pytest.raises(FileExistsError):
        store.release("paper-v1", rows)
