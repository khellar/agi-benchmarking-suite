"""The registry: benchmarks people can load, and result releases anyone can verify.

Entries are TOML files in a registry folder:

    registry/benchmarks/<id>.toml       a benchmark and the omegaeval.toml block that loads it
    registry/results/<release>.toml     a release made with omegaeval release, packed by omegaeval bundle

Anyone registers through GitHub: an issue form that a workflow turns into a pull request, or a
pull request that adds the file directly. The checks here run on every pull request, and the
site build embeds the entries, with each result's numbers recomputed from its bundle.

    python -m omegaeval.registry check [--fetch] [files ...]
    python -m omegaeval.registry build --out site/registry.json [--fetch]
    python -m omegaeval.registry intake --event "$GITHUB_EVENT_PATH"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from . import __version__
from .config import BUILTIN_ENVS, check_id, unknown_keys

ROOT = Path(__file__).resolve().parents[1] / "registry"
KINDS = ("interactive", "qa", "memory")
SCORING = ("deterministic", "judge", "mixed")
HANDLE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
RELEASE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_BUNDLE = 300 * 1024 * 1024
MAX_UNPACKED = 4 * MAX_BUNDLE         # a compressed bundle may not unpack to more than this

# (field, required, meaning). docs/REGISTRY.md documents exactly these, and a test checks it.
BENCHMARK_FIELDS = [
    ("id", True, "The benchmark id: 1 to 40 lowercase letters, digits, - or _. It names the file too."),
    ("label", True, "Short display name, such as HLE. At most 24 characters."),
    ("name", True, "Full name. At most 100 characters."),
    ("kind", True, "interactive, qa or memory."),
    ("description", True, "One or two sentences on what it measures. At most 400 characters."),
    ("homepage", True, "https URL of the benchmark, its paper or its dataset."),
    ("maintainers", True, "GitHub handles of the people who answer for the entry."),
    ("config", True, "The omegaeval.toml block that loads it: one [environments.<id>] table and its options."),
    ("scoring", False, "deterministic, judge or mixed."),
    ("install", False, "The command that installs the adapter, when it lives in a package."),
    ("license", False, "The license of the benchmark's data or code."),
    ("tags", False, "Keywords, such as reasoning or memory."),
]
RESULT_FIELDS = [
    ("release", True, "The release name given to omegaeval release. It names the file too."),
    ("benchmark", True, "The id of the benchmark the release covers: registered, or built in."),
    ("agent", True, "The agent or hive id in the release."),
    ("date", True, "The day the release was made, as YYYY-MM-DD."),
    ("bundle", True, "https URL of the file made by omegaeval bundle, such as a GitHub release asset."),
    ("bundle_sha256", True, "The sha256 omegaeval bundle printed for that file."),
    ("maintainers", True, "GitHub handles of the people who answer for the entry."),
    ("agent_description", False, "What the agent is: version, model and settings."),
    ("judged_by", False, "Who or what judged the answers, when a judge graded them."),
    ("notes", False, "Anything a reader needs to interpret the results."),
]
# Issue form labels, in docs/REGISTRY.md and .github/ISSUE_TEMPLATE, mapped to entry fields.
FORM_LABELS = {
    "Benchmark id": "id", "Label": "label", "Full name": "name", "Shape": "kind", "Description": "description",
    "Homepage": "homepage", "Maintainers": "maintainers", "Config block": "config", "Scoring": "scoring",
    "Install command": "install", "License": "license", "Tags": "tags",
    "Release name": "release", "Benchmark": "benchmark", "Agent": "agent", "Release date": "date",
    "Bundle URL": "bundle", "Bundle sha256": "bundle_sha256", "Agent description": "agent_description",
    "Judged by": "judged_by", "Notes": "notes",
}
LISTS = {"maintainers", "tags"}


# -- reading ------------------------------------------------------------------------------------
def load(root: Path = ROOT) -> tuple[dict[str, dict], dict[str, dict]]:
    """benchmarks by id and results by release name, each with the file it came from."""
    benchmarks, results = {}, {}
    for f in sorted((Path(root) / "benchmarks").glob("*.toml")):
        entry = tomllib.loads(f.read_text(encoding="utf-8"))
        benchmarks[entry.get("id") or f.stem] = {**entry, "_file": f}
    for f in sorted((Path(root) / "results").glob("*.toml")):
        entry = tomllib.loads(f.read_text(encoding="utf-8"))
        results[entry.get("release") or f.stem] = {**entry, "_file": f}
    return benchmarks, results


def _fields_errors(entry: dict, spec: list) -> list[str]:
    errors = []
    names = {f for f, _, _ in spec}
    for field, required, _ in spec:
        value = entry.get(field)
        if required and (value is None or value == "" or value == []):
            errors.append(f"{field} is required")
        elif value is not None and field in LISTS:
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                errors.append(f"{field} must be a list of text")
        elif value is not None and not isinstance(value, str):
            errors.append(f"{field} must be text")
    for extra in sorted(set(entry) - names - {"_file"}):
        errors.append(f"{extra} is not a registry field")
    for handle in entry.get("maintainers") or []:
        if isinstance(handle, str) and not HANDLE_RE.match(handle.lstrip("@")):
            errors.append(f"maintainer {handle!r} is not a GitHub handle")
    return errors


def check_benchmark(entry: dict) -> list[str]:
    errors = _fields_errors(entry, BENCHMARK_FIELDS)
    bid = entry.get("id") or ""
    if isinstance(bid, str) and bid:
        err = check_id(bid)
        if err:
            errors.append(err)
        if bid in BUILTIN_ENVS:
            errors.append(f"{bid} is a built-in sample; choose another id")
    f = entry.get("_file")
    if f is not None and bid and Path(f).stem != bid:
        errors.append(f"the file must be named {bid}.toml")
    if entry.get("kind") and entry["kind"] not in KINDS:
        errors.append(f"kind must be one of {', '.join(KINDS)}")
    if entry.get("scoring") and entry["scoring"] not in SCORING:
        errors.append(f"scoring must be one of {', '.join(SCORING)}")
    for field, limit in (("label", 24), ("name", 100), ("description", 400)):
        if isinstance(entry.get(field), str) and len(entry[field]) > limit:
            errors.append(f"{field} is longer than {limit} characters")
    if isinstance(entry.get("homepage"), str) and not entry["homepage"].startswith("https://"):
        errors.append("homepage must be an https URL")
    config = entry.get("config")
    if isinstance(config, str) and config.strip():
        try:
            data = tomllib.loads(config)
        except tomllib.TOMLDecodeError as exc:
            errors.append(f"config is not valid TOML: {exc}")
        else:
            if set(data) != {"environments"} or set(data.get("environments", {})) != {bid}:
                errors.append(f"config must hold exactly one table, [environments.{bid}], and nothing else")
            else:
                block = data["environments"][bid]
                if not isinstance(block.get("adapter"), str) or ":" not in block["adapter"]:
                    errors.append("config must name its adapter as module:Class")
                else:
                    errors += _adapter_errors(block["adapter"], entry.get("install"))
                errors += [f"config key {k} is not read by omegaeval" for k in unknown_keys(data)]
    return errors


def _package_of(install: str) -> str | None:
    """The importable name of the package an install command installs, or None."""
    m = re.fullmatch(r"\s*pip install\s+(?:-U\s+)?(\S+)\s*", install or "")
    if not m:
        return None
    target = m.group(1)
    egg = re.search(r"#egg=([A-Za-z0-9_.-]+)", target)
    if egg:
        name = egg.group(1)
    elif target.startswith("git+"):
        name = re.sub(r"\.git$", "", target.split("#")[0].split("@")[0].rstrip("/").rsplit("/", 1)[-1])
    else:
        name = re.split(r"[\[=<>!~;]", target)[0]
    return re.sub(r"[-.]", "_", name).lower() or None


def _adapter_errors(adapter: str, install: str | None) -> list[str]:
    """An adapter must be built in, or come from the package the entry's install command installs,
    so an entry can never point the harness at some other module on the reader's machine."""
    module = adapter.split(":", 1)[0]
    root = module.split(".")[0].lower()
    if root == "omegaeval":
        return [] if module.startswith("omegaeval.envs.") else ["a built-in adapter lives in omegaeval.envs"]
    pkg = _package_of(install or "")
    if pkg is None:
        return [f"adapter {adapter} is not built in, so install must say how to get it: pip install <package>"]
    if pkg != root:
        return [f"adapter module {module} must belong to the package that install installs ({pkg})"]
    return []


