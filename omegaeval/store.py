"""The run store: append-only history of every episode, laid out for publication.

    <store>/store.json                          store id and format version
    <store>/index.jsonl                         ledger: one line per event, never rewritten (source of truth)
    <store>/index.sqlite                        query index derived from the ledger and results; safe to delete
    <store>/runs/<run_id>/<attempt>/
        manifest.json                           inputs and provenance
        env.jsonl                               what the harness sent and received (evidence)
        judgments.jsonl                         verdicts, when a judge graded the answer (evidence)
        agents/<agent>/omega.log.gz             raw log slice, compressed when the episode ends (evidence)
        agents/<agent>/trace.jsonl              parsed iterations (derived; parser version inside)
        agents/<agent>/trace-meta.json
        result.json                             Result Record (derived)
        checksums.json                          sha256 of every file above
    <store>/releases/<name>/release.json        frozen set of attempts with checksums, for citation

A run id is the benchmark id plus a hash of the episode's inputs, for example
hle-3f9a1c1b2c3d, so re-running the same inputs adds an attempt under the same id and
never overwrites an earlier one. Files are the evidence. The SQLite index only makes
queries fast; it is rebuilt from the ledger and the result files whenever it is missing.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import RUN_SCHEMA, __version__
from .omega_trace import PARSER_VERSION

STORE_SCHEMA = "omegaeval.store/1"
INDEX_VERSION = "2"
_lock = threading.Lock()

# Ledger fields the index keeps, and that selections and --where filters can use.
COLUMNS = ["env", "task", "seed", "subject", "label", "variant", "memory_mode", "status", "terminal", "success",
           "needs_judge", "judged", "void", "steps", "started_at", "ended_at", "harness", "agent_version", "path",
           "note", "exact"]
FILTER_KEYS = ["run_id", "attempt"] + COLUMNS
_BOOLS = {"success", "needs_judge", "judged", "void", "exact"}
_INTS = {"attempt", "seed", "steps"}


def _alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def run_id_for(identity: dict) -> str:
    """The benchmark id, a dash, and 12 hex digits of the hash of the episode's inputs."""
    digest = hashlib.sha256(canonical(identity).encode("utf-8")).hexdigest()[:12]
    return f"{identity.get('env') or 'run'}-{digest}"


def run_hash(run_id: str) -> str:
    return run_id.rsplit("-", 1)[-1]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def append_line(path: Path, obj, durable: bool = False) -> None:
    """Append one JSON line in a single write. A crash can leave a torn line without its newline; that
    line is ended first, so it cannot swallow this record. durable waits until the line is on disk."""
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    with open(path, "a+b", buffering=0) as fh:
        if fh.seek(0, os.SEEK_END):
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                data = b"\n" + data
        fh.write(data)
        if durable:
            os.fsync(fh.fileno())


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue   # a line being written right now
    return out


def raw_log(agent_dir: Path) -> Path | None:
    """The raw log slice of one agent in an attempt, compressed or not."""
    for name in ("omega.log.gz", "omega.log"):
        if (agent_dir / name).exists():
            return agent_dir / name
    return None


def open_log(path: Path):
    return gzip.open(path, "rt", encoding="utf-8", errors="replace") if path.suffix == ".gz" else \
        open(path, encoding="utf-8", errors="replace")


def compress_log(agent_dir: Path) -> Path | None:
    """Replace omega.log with omega.log.gz. Raw logs carry full prompts and compress well."""
    src = agent_dir / "omega.log"
    if not src.exists():
        return None
    dst = agent_dir / "omega.log.gz"
    tmp = agent_dir / "omega.log.gz.tmp"
    with open(src, "rb") as fin, gzip.open(tmp, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, 1 << 20)
    tmp.replace(dst)
    src.unlink()
    return dst


