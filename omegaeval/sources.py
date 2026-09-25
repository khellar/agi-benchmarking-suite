"""Where an agent's evidence comes from: its log stream and its memory stores.

Each agent in omegaeval.toml names one log source and, optionally, one memory
location. Both are read-only: the harness never writes into an agent.

    logs   = { docker = "omega" }                       # docker logs -f -t omega
    logs   = { docker = "omega", host = "ssh://user@gpu-box" }
    logs   = { file = "/srv/agents/oma-1/omega.log" }   # standalone: run.sh ... 2>> omega.log
    memory = { docker = "omega" }                       # docker exec, read-only queries
    memory = { home = "/srv/agents/oma-1/PeTTa" }       # standalone PeTTa root
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .omega_trace import EP_TAG_RE, TraceBuilder

DOCKER_HISTORY = "/PeTTa/repos/Omega/memory/history.metta"
DOCKER_CHROMA = "/PeTTa/chroma_db/chroma.sqlite3"

_FINGERPRINT_PY = r"""
import json, hashlib, sqlite3, sys
h, c = sys.argv[1], sys.argv[2]
out = {}
try:
    b = open(h, 'rb').read(); out['history_bytes'] = len(b); out['history_sha256'] = hashlib.sha256(b).hexdigest()
except Exception as e:
    out['history_error'] = str(e)
try:
    db = sqlite3.connect('file:' + c + '?mode=ro', uri=True)
    out['chroma_embeddings'] = db.execute('select count(*) from embeddings').fetchone()[0]
    out['chroma_memories'] = db.execute(
        "select count(*) from embedding_metadata where key='time' and string_value != 'knowledge_prior'").fetchone()[0]
except Exception as e:
    out['chroma_error'] = str(e)
print(json.dumps(out))
"""


def _docker(spec: dict) -> list[str]:
    cmd = ["docker"]
    if spec.get("host"):
        cmd += ["-H", spec["host"]]
    return cmd


def evidence_grade(logs: dict | None) -> str:
    """How far a log source can be trusted not to have been written by the agent itself."""
    if not logs:
        return "none: no log source"
    if "docker" in logs:
        return "daemon: docker holds the container's stderr, and the agent's skills cannot write to it"
    if "file" in logs:
        return ("file: the agent's own process writes this file, so its file skills could too; "
                "use docker logs for evidence-grade runs")
    return "unknown"


def memory_fingerprint(spec: dict | None) -> dict:
    """Sizes and hashes of the agent's history and Chroma store, read-only."""
    t = datetime.now(timezone.utc).isoformat()
    if not spec:
        return {"available": False, "reason": "no memory location configured for this agent", "t": t}
    try:
        if "docker" in spec:
            cmd = _docker(spec) + ["exec", spec["docker"], "python3", "-c", _FINGERPRINT_PY,
                                   spec.get("history", DOCKER_HISTORY), spec.get("chroma", DOCKER_CHROMA)]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if out.returncode != 0:
                return {"available": False, "reason": (out.stderr or out.stdout).strip()[:300], "t": t}
            data = json.loads(out.stdout.strip().splitlines()[-1])
        elif "home" in spec:
            home = Path(spec["home"])
            hist = Path(spec.get("history", home / "repos/Omega/memory/history.metta"))
            chroma = Path(spec.get("chroma", home / "chroma_db/chroma.sqlite3"))
            data = {}
            if hist.exists():
                b = hist.read_bytes()
                data.update(history_bytes=len(b), history_sha256=hashlib.sha256(b).hexdigest())
            else:
                data["history_error"] = f"{hist} not found"
            if chroma.exists():
                db = sqlite3.connect(f"file:{chroma}?mode=ro", uri=True)
                data["chroma_embeddings"] = db.execute("select count(*) from embeddings").fetchone()[0]
                data["chroma_memories"] = db.execute(
                    "select count(*) from embedding_metadata where key='time' "
                    "and string_value != 'knowledge_prior'").fetchone()[0]
                db.close()
            else:
                data["chroma_error"] = f"{chroma} not found"
        else:
            return {"available": False, "reason": f"unknown memory spec {spec}", "t": t}
    except Exception as exc:  # the fingerprint is evidence, never a reason to stop an episode
        return {"available": False, "reason": str(exc)[:300], "t": t}
    data.update(available=True, t=t)
    return data