def check_result(entry: dict, benchmarks: dict) -> list[str]:
    errors = _fields_errors(entry, RESULT_FIELDS)
    rel = entry.get("release") or ""
    if isinstance(rel, str) and rel and not RELEASE_RE.match(rel):
        errors.append("release must be 1 to 64 lowercase letters, digits, '.', '-' or '_'")
    f = entry.get("_file")
    if f is not None and rel and Path(f).stem != rel:
        errors.append(f"the file must be named {rel}.toml")
    bench = entry.get("benchmark")
    if isinstance(bench, str) and bench and bench not in benchmarks and bench not in BUILTIN_ENVS:
        errors.append(f"benchmark {bench!r} is not registered; register it first")
    if isinstance(entry.get("date"), str):
        try:
            date.fromisoformat(entry["date"])
        except ValueError:
            errors.append("date must be YYYY-MM-DD")
    if isinstance(entry.get("bundle"), str) and not entry["bundle"].startswith("https://"):
        errors.append("bundle must be an https URL")
    if isinstance(entry.get("bundle_sha256"), str) and not SHA_RE.match(entry["bundle_sha256"]):
        errors.append("bundle_sha256 must be 64 lowercase hex digits")
    return errors


# -- bundles ---------------------------------------------------------------------------------------
def fetch(url: str, dest: Path, limit: int = MAX_BUNDLE) -> str:
    """Download a bundle, refusing anything over the size limit. Returns its sha256."""
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": f"omegaeval/{__version__}"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        total = 0
        for chunk in iter(lambda: resp.read(1 << 20), b""):
            total += len(chunk)
            if total > limit:
                raise ValueError(f"bundle is larger than {limit // (1024 * 1024)} MB")
            h.update(chunk)
            out.write(chunk)
    return h.hexdigest()


