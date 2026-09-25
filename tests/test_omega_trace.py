"""Parser tests. Fixture provenance is documented at the top of fixtures/omega_log_sample.log."""
from pathlib import Path

from omegaeval import omega_trace as ot

FIXTURE = Path(__file__).parent / "fixtures" / "omega_log_sample.log"


def _trace():
    return ot.parse_lines(FIXTURE.read_text(encoding="utf-8").splitlines())


def test_line_format_and_noise():
    assert ot.parse_line("Welcome to PeTTa") is None
    rec = ot.parse_line("2026-08-13 11:12:20 | INFO     | loop | (---------iteration 43)")
    assert rec["logger"] == "loop" and rec["level"] == "INFO" and rec["t_source"] == "logging"
    rec = ot.parse_line("2026-09-25T10:00:00.123456789Z 2026-09-25 10:00:00 | INFO     | loop | x | y")
    assert rec["t"] == "2026-09-25T10:00:00.123456+00:00" and rec["msg"] == "x | y"


def test_iterations_and_startup():
    tb = _trace()
    assert [i["i"] for i in tb.iterations] == [41, 42, 43, 44, 45, 46]
    assert tb.startup["skills_added"] == ["- Load workflow instructions: workflow-load-instructions name"]


def test_human_message_decoding():
    it = _trace().iterations[0]
    assert it["human_msg"].startswith("@...: Find the current Bitcoin price")
    assert '"btc-price"' in it["human_msg"]


def test_prompt_sections_and_usage():
    it = _trace().iterations[0]
    p = it["prompt"]
    assert p["chars"] == 402
    for key in ("prompt", "skills", "output_format", "save_dir", "last_results", "history", "time", "input"):
        assert key in p["sections"], key
    assert p["time"] == "2026-08-13 11:11:40"
    assert p["input"].startswith("(HUMAN-MSG:")
    u = it["llm"][0]
    assert u["provider"] == "ASICloud" and u["prompt_tokens"] == 10233 and u["reasoning_tokens"] == 180


def test_truncated_results_line_does_not_crash():
    it = _trace().iterations[0]
    assert it["calls"][0]["skill"] == "websearch"


def test_memory_write_and_send():
    it = _trace().iterations[1]
    remember, send = it["calls"]
    assert remember["lane"] == "memory" and remember["store"] == "chroma" and remember["op"] == "add"
    assert remember["result"]["kind"] == "ok" and remember["result"]["text"] == "REMEMBER-SUCCESS"
    assert send["lane"] == "action" and send["result"]["text"] == "None"
    assert it["human_msg"] is None


def test_memory_read_items():
    it = _trace().iterations[2]
    q = it["calls"][0]
    assert q["store"] == "chroma" and q["op"] == "read"
    assert q["result"]["kind"] == "items"
    assert q["result"]["items"][0][0] == "2026-08-13 11:12:02"


def test_escaped_quotes_in_args():
    it = _trace().iterations[3]
    assert it["calls"][0]["args"][0].startswith('Queried memory for "btc-price"')
    assert it["prompt"] is None and it["idle"] is True


def test_nal_call_and_unknown_skill_error():
    it = _trace().iterations[4]
    metta, err = it["calls"]
    assert metta["engine"] == "nal"
    assert len(metta["premises"]) == 2 and metta["premises"][1]["c"] == 0.9
    assert metta["conclusions"][0]["c"] == 0.81
    assert err["lane"] == "error" and err["error"] == "UNKNOWN_SKILL_CALL"
    types = [e["type"] for e in it["errors"]]
    assert "UNKNOWN_SKILL_CALL" in types and "LOG_ERROR" in types


def test_episode_tags_ticks_and_predictions():
    it = _trace().iterations[5]
    assert it["t_source"] == "docker"
    assert it["ep_ticks"] == [{"ep": "k7x2", "t": 3}]
    pin, send = it["calls"]
    assert pin["store"] == "working" and pin["op"] == "pin"
    assert send["tick_claim"] == 3 and send["prediction"] == 0.7
