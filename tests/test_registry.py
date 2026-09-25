"""The registry: entry checks, issue-form intake, verified result bundles, omegaeval add, the site."""
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import threading
import time
import tomllib
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omegaeval import registry
from omegaeval.cli import main
from omegaeval.config import AgentSpec, load

from test_benchmarks import ToolUser

ROOT = Path(__file__).resolve().parents[1]
GOOD = {"id": "my-bench", "label": "My bench", "name": "My benchmark", "kind": "qa",
        "description": "Questions about things.", "homepage": "https://example.org",
        "maintainers": ["someone"], "scoring": "deterministic",
        "config": '[environments.my-bench]\nadapter = "omegaeval.envs.qa:QADataset"\n\n'
                  '[environments.my-bench.options]\npath = "/data/q.jsonl"\n'}


def test_the_seeded_registry_passes():
    benchmarks, results = registry.load(ROOT / "registry")
    assert {"hle", "longmemeval-s"} <= set(benchmarks)
    for e in benchmarks.values():
        assert registry.check_benchmark(e) == []
    for e in results.values():
        assert registry.check_result(e, benchmarks) == []


@pytest.mark.parametrize("change,problem", [
    ({"id": "My Bench"}, "must be 1-40 lowercase"),
    ({"id": "keydoor"}, "built-in sample"),
    ({"homepage": "http://example.org"}, "https"),
    ({"kind": "game"}, "kind must be"),
    ({"maintainers": ["not a handle!"]}, "GitHub handle"),
    ({"color": "blue"}, "not a registry field"),
    ({"config": '[environments.my-bench]\nadapter = "omegaeval.envs.qa:QADataset"\nlabl = "x"\n'}, "not read by omegaeval"),
    ({"config": '[environments.other]\nadapter = "omegaeval.envs.qa:QADataset"\n'}, "exactly one table"),
    ({"config": '[agents.x]\ntoken = "t"\n[environments.my-bench]\nadapter = "omegaeval.envs.qa:QADataset"\n'}, "exactly one table"),
    ({"config": '[environments.my-bench]\nadapter = "subprocess:Popen"\n'}, "install must say"),
    ({"config": '[environments.my-bench]\nadapter = "evil:Thing"\n', "install": "pip install arc-omegaeval"}, "must belong to the package"),
])
def test_bad_benchmark_entries_are_refused(change, problem):
    errors = registry.check_benchmark({**GOOD, **change})
    assert any(problem in e for e in errors), errors


def test_a_packaged_adapter_must_come_from_its_install_command():
    entry = {**GOOD, "install": "pip install arc-omegaeval",
             "config": '[environments.my-bench]\nadapter = "arc_omegaeval.games:Arc"\n'}
    assert registry.check_benchmark(entry) == []


def _form(fields: dict) -> str:
    out = []
    for label, value in fields.items():
        out.append(f"### {label}\n\n{value if value != '' else '_No response_'}\n")
    return "\n".join(out)


def test_issue_forms_match_the_parser():
    for name, kind in (("register-benchmark.yml", "benchmark"), ("register-results.yml", "result")):
        text = (ROOT / ".github" / "ISSUE_TEMPLATE" / name).read_text()
        labels = re.findall(r"^\s+label: (.+)$", text, re.M)
        fields = [registry.FORM_LABELS.get(lab) for lab in labels]
        assert None not in fields, [lab for lab in labels if lab not in registry.FORM_LABELS]
        spec = registry.BENCHMARK_FIELDS if kind == "benchmark" else registry.RESULT_FIELDS
        assert set(fields) == {f for f, _, _ in spec}, name


def test_a_registration_issue_becomes_an_entry(tmp_path):
    root = tmp_path / "registry"
    body = _form({"Benchmark id": "my-bench", "Label": "My bench", "Full name": "My benchmark", "Shape": "qa",
                  "Description": "Questions about things.", "Homepage": "https://example.org",
                  "Maintainers": "someone, @Other-Person", "Config block": "```toml\n" + GOOD["config"] + "```",
                  "Scoring": "", "Install command": "", "License": "", "Tags": "exam, reasoning"})
    out = registry.intake({"issue": {"number": 7, "body": body, "user": {"login": "someone"}}}, root)
    assert out["action"] == "pull-request" and out["branch"] == "registry/benchmark-my-bench"
    entry = tomllib.loads((root / "benchmarks" / "my-bench.toml").read_text())
    assert entry["maintainers"] == ["someone", "Other-Person"] and entry["tags"] == ["exam", "reasoning"]
    assert registry.check_benchmark({**entry, "_file": root / "benchmarks" / "my-bench.toml"}) == []
    assert "Closes #7" in out["body"]
    other = registry.intake({"issue": {"number": 8, "body": body, "user": {"login": "intruder"}}}, root)
    assert other["action"] == "comment" and any("maintainers" in e for e in other["errors"])
    bad = registry.intake({"issue": {"number": 9, "body": body.replace("my-bench", "My Bench"),
                                     "user": {"login": "someone"}}}, root)
    assert bad["action"] == "comment"
    assert registry.intake({"issue": {"number": 10, "body": "Just a question", "user": {"login": "a"}}}, root) == \
        {"action": "ignore"}