class LogTap(threading.Thread):
    """Follows one agent's log from now on; writes the raw slice and parsed iterations."""

    def __init__(self, agent_id: str, spec: dict | None, out_dir: Path, tz: str | None = None):
        super().__init__(name=f"logtap-{agent_id}", daemon=True)
        self.agent_id = agent_id
        self.spec = spec or {}
        self.out_dir = out_dir
        self.raw_path = out_dir / "omega.log"
        self.trace_path = out_dir / "trace.jsonl"
        self.meta_path = out_dir / "trace-meta.json"
        self.builder = TraceBuilder()
        self.started = datetime.now(timezone.utc)
        self._halt = threading.Event()
        self._proc: subprocess.Popen | None = None
        self.error: str | None = None
        self.lines = 0
        self.llm_calls = 0                      # CHARS_SENT lines: one per LLM call
        self._seen: set[tuple[str, int]] = set()
        self._seen_lock = threading.Lock()
        out_dir.mkdir(parents=True, exist_ok=True)

    def has_read(self, ep: str, tick: int) -> bool:
        """True once the agent's log shows a loop turn that read the message tagged ep/tick."""
        with self._seen_lock:
            return (ep, tick) in self._seen

    @property
    def available(self) -> bool:
        return bool(self.spec) and self.error is None

    def run(self) -> None:
        try:
            if "docker" in self.spec:
                self._follow_docker()
            elif "file" in self.spec:
                self._follow_file(Path(self.spec["file"]))
            else:
                self.error = "no log source configured for this agent"
        except Exception as exc:
            self.error = str(exc)[:500]
        finally:
            self._finish()

    def stop(self, grace: float = 0.0) -> None:
        if grace:
            time.sleep(grace)
        self._halt.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self.join(timeout=10)

    # -- followers --------------------------------------------------------------------
    def _follow_docker(self) -> None:
        since = self.started.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        cmd = _docker(self.spec) + ["logs", "-f", "-t", "--since", since, self.spec["docker"]]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                      encoding="utf-8", errors="replace", bufsize=1)
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self._line(line)
            if self._halt.is_set():
                break
        if self._proc.poll() not in (None, 0, -15) and not self._halt.is_set():
            self.error = f"docker logs exited with code {self._proc.returncode}"

    def _follow_file(self, path: Path) -> None:
        while not path.exists() and not self._halt.is_set():
            time.sleep(0.5)
        fh = open(path, "r", encoding="utf-8", errors="replace")
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        buf = ""
        while not self._halt.is_set():
            chunk = fh.readline()
            if chunk:
                buf += chunk
                if buf.endswith("\n"):
                    self._line(buf)
                    buf = ""
                pos = fh.tell()
                continue
            try:
                if path.stat().st_size < pos:     # truncated or rotated
                    fh.close()
                    fh = open(path, "r", encoding="utf-8", errors="replace")
                    pos = 0
            except FileNotFoundError:
                pass
            time.sleep(0.2)
        # read whatever arrived during the grace period
        for chunk in fh:
            self._line(chunk)
        fh.close()

    # -- recording ----------------------------------------------------------------------
    def _line(self, line: str) -> None:
        with open(self.raw_path, "a", encoding="utf-8") as raw:
            raw.write(line if line.endswith("\n") else line + "\n")
        closed = self.builder.feed(line, self.lines)
        self.lines += 1
        if "(HUMAN-MSG: " in line:
            with self._seen_lock:
                for ep, t in EP_TAG_RE.findall(line):
                    self._seen.add((ep, int(t)))
        elif "(CHARS_SENT: " in line:
            self.llm_calls += 1
        if closed is not None:
            self._write_iteration(closed)

    def _write_iteration(self, it: dict) -> None:
        with open(self.trace_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")

    def _finish(self) -> None:
        last = self.builder.finish()
        if last is not None:
            self._write_iteration(last)
        meta = {"agent": self.agent_id, "source": self.spec, "error": self.error,
                "started": self.started.isoformat(), "startup": self.builder.startup,
                "lines": self.lines, "summary": self.builder.summary()}
        self.meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
