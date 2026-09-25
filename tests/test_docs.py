"""The documentation and the code agree, in both directions.

Every command shown parses, every config snippet loads with no key the harness ignores, every
link resolves, the example benchmark passes conformance, the reference tables match the code,
and the generated sections of the reference are current.
"""
import argparse
import inspect
import json
import re
import shlex
import tomllib
from pathlib import Path

import pytest

from omegaeval import docgen
from omegaeval.checks import FRAME_TYPES, conform
from omegaeval.cli import build_parser
from omegaeval.config import Config, load, unknown_keys
from omegaeval.envs.base import Environment
from omegaeval.envs.chatmemory import ChatMemory
from omegaeval.envs.qa import QADataset

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", ROOT / "CHANGELOG.md", *sorted((ROOT / "docs").glob("*.md"))]
FENCE = re.compile(r"^( *)```(\w*)[^\n]*\n(.*?)^\1```[ \t]*$", re.S | re.M)
PARSER = build_parser()
SUB = next(a for a in PARSER._actions if isinstance(a, argparse._SubParsersAction))


def blocks(lang: str | None = None):
    for path in DOCS:
        for m in FENCE.finditer(path.read_text(encoding="utf-8")):
            indent, kind, body = m.group(1), m.group(2), m.group(3)
            body = "\n".join(line[len(indent):] if line.startswith(indent) else line for line in body.splitlines())
            if lang is None or kind == lang:
                yield path, body


def ids(items):
    return [f"{p.name}:{i}" for i, (p, _) in enumerate(items)]


BASH = list(blocks("bash"))
TOML = [(p, b) for p, b in blocks("toml") if not b.lstrip().startswith(("# pyproject.toml", "# registry/"))]
REGISTRY_TOML = [(p, b) for p, b in blocks("toml") if b.lstrip().startswith("# registry/")]
PYTHON = list(blocks("python"))
PLAIN = list(blocks(""))


def test_generated_reference_is_current():
    ref = ROOT / "docs" / "REFERENCE.md"
    assert docgen.render(ref.read_text(encoding="utf-8")) == ref.read_text(encoding="utf-8"), \
        "run: python -m omegaeval.docgen"


@pytest.mark.parametrize("path,body", BASH, ids=ids(BASH))
def test_every_command_shown_parses(path, body):
    for line in body.splitlines():
        line = line.split("  #")[0].strip()
        if not line.startswith("omegaeval "):
            continue
        try:
            PARSER.parse_args(shlex.split(line)[1:])
        except SystemExit:
            pytest.fail(f"{path.name}: `{line}` does not parse")


@pytest.mark.parametrize("path,body", PLAIN, ids=ids(PLAIN))
def test_command_summaries_name_real_commands_and_options(path, body):
    for line in body.splitlines():
        if not line.startswith("omegaeval "):
            continue
        words = line.split()
        name = words[1]
        assert name in SUB.choices, f"{path.name}: `omegaeval {name}` is not a command"
        flags = {o for a in SUB.choices[name]._actions for o in a.option_strings}
        for w in re.findall(r"--[a-z][a-z-]*", line):
            assert w in flags, f"{path.name}: `omegaeval {name}` has no option {w}"


def test_every_argument_is_explained():
    missing = [f"{name} {a.dest}" for name, sp in SUB.choices.items() for a in sp._actions
               if not isinstance(a, argparse._HelpAction) and not a.help]
    assert not missing


@pytest.mark.parametrize("path,body", TOML, ids=ids(TOML))
def test_config_snippets_load_and_use_only_real_keys(path, body, tmp_path):
    data = tomllib.loads(body)
    assert unknown_keys(data) == [], f"{path.name} shows keys the harness does not read"
    cfg_file = tmp_path / "omegaeval.toml"
    cfg_file.write_text(body, encoding="utf-8")
    cfg = load(cfg_file)
    for env_id in cfg.environments:
        assert cfg.registry()[env_id].get("error") is None, f"{path.name}: {env_id}"


@pytest.mark.parametrize("path,body", PYTHON, ids=ids(PYTHON))
def test_python_snippets_compile(path, body):
    compile(body, str(path), "exec")


def test_json_snippets_parse():
    for path, body in blocks("json"):
        json.loads(body)


def test_example_benchmark_passes_every_check(tmp_path):
    example = next(b for _, b in PYTHON if b.startswith("# benchmarks/hotcold.py"))
    folder = tmp_path / "benchmarks"
    folder.mkdir()
    (folder / "hotcold.py").write_text(example, encoding="utf-8")
    cfg = Config(path=None, store=tmp_path / "store", benchmark_dirs=[folder])
    assert cfg.describe("hotcold")["label"] == "Hot/cold"
    for task in ("small", "large"):
        failed = [(n, d) for n, ok, d in conform(cfg.env_factory("hotcold"), [1, 2, 3], task) if not ok]
        assert not failed, failed


def test_the_nine_checks_are_the_ones_documented():
    names = [n for n, _, _ in conform(Config(path=None, store=Path("unused")).env_factory("keydoor"), [1, 2])]
    assert len(names) == 9
    text = (ROOT / "docs" / "BENCHMARKS.md").read_text(encoding="utf-8")
    listed = re.findall(r"^\d\. ", text.split("runs nine checks")[1].split("\n\n`omegaeval doctor`")[0], re.M)
    assert len(listed) == 9


def _slug(heading: str) -> str:
    h = re.sub(r"[`*_]", "", heading.strip().lower())
    h = re.sub(r"[^\w\- ]", "", h)
    return h.replace(" ", "-")