def _release(monkeypatch, tmp_path, name="demo-1"):
    monkeypatch.chdir(tmp_path)
    assert main(["baseline", "oracle", "qa-sample", "--seeds", "1-3"]) == 0
    assert main(["baseline", "random", "qa-sample", "--seeds", "1-3"]) == 0
    assert main(["release", name, "qa-sample"]) == 0
    return name


def test_bundles_verify_and_their_numbers_are_recomputed(tmp_path, monkeypatch, capsys):
    name = _release(monkeypatch, tmp_path)
    capsys.readouterr()
    assert main(["bundle", name]) == 0
    printed = capsys.readouterr().out
    digest = re.search(r"sha256 ([0-9a-f]{64})", printed).group(1)
    path = tmp_path / f"{name}.tar.gz"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    summary = registry.verify_bundle(path, name)
    sets = {s["agent"]: s for s in summary["sets"]}
    assert summary["attempts"] == 6 and sets["baseline-oracle"]["solved"] == 3 and sets["baseline-oracle"]["graded"] == 3
    assert summary["logs_withheld"] is False
    with pytest.raises(ValueError):
        registry.verify_bundle(path, "another-name")
    tampered = tmp_path / "tampered.tar.gz"                       # change one result, keep the checksums
    with tarfile.open(path) as src, tarfile.open(tampered, "w:gz") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read() if m.isfile() else None
            if m.name.endswith("result.json") and data:
                data = data.replace(b'"success": false', b'"success": true')
                m.size = len(data)
            dst.addfile(m, __import__("io").BytesIO(data) if data is not None else None)
    with pytest.raises(ValueError, match="checksums"):
        registry.verify_bundle(tampered, name)


def test_withheld_logs_still_verify(rig, tmp_path):
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
    runner.run("agent", "moons", "all", 1)
    agent.stop_flag.set()
    store.release("moons-1", store.attempts(env="moons"))
    toml = tmp_path / "omegaeval.toml"
    toml.write_text(f'[harness]\nstore = "{store.root}"\n')
    out = tmp_path / "moons-1.tar.gz"
    assert main(["bundle", "moons-1", "-c", str(toml), "--out", str(out), "--withhold-logs"]) == 0
    with tarfile.open(out) as tar:
        assert not [n for n in tar.getnames() if n.endswith("omega.log.gz")]
    summary = registry.verify_bundle(out, "moons-1")
    assert summary["logs_withheld"] is True and summary["sets"][0]["episodes"] == 1


def test_a_hosted_bundle_is_fetched_and_checked(tmp_path, monkeypatch):
    name = _release(monkeypatch, tmp_path)
    assert main(["bundle", name]) == 0
    handler = partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/{name}.tar.gz"
    digest = hashlib.sha256((tmp_path / f"{name}.tar.gz").read_bytes()).hexdigest()
    entry = {"release": name, "benchmark": "qa-sample", "agent": "baseline-oracle", "bundle": url,
             "bundle_sha256": digest}
    try:
        summary, problems = registry.check_fetched(entry)
        assert problems == [] and summary["release"] == name
        _, wrong = registry.check_fetched({**entry, "bundle_sha256": "0" * 64})
        assert "sha256" in wrong[0]
        _, wrong = registry.check_fetched({**entry, "agent": "someone-else"})
        assert "no runs of agent" in wrong[0]
        _, wrong = registry.check_fetched({**entry, "benchmark": "keydoor"})
        assert "not only keydoor" in wrong[0]
    finally:
        httpd.shutdown()


def test_add_pulls_a_registered_benchmark_into_the_config(tmp_path, monkeypatch, capsys):
    data = registry.build(ROOT / "registry")
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps(data))
    monkeypatch.chdir(tmp_path)
    assert main(["add", "HLE", "--registry", str(reg)]) == 0
    assert "options.path" in capsys.readouterr().out
    cfg = load(tmp_path / "omegaeval.toml")
    assert cfg.environments["hle"]["label"] == "HLE" and cfg.environments["hle"]["options"]["judge_all"] is True
    assert cfg.unknown == []
    with pytest.raises(SystemExit):
        main(["add", "hle", "--registry", str(reg)])
    with pytest.raises(SystemExit):
        main(["add", "nothing-like-it", "--registry", str(reg)])


def test_the_site_builds_with_every_link_filled_in(tmp_path):
    out = tmp_path / "site"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_site.py"), "--out", str(out), "--no-console"],
                   check=True, capture_output=True)
    for page in ("index.html", "registry.html", "site.css", "registry.json", ".nojekyll"):
        assert (out / page).exists(), page
    for page in ("index.html", "registry.html"):
        html = (out / page).read_text()
        assert "{{" not in html and "https://github.com/khellar/agi-benchmarking-suite/issues/new?template=register" in html
    data = json.loads((out / "registry.json").read_text())
    assert {b["id"] for b in data["benchmarks"]} >= {"hle", "longmemeval-s"}


def test_a_bundle_that_unpacks_too_large_is_refused(tmp_path, monkeypatch):
    import io
    import tarfile

    from omegaeval import registry
    monkeypatch.setattr(registry, "MAX_UNPACKED", 1000)
    bomb = tmp_path / "bomb.bundle.tgz"
    with tarfile.open(bomb, "w:gz") as tar:
        data = b"0" * 5000                                   # compresses to almost nothing
        info = tarfile.TarInfo("releases/r1/zeros.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="unpacks to more than"):
        registry.verify_bundle(bomb)
