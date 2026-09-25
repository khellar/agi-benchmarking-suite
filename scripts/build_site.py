"""Build the GitHub Pages site: the home page, the registry and the console demo.

    python scripts/build_site.py --out site [--fetch] [--no-console]

--fetch downloads every registered result bundle and verifies it, so the registry shows
numbers recomputed from sealed files. The console demo is exported from reference runs made
with this checkout. Links point at the repository named in pyproject.toml.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from omegaeval import __version__
from omegaeval.registry import build as build_registry
from omegaeval.server import project_links

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_RUNS = [
    ["keydoor", "--task", "classic", "--seeds", "1-5"],
    ["qa-sample", "--task", "all", "--seeds", "1-10"],
    ["memory-sample", "--task", "recall", "--seeds", "1-3"],
    ["memory-sample", "--task", "update", "--seeds", "1-3"],
]


def omegaeval(cwd: str, *args: str) -> None:
    subprocess.run([sys.executable, "-m", "omegaeval.cli", *args], cwd=cwd, check=True, stdout=subprocess.DEVNULL)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="build_site.py")
    p.add_argument("--out", default="site")
    p.add_argument("--fetch", action="store_true", help="download and verify registered result bundles")
    p.add_argument("--no-console", action="store_true", help="skip the reference runs and the console demo")
    args = p.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    repo = (project_links().get("repository") or "https://github.com/khellar/agi-benchmarking-suite").rstrip("/")
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for f in sorted((ROOT / "website").iterdir()):
        text = f.read_text(encoding="utf-8")
        for key, value in (("{{repository}}", repo), ("{{version}}", __version__), ("{{built}}", built)):
            text = text.replace(key, value)
        (out / f.name).write_text(text, encoding="utf-8")
    data = build_registry(ROOT / "registry", args.fetch)
    (out / "registry.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"registry: {len(data['benchmarks'])} benchmark(s), {len(data['results'])} result(s)")
    if not args.no_console:
        (out / "console").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="omegaeval-site-") as tmp:
            for policy in ("random", "oracle"):
                for run in REFERENCE_RUNS:
                    omegaeval(tmp, "baseline", policy, *run)
            omegaeval(tmp, "verify")
            omegaeval(tmp, "replay")
            omegaeval(tmp, "export", "--html", str((out / "console" / "index.html").resolve()))
        print(f"console: {out / 'console' / 'index.html'}")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