def _anchors(path: Path) -> set:
    return {_slug(m) for m in re.findall(r"^#{1,6} (.+)$", FENCE.sub("", path.read_text(encoding="utf-8")), re.M)}


def test_every_link_resolves():
    bad = []
    for path in DOCS:
        text = FENCE.sub("", path.read_text(encoding="utf-8"))
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if re.match(r"(https?|mailto):", target):
                continue
            file, _, anchor = target.partition("#")
            dest = (path.parent / file).resolve() if file else path
            if not dest.exists():
                bad.append(f"{path.name}: {target} (no such file)")
            elif anchor and dest.suffix == ".md" and anchor not in _anchors(dest):
                bad.append(f"{path.name}: {target} (no such heading)")
    assert not bad, bad


def _table(doc: str, header: str) -> list[list[str]]:
    """Rows of the table whose header row is exactly `header`."""
    text = (ROOT / "docs" / doc).read_text(encoding="utf-8")
    assert text.count(header + "\n") == 1, f"{doc}: expected one table headed {header}"
    lines = text.split(header + "\n", 1)[1].splitlines()[1:]
    rows = []
    for line in lines:
        if not line.startswith("|"):
            break
        rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows


def _code(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


def test_reference_tables_match_the_code():
    methods = {c.split("(")[0] for row in _table("REFERENCE.md", "| Method | Side | Returns |") for c in _code(row[0])}
    public = {n for n, v in vars(Environment).items() if callable(v) and not n.startswith("_")}
    assert methods == public
    assert {_code(r[0])[0] for r in _table("REFERENCE.md", "| Type | Fields | Drawn as |")} == FRAME_TYPES
    for adapter, header in ((QADataset, "| QADataset option | Default | Meaning |"),
                            (ChatMemory, "| ChatMemory option | Default | Meaning |")):
        documented = {_code(r[0])[0] for r in _table("REFERENCE.md", header)}
        params = set(inspect.signature(adapter.__init__).parameters) - {"self", "options"}
        assert documented == params, adapter.__name__
    required = {_code(r[0])[0] for r in _table("REFERENCE.md", "| Key | Required | Meaning |") if r[1] == "yes"}
    assert required == {"id", "version", "name", "kind", "tasks", "observation", "actions", "utility"}


def test_result_record_keys_match():
    from omegaeval import result

    t = "2026-09-25T00:00:00+00:00"
    events = [{"kind": "episode_start", "t": t},
              {"kind": "terminal", "t": t, "status": "solved", "max_steps": 5, "floor": 0.0,
               "ceiling": {"utility": 1.0, "steps": 1}, "score": {"success": True, "utility": 1.0, "steps": 1}}]
    keys = set(result.compute({"run_id": "x-1", "attempt": 1}, events, {}))
    documented = {k for r in _table("REFERENCE.md", "| Key | Contents |") for k in _code(r[0])}
    assert documented == keys


def test_ledger_events_are_the_ones_documented():
    written = set()
    for f in (ROOT / "omegaeval").rglob("*.py"):
        written |= set(re.findall(r'"event": "(\w+)"', f.read_text(encoding="utf-8")))
    text = (ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8")
    sentence = text.split("The ledger records these events:")[1].split(".")[0]
    assert set(_code(sentence)) == written


def test_api_table_lists_every_route():
    from omegaeval.server import API_ROUTES

    src = (ROOT / "omegaeval" / "server.py").read_text(encoding="utf-8")
    routes = {"/" + "/".join(re.findall(r'"(\w[\w-]*)"', m)) for m in re.findall(r"parts == \[([^\]]+)\]", src)}
    routes |= set(re.findall(r'url\.path == "(/[^"]+)"', src))
    documented = {p for _, p, _ in API_ROUTES if "<" not in p and p != "/"}
    assert routes == documented


def test_store_files_are_documented(tmp_path, monkeypatch):
    from omegaeval.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["baseline", "oracle", "qa-sample", "--seeds", "1"]) == 0
    attempt = next((tmp_path / "omegaeval-store" / "runs").glob("*/1"))
    produced = {p.name for p in attempt.rglob("*") if p.is_file()} | \
               {p.name for p in (tmp_path / "omegaeval-store").iterdir() if p.is_file() and not p.name.endswith(("-wal", "-shm"))}
    text = (ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8").split("## The store")[1]
    for name in produced | {"omega.log.gz", "trace.jsonl", "trace-meta.json", "judgments.jsonl", "release.json"}:
        assert name in text, name


@pytest.mark.parametrize("path,body", REGISTRY_TOML, ids=ids(REGISTRY_TOML))
def test_registry_examples_pass_the_registry_checks(path, body):
    from omegaeval import registry

    first = body.lstrip().splitlines()[0]
    file = Path(first[2:].strip())
    entry = {**tomllib.loads(body), "_file": file}
    benchmarks, _ = registry.load(ROOT / "registry")
    errors = registry.check_benchmark(entry) if file.parent.name == "benchmarks" else \
        registry.check_result(entry, benchmarks)
    assert errors == [], f"{path.name}: {errors}"


def test_registry_field_tables_match_the_code():
    from omegaeval import registry

    for header, spec in (("| Field | Required | Meaning |", None),):
        text = (ROOT / "docs" / "REGISTRY.md").read_text(encoding="utf-8")
        tables = text.split(header + "\n")[1:]
        assert len(tables) == 2
        for table, spec in zip(tables, (registry.BENCHMARK_FIELDS, registry.RESULT_FIELDS)):
            rows = []
            for line in table.splitlines()[1:]:
                if not line.startswith("|"):
                    break
                rows.append([c.strip() for c in line.strip("|").split("|")])
            assert [(_code(r[0])[0], r[1] == "yes", r[2]) for r in rows] == spec
