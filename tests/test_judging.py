"""Judge verdicts, published-format questions, tool allowlists, verification and run identity."""
import json
import shlex
import sys
import time
from pathlib import Path

import pytest

from omegaeval import judging
from omegaeval.config import AgentSpec
from omegaeval.envs.qa import QADataset, embedded_choices
from omegaeval.store import read_jsonl

from test_benchmarks import ToolUser

HLE_STYLE = "Which number is prime?\n\nAnswer Choices:\nA. 51\nB. 53\nC. 57\nD. 91"


def test_options_written_into_the_question_are_read():
    assert embedded_choices(HLE_STYLE) == ["51", "53", "57", "91"]
    assert embedded_choices("Pick one.\nA. red\nB. blue") == ["red", "blue"]
    assert embedded_choices("Answer Choices:\nA. a long\noption\nB. b") == ["a long option", "b"]
    assert embedded_choices("Compute A. then B. in order.") is None
    assert embedded_choices("Options below.\nB. one\nC. two") is None


def test_published_multiple_choice_is_graded_by_letter():
    class HLELike(QADataset):
        id = "hle-like"
        RECORDS = [{"id": "h1", "question": HLE_STYLE, "answer": "B", "answer_type": "multipleChoice"},
                   {"id": "h2", "question": "Which is right? The options are in the figure.", "answer": "C",
                    "answer_type": "multipleChoice"}]

    env = HLELike()
    obs = env.reset("all", 1)
    assert obs.text.count("A. 51") == 1                      # sent as written, never listed twice
    for reply, ok in (("B. 53", True), ("The answer is D", False), ("53", True)):
        env.reset("all", 1)
        env.step(env.parse_action(reply))
        assert env.score()["success"] is ok, reply
    env.reset("all", 2)
    env.step(env.parse_action("C"))
    assert env.score()["success"] is True                    # options unknown: a bare letter still decides
    env.reset("all", 2)
    env.step(env.parse_action("probably the third one"))
    assert env.score()["needs_judge"] is True                # options unknown and no letter: a judge decides
    assert env.describe()["choices_read_from_text"] == 1


def test_judge_all_holds_every_answer_and_keeps_a_pregrade():
    env = QADataset(sample=True, judge_all=True)
    env.reset("all", 1)
    env.step(env.solution()[0])
    s = env.score()
    assert s["needs_judge"] and s["success"] is None and s["metrics"]["pregrade"] == "match"
    assert env.describe()["scoring"] == "judge"


def _held_attempt(cfg, runner, baseline, tmp_path):
    qfile = tmp_path / "q.json"
    qfile.write_text(json.dumps([{"id": "a", "question": "Name the largest moon of Saturn.", "answer": "Titan"}]))
    cfg.environments["judged"] = {"adapter": "omegaeval.envs.qa:QADataset", "options": {"path": str(qfile)}}
    out = runner.run(baseline("random"), "judged", "all", 1)     # the random policy says it does not know
    assert out["needs_judge"]
    return out


def test_verdicts_settle_held_answers_and_name_the_judge(rig, tmp_path):
    cfg, store, gw, runner, baseline = rig
    out = _held_attempt(cfg, runner, baseline, tmp_path)
    rid, att = out["run_id"], out["attempt"]
    cases = judging.held(store)
    assert len(cases) == 1 and cases[0]["gold"] == "Titan" and "Saturn" in cases[0]["question"]

    res = judging.record(store, cfg, rid, att, "wrong", "tester", "said it did not know")
    oc = res["outcome"]
    assert oc["success"] is False and oc["terminal"] == "judged" and not oc.get("needs_judge")
    assert oc["judged"]["by"] == "tester" and oc["judged"]["via"] == "person"
    assert judging.held(store) == [] and store.verify() == []

    script = tmp_path / "judge.py"
    script.write_text("import json, sys\nc = json.load(sys.stdin)\n"
                      "print(json.dumps({'verdict': 'yes' if c['gold'] == 'Titan' else 'no', 'reason': 'test',"
                      " 'model': 'fake'}))\n")
    v = judging.ask(f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}", judging.case(store, rid, att))
    assert v["verdict"] == "correct" and v["detail"] == {"model": "fake"}
    res = judging.record(store, cfg, rid, att, v["verdict"], "fake judge", v["reason"], via="command",
                         detail=v["detail"])
    j = res["outcome"]["judged"]
    assert res["outcome"]["success"] is True and j["judgments"] == 2 and j["unanimous"] is False
    assert len(read_jsonl(Path(out["path"]) / "judgments.jsonl")) == 2
    assert [e["verdict"] for e in read_jsonl(store.index_path) if e["event"] == "judged"] == ["wrong", "correct"]
    assert store.attempts()[0]["success"] is True

    with pytest.raises(ValueError):
        judging.record(store, cfg, rid, att, "maybe", "tester")
    with pytest.raises(ValueError):
        judging.record(store, cfg, rid, att, "wrong", "")

    store.release("paper", [{"run_id": rid, "attempt": att}])
    with pytest.raises(PermissionError):
        judging.record(store, cfg, rid, att, "wrong", "tester")
    assert store.verify("paper") == []
    (Path(out["path"]) / "result.json").write_text("{}")
    assert any("result.json changed" in p for p in store.verify("paper"))
    assert any("result.json changed" in p for p in store.verify())


