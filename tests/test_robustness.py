"""What happens when things go wrong: a stopped harness, a torn ledger line, a fresh config."""
import json
import re
from pathlib import Path

import pytest

from omegaeval.cli import main
from omegaeval.config import load
from omegaeval.store import Store, read_jsonl


def test_a_stopped_harness_records_the_episode_as_interrupted_and_not_graded(rig, monkeypatch):
    cfg, store, gw, runner, baseline = rig
    agent = baseline("random")

    def stopped(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "_play", stopped)
    with pytest.raises(KeyboardInterrupt):
        runner.run(agent, "keydoor", "small", 1)
    rec = store.attempts()[-1]
    assert rec["status"] == "interrupted"
    res = json.loads((store.path(rec["run_id"], rec["attempt"]) / "result.json").read_text())
    assert res["outcome"]["success"] is None and "stopped" in res["outcome"]["not_graded"]
    [s] = store.progress(env="keydoor")
    assert (s["n"], s["graded"], s["solved"]) == (1, 0, 0)     # counted, never graded


def test_a_torn_ledger_line_cannot_swallow_the_next_record(tmp_path):
    store = Store(tmp_path / "store")
    with open(store.index_path, "ab") as fh:
        fh.write(b'{"t": "2026-09-25T00:00:00", "event": "sta')      # a crash in the middle of a write
    store.ledger({"event": "rebuilt", "run_id": "keydoor-000000000000", "attempt": 1})
    assert read_jsonl(store.index_path)[-1]["event"] == "rebuilt"
    store.ledger({"event": "rebuilt", "run_id": "keydoor-000000000000", "attempt": 2})
    assert [e["attempt"] for e in read_jsonl(store.index_path)] == [1, 2]


def test_init_writes_a_config_that_loads(tmp_path):
    assert main(["init", "--dir", str(tmp_path)]) == 0
    cfg = load(tmp_path / "omegaeval.toml")
    assert not cfg.unknown and re.fullmatch(r"[0-9a-f]{32}", cfg.agents["oma-1"].token)
    assert main(["init", "--dir", str(tmp_path)]) == 1                # never overwrites
    example = Path(__file__).resolve().parents[1] / "examples" / "omegaeval.example.toml"
    assert not load(example).unknown


def test_a_log_tap_whose_thread_starts_late_still_reads_the_whole_episode(tmp_path):
    from omegaeval.sources import LogTap
    log = tmp_path / "omega.log"
    log.write_text("2026-09-27 10:00:00 | INFO     | loop | before the episode\n")
    tap = LogTap("a", {"file": str(log)}, tmp_path / "out")
    with open(log, "a", encoding="utf-8") as fh:      # the agent writes before the tap's thread has run
        fh.write("2026-09-27 10:00:01 | INFO     | loop | (HUMAN-MSG: [omegaeval ep=abc1 t=0] hello)\n")
    tap.start()
    tap.stop(grace=0.3)
    raw = (tmp_path / "out" / "omega.log").read_text()
    assert "before the episode" not in raw and "HUMAN-MSG" in raw
    assert tap.has_read("abc1", 0)