def verify_bundle(path: Path, release: str | None = None) -> dict:
    """Unpack a bundle made by omegaeval bundle, check every checksum, and summarize the release."""
    from .store import Store

    with tempfile.TemporaryDirectory(prefix="omegaeval-bundle-") as tmp:
        with tarfile.open(path, "r:gz") as tar:
            members = tar.getmembers()
            for m in members:
                if not (m.isfile() or m.isdir()) or m.name.startswith(("/", "..")) or ".." in Path(m.name).parts:
                    raise ValueError(f"bundle member {m.name!r} is not a plain file inside the bundle")
            if sum(m.size for m in members) > MAX_UNPACKED:
                raise ValueError(f"bundle unpacks to more than {MAX_UNPACKED >> 20} MB")
            try:
                tar.extractall(tmp, filter="data")
            except TypeError:                      # Python before 3.11.4; members were checked above
                tar.extractall(tmp)
        root = Path(tmp)
        info = json.loads((root / "bundle.json").read_text(encoding="utf-8")) if (root / "bundle.json").exists() else {}
        withheld = set(info.get("withheld") or [])
        names = sorted(p.parent.name for p in (root / "releases").glob("*/release.json"))
        if len(names) != 1:
            raise ValueError(f"a bundle holds exactly one release; found {names or 'none'}")
        name = names[0]
        if release and name != release:
            raise ValueError(f"the bundle holds release {name!r}, not {release!r}")
        store = Store(root)
        problems = [p for p in store.verify(name)
                    if not (p.endswith(" is missing") and Path(p.split(": ", 1)[1][:-len(" is missing")]).name in withheld)]
        if problems:
            raise ValueError("checksums do not match: " + "; ".join(problems[:5]))
        doc = json.loads((root / "releases" / name / "release.json").read_text(encoding="utf-8"))
        return {"release": name, "created": doc.get("created"), "harness": doc.get("harness"),
                "attempts": len(doc.get("attempts", [])), "logs_withheld": bool(withheld),
                "sets": summarize(root, doc)}