def test_deterministic_answers_are_not_judged(rig):
    cfg, store, gw, runner, baseline = rig
    out = runner.run(baseline("oracle"), "qa-sample", "all", 1)
    with pytest.raises(ValueError):
        judging.record(store, cfg, out["run_id"], out["attempt"], "wrong", "tester")


@pytest.mark.parametrize("rule", [{"forbid_tools": ["websearch"]}, {"allow_tools": ["remember", "query", "pin"]}])
def test_tool_rules_void_the_episode(rig, tmp_path, rule):
    cfg, store, gw, runner, baseline = rig
    qfile = tmp_path / "q.json"
    qfile.write_text(json.dumps([{"id": "a", "question": "Name the largest moon of Saturn.", "answer": "Titan"}]))
    cfg.environments["exam"] = {"adapter": "omegaeval.envs.qa:QADataset", "options": {"path": str(qfile), **rule}}
    log_path = tmp_path / "omega.log"
    cfg.agents["cheater"] = AgentSpec(id="cheater", token="tok-cheat", kind="omega", logs={"file": str(log_path)})
    gw.add("cheater", "tok-cheat")
    agent = ToolUser(f"ws://127.0.0.1:{gw.port}/agents/cheater", "tok-cheat", log_path)
    agent.start()
    agent.ready.wait(5)
    time.sleep(0.2)
    out = runner.run("cheater", "exam", "all", 1)
    agent.stop_flag.set()
    res = json.loads(Path(out["path"], "result.json").read_text())
    assert res["outcome"]["void"] is True and "websearch" in res["outcome"]["void_reason"]
    sent = [t for _, t, _ in gw.links["cheater"].sent]
    assert any(("only these skills" if "allow_tools" in rule else "do not use these skills") in t for t in sent)


def test_new_data_or_options_get_a_new_run_id(rig, tmp_path):
    cfg, store, gw, runner, baseline = rig
    qfile = tmp_path / "q.jsonl"
    qfile.write_text(json.dumps({"id": "a", "question": "What is 2 + 2?", "answer": "4"}) + "\n")
    cfg.environments["exam"] = {"adapter": "omegaeval.envs.qa:QADataset", "options": {"path": str(qfile)}}
    orc = baseline("oracle")
    first = runner.run(orc, "exam", "all", 1)
    again = runner.run(orc, "exam", "all", 1)
    assert again["run_id"] == first["run_id"] and again["attempt"] == first["attempt"] + 1
    qfile.write_text(json.dumps({"id": "a", "question": "What is 2 + 2?", "answer": "4.0"}) + "\n")
    changed = runner.run(orc, "exam", "all", 1)
    assert changed["run_id"] != first["run_id"]
    cfg.environments["exam"]["options"]["judge_all"] = True
    assert runner.run(orc, "exam", "all", 1)["run_id"] not in (first["run_id"], changed["run_id"])
    ident = json.loads(Path(changed["path"], "manifest.json").read_text())["identity"]
    assert ident["env_data"]["sha256"] and "path" not in ident["env_options"]


def test_cli_judge_verify_and_release(tmp_path, monkeypatch, capsys):
    from omegaeval.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "benchmarks").mkdir()
    (tmp_path / "benchmarks" / "moons.py").write_text(
        "from omegaeval.envs.qa import QADataset\n\n\nclass Moons(QADataset):\n    id = 'moons'\n"
        "    RECORDS = [{'id': 'm1', 'question': 'Name the largest moon of Saturn.', 'answer': 'Titan'}]\n")
    assert main(["baseline", "random", "moons", "--seeds", "1"]) == 0
    capsys.readouterr()
    assert main(["judge"]) == 0
    listing = capsys.readouterr().out
    assert "1 answer(s) held for a judge" in listing and "Titan" in listing
    ref = listing.split()[0]
    assert main(["judge", ref, "--verdict", "wrong", "--by", "tester"]) == 0
    assert main(["verify"]) == 0
    assert main(["release", "p1"]) == 0
    assert main(["verify", "p1"]) == 0
    assert main(["judge", ref, "--verdict", "correct", "--by", "tester"]) == 1
    assert "frozen in release p1" in capsys.readouterr().out
    assert main(["rebuild"]) == 0
    assert "skipped" in capsys.readouterr().out