def compact_result(res: dict) -> dict:
    """The part of a Result Record that comparisons read: small enough to send for every run."""
    if not isinstance(res, dict):
        return {}
    oc = res.get("outcome") or {}
    out: dict = {"outcome": {k: oc[k] for k in ("available", "reason", "success", "utility", "terminal", "normalized",
                                                "needs_judge", "void", "floor", "ceiling") if k in oc}}
    if oc.get("judged"):
        out["outcome"]["judged"] = {k: oc["judged"].get(k) for k in ("verdict", "by", "via", "judgments", "unanimous")}
    for block, keys in (("efficiency", ("steps", "max_steps", "invalid_actions", "wall_seconds")),
                        ("regret", ("available", "reason", "oracle_steps", "extra_steps")),
                        ("cost", ("available", "reason", "usd")),
                        ("prediction", ("available", "reason", "n", "brier")),
                        ("tool_policy", ("violations", "void")),
                        ("concepts", ("available", "reason", "distinct", "reidentified"))):
        b = res.get(block)
        if isinstance(b, dict):
            out[block] = {k: b[k] for k in keys if k in b}
    cog = res.get("cognition")
    if isinstance(cog, dict):
        if cog.get("available") is False:
            out["cognition"] = cog
        else:
            out["cognition"] = {
                agent: c if not isinstance(c, dict) or c.get("available") is False else
                {"llm_calls": c.get("llm_calls"), "iterations": c.get("iterations"), "models": c.get("models"),
                 "tokens": c.get("tokens") if not isinstance(c.get("tokens"), dict) else
                 {k: c["tokens"].get(k) for k in ("available", "reason", "prompt_tokens", "cached_tokens",
                                                  "completion_tokens", "reasoning_tokens") if k in c["tokens"]},
                 "reasoning": c.get("reasoning"), "memory": c.get("memory"), "errors": c.get("errors")}
                for agent, c in cog.items()}
    return out


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.jsonl"
        self.db_path = self.root / "index.sqlite"
        self._db: sqlite3.Connection | None = None
        self._dblock = threading.RLock()
        self.meta = self._meta()

    def _meta(self) -> dict:
        p = self.root / "store.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        meta = {"schema": STORE_SCHEMA, "store_id": uuid.uuid4().hex[:12], "created": _now(),
                "created_by": f"omegaeval {__version__}", "host": socket.gethostname()}
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        try:
            os.link(tmp, p)          # first writer wins; another process may have created it
        except FileExistsError:
            tmp.unlink(missing_ok=True)
            return json.loads(p.read_text(encoding="utf-8"))
        except OSError:
            tmp.replace(p)
        else:
            tmp.unlink(missing_ok=True)
        return meta

    # -- writing -----------------------------------------------------------------------------
    def new_attempt(self, identity: dict) -> tuple[str, int, Path]:
        rid = run_id_for(identity)
        return (rid, *self._claim(rid))

    def _claim(self, rid: str, start: int | None = None) -> tuple[int, Path]:
        base = self.root / "runs" / rid
        with _lock:
            base.mkdir(parents=True, exist_ok=True)
            existing = [int(p.name) for p in base.iterdir() if p.is_dir() and p.name.isdigit()]
            attempt = max(existing + [(start or 1) - 1], default=0) + 1
            while True:                  # another process may claim the same number first
                path = base / str(attempt)
                try:
                    path.mkdir()
                    break
                except FileExistsError:
                    attempt += 1
            (path / "agents").mkdir()
        return attempt, path

    @staticmethod
    def owner() -> dict:
        return {"pid": os.getpid(), "host": socket.gethostname()}

    def recover_interrupted(self) -> list[dict]:
        """Mark attempts left running by a harness process that no longer exists."""
        fixed = []
        for rec in self.attempts(status="running"):
            try:
                p = self.path(rec["run_id"], rec["attempt"])
                m = json.loads((p / "manifest.json").read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            owner = (m.get("harness") or {}).get("owner") or {}
            if owner.get("host") == socket.gethostname() and _alive(owner.get("pid")):
                continue
            m["status"] = "interrupted"
            m["ended_at"] = _now()
            self.write_json(p / "manifest.json", m)
            self.seal(p)
            self.ledger({"event": "interrupted", "run_id": rec["run_id"], "attempt": rec["attempt"],
                         "status": "interrupted"})
            fixed.append(rec)
        return fixed

    def ledger(self, record: dict) -> None:
        with _lock:
            append_line(self.index_path, {"t": _now(), **record}, durable=True)
        self.sync()

    @staticmethod
    def write_json(path: Path, obj) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def append_event(path: Path, event: dict) -> None:
        append_line(path, event)

    def seal(self, path: Path) -> dict:
        sums = {}
        for f in sorted(path.rglob("*")):
            if f.is_file() and f.name != "checksums.json" and not f.name.endswith(".tmp"):
                sums[str(f.relative_to(path))] = sha256_file(f)
        self.write_json(path / "checksums.json", {"algorithm": "sha256", "files": sums})
        return sums

    # -- the query index ---------------------------------------------------------------------------
    def _con(self) -> sqlite3.Connection:
        if self._db is None:
            con = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL") if not _synced_folder(self.root) else None
            con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            row = con.execute("SELECT value FROM meta WHERE key = 'index_version'").fetchone()
            if not row or row[0] != INDEX_VERSION:     # built by another version: the index is derived, so rebuild
                con.executescript("DROP TABLE IF EXISTS attempts; DELETE FROM meta;")
                con.execute("INSERT INTO meta VALUES ('index_version', ?)", (INDEX_VERSION,))
            con.executescript(f"""
                CREATE TABLE IF NOT EXISTS attempts (
                    run_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                    {', '.join(c + (' INTEGER' if c in _INTS or c in _BOOLS else ' TEXT') for c in COLUMNS)},
                    summary TEXT, PRIMARY KEY (run_id, attempt));
                CREATE INDEX IF NOT EXISTS attempts_env ON attempts (env, started_at);
                CREATE INDEX IF NOT EXISTS attempts_status ON attempts (status);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """)
            self._db = con
        return self._db

    def sync(self) -> int:
        """Bring the index up to date with the ledger. Returns the number of events applied."""
        size = self.index_path.stat().st_size if self.index_path.exists() else 0
        with self._dblock:
            con = self._con()
            if self._offset(con) == size:
                return 0
            con.execute("BEGIN IMMEDIATE")
            try:
                off = self._offset(con)
                if off > size:                      # the ledger was replaced: start over
                    con.execute("DELETE FROM attempts")
                    off = 0
                with open(self.index_path, "rb") as fh:
                    fh.seek(off)
                    data = fh.read(size - off)
                end = data.rfind(b"\n") + 1         # complete lines only
                touched: set[tuple] = set()
                n = 0
                for line in data[:end].splitlines():
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if self._apply(con, e):
                        n += 1
                        if e.get("event") in ("finished", "judged", "rebuilt", "interrupted", "merged"):
                            touched.add((e["run_id"], int(e["attempt"])))
                for key in touched:
                    self._summarize(con, *key)
                con.execute("INSERT OR REPLACE INTO meta VALUES ('ledger_offset', ?)", (str(off + end),))
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
            return n

    @staticmethod
    def _offset(con) -> int:
        row = con.execute("SELECT value FROM meta WHERE key = 'ledger_offset'").fetchone()
        return int(row[0]) if row else 0

    def _apply(self, con, e: dict) -> bool:
        rid, att = e.get("run_id"), e.get("attempt")
        if not rid or att is None:
            return False
        ev = e.get("event")
        fields = {k: e[k] for k in COLUMNS if k in e}
        if ev == "judged":
            fields["judged"] = True
        if ev == "replayed":
            fields = {"exact": bool(e.get("exact"))}
        if ev == "rebuilt":
            fields = {}
        fields = {k: (int(v) if isinstance(v, bool) else v) for k, v in fields.items()
                  if not isinstance(v, (dict, list))}
        con.execute("INSERT OR IGNORE INTO attempts (run_id, attempt) VALUES (?, ?)", (rid, int(att)))
        if fields:
            con.execute(f"UPDATE attempts SET {', '.join(k + ' = ?' for k in fields)} WHERE run_id = ? AND attempt = ?",
                        (*fields.values(), rid, int(att)))
        return True

    def _summarize(self, con, rid: str, att: int) -> None:
        p = self.root / "runs" / rid / str(att)
        try:
            res = json.loads((p / "result.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            res = None
        try:
            m = json.loads((p / "manifest.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            m = {}
        prov = m.get("provenance") or {}
        summary = {"result": compact_result(res) if res else None, "agent_versions": prov.get("agent_versions"),
                   "models_seen": prov.get("models_seen"), "env_version": (m.get("identity") or {}).get("env_version")}
        version = next((v for v in (prov.get("agent_versions") or {}).values() if v), None)
        con.execute("UPDATE attempts SET summary = ?, agent_version = ? WHERE run_id = ? AND attempt = ?",
                    (json.dumps(summary), version, rid, att))

    def reindex(self) -> int:
        """Empty the index and rebuild it from the ledger and the result files. Done in place, so a
        running harness that has the index open keeps working."""
        with self._dblock:
            con = self._con()
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("DELETE FROM attempts")
                con.execute("DELETE FROM meta WHERE key = 'ledger_offset'")
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
            return self.sync()

    @staticmethod
    def _where(filters: dict) -> tuple[str, list]:
        sql, args = [], []
        for k, v in filters.items():
            if k == "variant_is":                     # exact variant, where "" means no variant
                sql.append("COALESCE(variant, '') = ?")
                args.append(v or "")
                continue
            if v is None or v == "":
                continue
            if k == "q":
                like = f"%{v}%"
                sql.append("(run_id LIKE ? OR subject LIKE ? OR variant LIKE ? OR label LIKE ? OR env LIKE ? "
                           "OR task LIKE ? OR CAST(seed AS TEXT) = ?)")
                args += [like, like, like, like, like, like, str(v)]
                continue
            if k not in FILTER_KEYS:
                raise ValueError(f"cannot filter on {k!r}; use one of: {', '.join(FILTER_KEYS)}")
            if k in _BOOLS:
                v = 1 if str(v).lower() in ("1", "true", "yes") else 0
            elif k in _INTS:
                v = int(v)
            sql.append(f"{k} = ?")
            args.append(v)
        return (" WHERE " + " AND ".join(sql)) if sql else "", args

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = {k: r[k] for k in r.keys() if k != "summary"}
        for k in _BOOLS:
            if d.get(k) is not None:
                d[k] = bool(d[k])
        return d

    # -- reading ---------------------------------------------------------------------------------
    def attempts(self, limit: int | None = None, offset: int = 0, **filters) -> list[dict]:
        """Latest known state of the matching attempts, newest first."""
        self.sync()
        where, args = self._where(filters)
        sql = f"SELECT * FROM attempts{where} ORDER BY started_at DESC, run_id, attempt"
        if limit:
            sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        with self._dblock:
            return [self._row(r) for r in self._con().execute(sql, args).fetchall()]

    def count(self, **filters) -> int:
        self.sync()
        where, args = self._where(filters)
        with self._dblock:
            return self._con().execute(f"SELECT COUNT(*) FROM attempts{where}", args).fetchone()[0]

    def env_counts(self) -> dict[str, dict]:
        self.sync()
        with self._dblock:
            rows = self._con().execute("SELECT env, COUNT(*) AS n, MAX(started_at) AS last FROM attempts "
                                       "GROUP BY env").fetchall()
        return {r["env"]: {"runs": r["n"], "last": r["last"]} for r in rows if r["env"]}

    def ledger_rev(self) -> int:
        """Changes whenever the ledger grows: a cheap signal that something new happened."""
        return self.index_path.stat().st_size if self.index_path.exists() else 0

    def path(self, run_id: str, attempt: int | str) -> Path:
        if "/" in run_id or ".." in run_id or not str(attempt).isdigit():
            raise FileNotFoundError(f"no attempt {run_id}/{attempt}")
        p = self.root / "runs" / run_id / str(attempt)
        if not p.is_dir():
            raise FileNotFoundError(f"no attempt {run_id}/{attempt}")
        return p

    def bundle(self, run_id: str, attempt: int | str, with_prompts: bool = True) -> dict:
        """Everything the console needs for one attempt, read from disk as it is right now."""
        p = self.path(run_id, attempt)
        manifest = json.loads((p / "manifest.json").read_text(encoding="utf-8"))
        result = json.loads((p / "result.json").read_text(encoding="utf-8")) if (p / "result.json").exists() else None
        agents = {}
        for adir in sorted((p / "agents").iterdir()) if (p / "agents").exists() else []:
            if not adir.is_dir():
                continue
            iters = read_jsonl(adir / "trace.jsonl")
            if not with_prompts:
                for it in iters:
                    if it.get("prompt"):
                        it["prompt"] = {k: v for k, v in it["prompt"].items() if k in ("chars", "sections", "line")}
                        it["prompt"]["redacted"] = True
                    for llm in it.get("llm", []):
                        llm.pop("raw", None)
            meta_path = adir / "trace-meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else None
            raw = raw_log(adir)
            lines = (meta or {}).get("lines")
            if lines is None and raw is not None:
                with open_log(raw) as fh:
                    lines = sum(1 for _ in fh)
            agents[adir.name] = {"iterations": iters, "meta": meta, "raw_lines": lines or 0}
        return {"schema": RUN_SCHEMA, "run_id": run_id, "attempt": int(attempt), "manifest": manifest,
                "result": result, "env": read_jsonl(p / "env.jsonl"), "agents": agents,
                "exported_by": f"omegaeval {__version__}", "parser": PARSER_VERSION}

    def results(self, limit: int | None = None, **filters) -> list[dict]:
        """Compact results of finished attempts, for comparisons. The full record is in result.json."""
        self.sync()
        where, args = self._where(filters)
        where = (where + " AND " if where else " WHERE ") + "status IS NOT 'running' AND summary IS NOT NULL"
        sql = f"SELECT * FROM attempts{where} ORDER BY started_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        out = []
        with self._dblock:
            rows = self._con().execute(sql, args).fetchall()
        for r in rows:
            s = json.loads(r["summary"])
            if not s.get("result"):
                continue
            out.append({"run_id": r["run_id"], "attempt": r["attempt"], "started_at": r["started_at"],
                        "status": r["status"], "agent_versions": s.get("agent_versions"),
                        "models_seen": s.get("models_seen"),
                        "identity": {"env": r["env"], "env_version": s.get("env_version"), "task": r["task"],
                                     "seed": r["seed"], "subject": r["subject"], "label": r["label"],
                                     "variant": r["variant"] or "", "memory_mode": r["memory_mode"]},
                        "result": s["result"]})
        return out

    def progress(self, **filters) -> list[dict]:
        """Solve counts per set of runs (benchmark, task, subject, variant) and per agent version.
        Small whatever the size of the store, so the Progress view never needs every result."""
        self.sync()
        where, args = self._where(filters)
        where = (where + " AND " if where else " WHERE ") + "status IS NOT 'running'"
        with self._dblock:
            rows = self._con().execute("SELECT env, task, subject, variant, agent_version, success, steps, started_at "
                                       f"FROM attempts{where}", args).fetchall()
        sets: dict[tuple, dict] = {}
        for r in rows:
            key = (r["env"], r["task"], r["subject"], r["variant"] or "")
            s = sets.setdefault(key, {"env": key[0], "task": key[1], "subject": key[2], "variant": key[3],
                                      "_steps": [], "n": 0, "graded": 0, "solved": 0, "first": r["started_at"],
                                      "versions": {}})
            ver = r["agent_version"] or "version not reported"
            v = s["versions"].setdefault(ver, {"version": ver, "_steps": [], "n": 0, "graded": 0, "solved": 0,
                                               "first": r["started_at"]})
            for bucket in (s, v):
                bucket["n"] += 1
                if r["success"] is not None:
                    bucket["graded"] += 1
                    bucket["solved"] += 1 if r["success"] else 0
                if r["steps"] is not None:
                    bucket["_steps"].append(r["steps"])
                if (r["started_at"] or "") < (bucket["first"] or "~"):
                    bucket["first"] = r["started_at"]
        out = []
        for s in sets.values():
            s["median_steps"] = _median(s.pop("_steps"))
            vers = []
            for v in s["versions"].values():
                v["median_steps"] = _median(v.pop("_steps"))
                vers.append(v)
            s["versions"] = sorted(vers, key=lambda v: v["first"] or "", reverse=True)
            out.append(s)
        return sorted(out, key=lambda s: (s["env"] or "", s["task"] or "", s["subject"] or "", s["variant"]))

    def raw_line(self, run_id: str, attempt: int | str, agent: str, n: int) -> str | None:
        if "/" in agent or ".." in agent:
            return None
        f = raw_log(self.path(run_id, attempt) / "agents" / agent)
        if f is None:
            return None
        with open_log(f) as fh:
            for i, line in enumerate(fh):
                if i == n:
                    return line.rstrip("\n")
        return None

    # -- combining stores ----------------------------------------------------------------------------
    def merge(self, other_root: str | Path) -> dict:
        """Copy another store's finished attempts into this one, for example from another machine.

        An attempt whose evidence is already here is skipped. When the attempt number is taken by
        a different episode, the copy gets the next free number, and its manifest records where
        it came from. Nothing already in this store is changed.
        """
        other = Store(other_root)
        if other.root.resolve() == self.root.resolve() or other.meta.get("store_id") == self.meta.get("store_id"):
            raise ValueError("a store cannot be merged into itself")
        counts = {"added": 0, "renumbered": 0, "skipped": 0, "running": 0}
        for rec in sorted(other.attempts(), key=lambda r: r.get("started_at") or ""):
            if rec.get("status") == "running":
                counts["running"] += 1
                continue
            src = other.path(rec["run_id"], rec["attempt"])
            evidence = _evidence_sums(src)
            rid = rec["run_id"]
            base = self.root / "runs" / rid
            same = [d for d in base.iterdir() if d.is_dir() and d.name.isdigit()
                    and _evidence_sums(d) == evidence] if base.exists() else []
            if same:
                counts["skipped"] += 1
                continue
            att, dst = self._claim(rid, start=int(rec["attempt"]))
            shutil.rmtree(dst)
            shutil.copytree(src, dst)
            m = json.loads((dst / "manifest.json").read_text(encoding="utf-8"))
            if att != int(rec["attempt"]):
                counts["renumbered"] += 1
            m["attempt"] = att
            m["merged_from"] = {"store_id": other.meta.get("store_id"), "attempt": int(rec["attempt"]),
                                "merged_at": _now()}
            self.write_json(dst / "manifest.json", m)
            if (dst / "result.json").exists():
                r = json.loads((dst / "result.json").read_text(encoding="utf-8"))
                r["attempt"] = att
                self.write_json(dst / "result.json", r)
            self.seal(dst)
            row = {k: rec[k] for k in COLUMNS if rec.get(k) is not None}
            row["path"] = str(dst.relative_to(self.root))
            self.ledger({"event": "merged", "run_id": rid, "attempt": att, **row,
                         "from_store": other.meta.get("store_id"), "from_attempt": int(rec["attempt"])})
            counts["added"] += 1
        return counts

    # -- releases ----------------------------------------------------------------------------------
    def releases(self) -> dict[str, dict]:
        out = {}
        for f in sorted((self.root / "releases").glob("*/release.json")):
            try:
                out[f.parent.name] = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                out[f.parent.name] = {"attempts": [], "error": "release.json is not valid JSON"}
        return out

    def released_in(self, run_id: str, attempt: int | str) -> list[str]:
        """Names of the releases that froze this attempt. A released attempt is never changed."""
        return [name for name, doc in self.releases().items()
                if any(a.get("run_id") == run_id and str(a.get("attempt")) == str(attempt)
                       for a in doc.get("attempts", []))]

    def verify(self, release: str | None = None) -> list[str]:
        """Problems found checking files against their checksums: one release, or every attempt."""
        problems = []

        def check(label: str, base: Path, sums: dict) -> None:
            for rel, digest in sums.items():
                f = base / rel
                if not f.is_file():
                    problems.append(f"{label}: {rel} is missing")
                elif sha256_file(f) != digest:
                    problems.append(f"{label}: {rel} changed")
            present = {str(f.relative_to(base)) for f in base.rglob("*")
                       if f.is_file() and f.name != "checksums.json" and not f.name.endswith(".tmp")}
            for rel in sorted(present - set(sums)):
                problems.append(f"{label}: {rel} is not covered by the checksums")

        if release:
            out = self.root / "releases" / release
            doc_path = out / "release.json"
            if not doc_path.exists():
                return [f"no release {release!r}"]
            want = (out / "release.sha256").read_text().split()[0] if (out / "release.sha256").exists() else None
            if want != sha256_file(doc_path):
                problems.append(f"release {release}: release.json does not match release.sha256")
            for a in json.loads(doc_path.read_text(encoding="utf-8")).get("attempts", []):
                check(f"{a['run_id']}/{a['attempt']}", self.root / a["path"], a.get("checksums") or {})
            return problems
        for rec in self.attempts():
            try:
                p = self.path(rec["run_id"], rec["attempt"])
            except FileNotFoundError:
                problems.append(f"{rec.get('run_id')}/{rec.get('attempt')}: in the ledger but not on disk")
                continue
            if rec.get("status") == "running":
                continue
            sums_path = p / "checksums.json"
            if not sums_path.exists():
                problems.append(f"{rec['run_id']}/{rec['attempt']}: no checksums.json")
                continue
            check(f"{rec['run_id']}/{rec['attempt']}", p, json.loads(sums_path.read_text(encoding="utf-8"))["files"])
        return problems

    def release(self, name: str, selected: list[dict], note: str = "") -> Path:
        out = self.root / "releases" / name
        if out.exists():
            raise FileExistsError(f"release {name!r} already exists; releases are never overwritten")
        out.mkdir(parents=True)
        items = []
        for rec in selected:
            p = self.path(rec["run_id"], rec["attempt"])
            sums_path = p / "checksums.json"
            sums = json.loads(sums_path.read_text()) if sums_path.exists() else {"files": self.seal(p)}
            manifest = json.loads((p / "manifest.json").read_text())
            items.append({"run_id": rec["run_id"], "attempt": rec["attempt"],
                          "path": str(p.relative_to(self.root)), "status": manifest.get("status"),
                          "identity": manifest.get("identity"), "checksums": sums["files"]})
        doc = {"release": name, "created": _now(), "note": note, "store_id": self.meta.get("store_id"),
               "harness": f"omegaeval {__version__}", "parser": PARSER_VERSION, "attempts": items}
        self.write_json(out / "release.json", doc)
        digest = sha256_file(out / "release.json")
        (out / "release.sha256").write_text(f"{digest}  release.json\n")
        return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _median(values: list) -> float | None:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def _evidence_sums(attempt_dir: Path) -> dict:
    """Checksums of the files that record what happened, as opposed to what was derived from them."""
    try:
        files = json.loads((attempt_dir / "checksums.json").read_text(encoding="utf-8"))["files"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return {"missing": attempt_dir.name}
    return {k: v for k, v in files.items() if k == "env.jsonl" or k.endswith("omega.log") or k.endswith("omega.log.gz")}


SYNCED_MARKERS = ("Mobile Documents", "iCloud", "Dropbox", "Google Drive", "GoogleDrive", "OneDrive", "CloudStorage")


def _synced_folder(path: Path) -> bool:
    return any(m in str(Path(path).resolve()) for m in SYNCED_MARKERS)