def summarize(root: Path, release_doc: dict) -> list[dict]:
    """Solve counts per set of runs in a release, computed from its result files."""
    sets: dict[tuple, dict] = {}
    for a in release_doc.get("attempts", []):
        ident = a.get("identity") or {}
        key = (ident.get("env"), ident.get("task"), ident.get("subject"), ident.get("variant") or "")
        s = sets.setdefault(key, {"benchmark": key[0], "task": key[1], "agent": key[2], "variant": key[3],
                                  "episodes": 0, "graded": 0, "solved": 0, "judged": 0, "void": 0, "held": 0,
                                  "judges": [], "agent_versions": [], "_steps": []})
        s["episodes"] += 1
        try:
            res = json.loads((Path(root) / a["path"] / "result.json").read_text(encoding="utf-8"))
            man = json.loads((Path(root) / a["path"] / "manifest.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            continue
        oc = res.get("outcome") or {}
        if oc.get("success") is not None:
            s["graded"] += 1
            s["solved"] += 1 if oc["success"] else 0
        s["held"] += 1 if oc.get("needs_judge") else 0
        s["void"] += 1 if oc.get("void") else 0
        if oc.get("judged"):
            s["judged"] += 1
            by = oc["judged"].get("by")
            if by and by not in s["judges"]:
                s["judges"].append(by)
        steps = (res.get("efficiency") or {}).get("steps")
        if steps is not None:
            s["_steps"].append(steps)
        for v in ((man.get("provenance") or {}).get("agent_versions") or {}).values():
            if v and v not in s["agent_versions"]:
                s["agent_versions"].append(v)
    out = []
    for s in sets.values():
        steps = sorted(s.pop("_steps"))
        s["median_steps"] = (steps[len(steps) // 2] if len(steps) % 2 else
                             (steps[len(steps) // 2 - 1] + steps[len(steps) // 2]) / 2) if steps else None
        out.append(s)
    return sorted(out, key=lambda s: (s["benchmark"] or "", s["task"] or "", s["agent"] or "", s["variant"]))


def check_fetched(entry: dict, limit: int = MAX_BUNDLE) -> tuple[dict | None, list[str]]:
    """Download an entry's bundle and verify it. Returns the summary, or the problems."""
    with tempfile.TemporaryDirectory(prefix="omegaeval-fetch-") as tmp:
        dest = Path(tmp) / "bundle.tar.gz"
        try:
            digest = fetch(entry["bundle"], dest, limit)
        except Exception as exc:
            return None, [f"could not download the bundle: {exc}"]
        if digest != entry.get("bundle_sha256"):
            return None, [f"the bundle's sha256 is {digest}, not the one registered"]
        try:
            summary = verify_bundle(dest, entry.get("release"))
        except Exception as exc:
            return None, [f"the bundle does not verify: {exc}"]
    wrong = sorted({s["benchmark"] for s in summary["sets"]} - {entry.get("benchmark")})
    if wrong:
        return None, [f"the release holds runs of {', '.join(map(str, wrong))}, not only {entry.get('benchmark')}"]
    agents = {s["agent"] for s in summary["sets"]}
    if entry.get("agent") not in agents:
        return None, [f"the release holds no runs of agent {entry.get('agent')!r}; it has {sorted(agents)}"]
    return summary, []


# -- the site's data -----------------------------------------------------------------------------
def build(root: Path = ROOT, fetch_bundles: bool = False) -> dict:
    benchmarks, results = load(root)
    out_b, out_r = [], []
    for bid, e in sorted(benchmarks.items()):
        errs = check_benchmark(e)
        if errs:
            print(f"skipping benchmark {bid}: {'; '.join(errs)}", file=sys.stderr)
            continue
        out_b.append({k: v for k, v in e.items() if k != "_file"})
    for rel, e in sorted(results.items(), key=lambda kv: kv[1].get("date", ""), reverse=True):
        errs = check_result(e, benchmarks)
        if errs:
            print(f"skipping result {rel}: {'; '.join(errs)}", file=sys.stderr)
            continue
        item = {k: v for k, v in e.items() if k != "_file"}
        if fetch_bundles:
            summary, problems = check_fetched(e)
            item.update(verified=summary is not None, summary=summary, problems=problems)
        else:
            item.update(verified=None, summary=None, problems=["not checked in this build"])
        out_r.append(item)
    return {"schema": "omegaeval.registry/1", "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "harness": __version__, "builtin": sorted(BUILTIN_ENVS), "benchmarks": out_b, "results": out_r}


# -- issue forms -------------------------------------------------------------------------------------
def parse_issue_form(body: str) -> dict[str, str]:
    """The fields of a GitHub issue form body: '### Label' headings, each followed by its value."""
    fields, current, lines = {}, None, []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        m = re.match(r"^### (.+?)\s*$", line)
        if m:
            if current is not None:
                fields[current] = "\n".join(lines).strip()
            current, lines = m.group(1), []
        elif current is not None:
            lines.append(line)
    if current is not None:
        fields[current] = "\n".join(lines).strip()
    for k, v in list(fields.items()):
        if v == "_No response_":
            fields[k] = ""
        fence = re.match(r"^```[\w-]*\n(.*?)\n?```$", v, re.S)
        if fence:
            fields[k] = fence.group(1).strip()
    return fields


def entry_from_form(fields: dict[str, str]) -> tuple[str | None, dict]:
    kind = "benchmark" if "Benchmark id" in fields else "result" if "Bundle URL" in fields else None
    entry = {}
    for label, value in fields.items():
        field = FORM_LABELS.get(label)
        if not field or value == "":
            continue
        if field in LISTS:
            entry[field] = [v.strip().lstrip("@") for v in re.split(r"[,\s]+", value) if v.strip()]
        else:
            entry[field] = value.strip() if field != "config" else value.strip() + "\n"
    return kind, entry


def _toml_str(v: str) -> str:
    if "\n" in v:
        if "'''" in v:
            raise ValueError("text fields cannot contain '''")
        return "'''\n" + v.rstrip("\n") + "\n'''"
    return json.dumps(v, ensure_ascii=False)


def to_toml(entry: dict, spec: list) -> str:
    lines = []
    for field, _, _ in spec:
        if field not in entry:
            continue
        v = entry[field]
        lines.append(f"{field} = " + ("[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in v) + "]"
                                      if isinstance(v, list) else _toml_str(v)))
    return "\n".join(lines) + "\n"


def intake(event: dict, root: Path = ROOT) -> dict:
    """Turn a registration issue into an entry file. Returns what the workflow should do next."""
    issue = event.get("issue") or {}
    kind, entry = entry_from_form(parse_issue_form(issue.get("body") or ""))
    if kind is None:
        return {"action": "ignore"}
    benchmarks, _ = load(root)
    key = entry.get("id") if kind == "benchmark" else entry.get("release")
    folder = "benchmarks" if kind == "benchmark" else "results"
    path = Path(root) / folder / f"{key}.toml" if key else None
    errors = check_benchmark({**entry, "_file": path}) if kind == "benchmark" else \
        check_result({**entry, "_file": path}, benchmarks)
    author = ((issue.get("user") or {}).get("login") or "").lower()
    if not errors and author and author not in [m.lower() for m in entry.get("maintainers", [])]:
        errors.append(f"list yourself ({author}) among the maintainers")
    if not errors and kind == "benchmark" and path.exists() and \
            author not in [m.lower() for m in tomllib.loads(path.read_text()).get("maintainers", [])]:
        errors.append(f"{key} is already registered by someone else; choose another id")
    if not errors and kind == "result" and path.exists() and \
            author not in [m.lower() for m in tomllib.loads(path.read_text()).get("maintainers", [])]:
        errors.append(f"a release named {key} is already registered by someone else; choose another name")
    if errors:
        return {"action": "comment", "kind": kind, "errors": errors}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_toml(entry, BENCHMARK_FIELDS if kind == "benchmark" else RESULT_FIELDS), encoding="utf-8")
    number = issue.get("number")
    return {"action": "pull-request", "kind": kind, "key": key, "path": str(path.relative_to(Path(root).parent)),
            "branch": f"registry/{kind}-{key}",
            "title": f"Register {kind} {key}", "body": f"Registers the {kind} `{key}`. Closes #{number}."
            if number else f"Registers the {kind} `{key}`."}


# -- command line ---------------------------------------------------------------------------------------
def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m omegaeval.registry", description="Check and build the registry.")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="validate registry entries")
    c.add_argument("files", nargs="*", help="entry files; default the whole registry")
    c.add_argument("--fetch", action="store_true", help="also download and verify result bundles")
    c.add_argument("--root", default=str(ROOT))
    b = sub.add_parser("build", help="write the registry data the site reads")
    b.add_argument("--out", required=True)
    b.add_argument("--fetch", action="store_true", help="download and verify result bundles")
    b.add_argument("--root", default=str(ROOT))
    i = sub.add_parser("intake", help="turn a registration issue into an entry file (used by a workflow)")
    i.add_argument("--event", required=True, help="the GitHub event JSON")
    i.add_argument("--out", default="intake.json", help="where to write what to do next")
    i.add_argument("--root", default=str(ROOT))
    args = p.parse_args(argv)
    root = Path(args.root)
    if args.cmd == "check":
        benchmarks, results = load(root)
        chosen = {str(Path(f).resolve()) for f in args.files}
        problems = 0
        for bid, e in benchmarks.items():
            if chosen and str(Path(e["_file"]).resolve()) not in chosen:
                continue
            errs = check_benchmark(e)
            problems += len(errs)
            print(f"{'ok  ' if not errs else 'FAIL'}  benchmark {bid}" + "".join(f"\n      {x}" for x in errs))
        for rel, e in results.items():
            if chosen and str(Path(e["_file"]).resolve()) not in chosen:
                continue
            errs = check_result(e, benchmarks)
            if not errs and args.fetch:
                summary, errs = check_fetched(e)
            problems += len(errs)
            print(f"{'ok  ' if not errs else 'FAIL'}  result {rel}" + "".join(f"\n      {x}" for x in errs))
        print(f"{problems} problem(s)" if problems else "all entries pass")
        return 1 if problems else 0
    if args.cmd == "build":
        data = build(root, args.fetch)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"{len(data['benchmarks'])} benchmark(s), {len(data['results'])} result(s) -> {args.out}")
        return 0
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    out = intake(event, root)
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    if out.get("action") == "comment":
        Path(args.out).with_suffix(".md").write_text(
            "Thanks. The registration has problems to fix; edit the issue and the check runs again:\n\n"
            + "\n".join(f"- {e}" for e in out["errors"]) + "\n", encoding="utf-8")
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
