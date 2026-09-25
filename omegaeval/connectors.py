"""How the harness talks to agents: one connector per agent, whatever the agent is built with.

    websocket   OmegaClaw's chat channel, or anything speaking its four frames (the default)
    http        POST each message to a URL; the response carries the reply
    command     start a process per episode; one JSON line in, one JSON line out, per message
    python      an in-process class whose act(message) returns the reply

The websocket connector is a chat channel: messages are tagged text, and the episode and step
are read back from the reply text, because OmegaClaw's loop is asynchronous. The other three are
request and response: each message goes out as JSON, and the connector binds the reply to its
step, so the agent never repeats tags. Any agent can also report what it did in between, as
trace events, which fill the same console lanes as a parsed OmegaClaw log:

    {"kind": "llm", "model": "...", "input_tokens": 1200, "output_tokens": 300}
    {"kind": "tool", "name": "websearch", "args": "...", "result": "..."}
    {"kind": "memory", "store": "vector", "op": "add", "text": "..."}
    {"kind": "reasoning", "engine": "pln", "premises": ["..."], "conclusions": ["..."]}
    {"kind": "context", "chars": 5400, "sections": {"system": 1200, "history": 4200}}
    {"kind": "error", "message": "..."}
"""
from __future__ import annotations

import atexit
import importlib
import json
import logging
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import __version__
from .omega_trace import PRED_RE, TICK_CLAIM_RE

log = logging.getLogger("omegaeval.connectors")

PROTOCOL = "omegaeval.agent/1"
TRANSPORTS = ("websocket", "http", "command", "python")
EVENT_KINDS = ("llm", "tool", "memory", "reasoning", "context", "error")
CHUNK_BYTES = 56_000      # OmegaClaw's websocket client refuses frames over 64 KiB
END_GRACE_S = 10.0        # how long the end of an episode waits for an agent still answering
MAX_REPLY_BYTES = 16 << 20
STDERR_BYTES = 10 << 20
POSIX = os.name == "posix"


@dataclass
class Message:
    """One message from the harness to an agent, before it is put on any wire."""
    episode: str
    step: int
    kind: str                         # brief | observation | info | end | hive
    role: str = "actor"               # actor | advisor
    text: str = ""                    # the observation, the information, the end notice or a relayed message
    instructions: str = ""            # the benchmark's brief and rules, on the first message only
    actions: list[str] = field(default_factory=list)
    expects_reply: bool = False
    free_text: bool = False
    predictions: bool = False
    benchmark: str = ""
    task: str = ""
    seed: int = 0
    actor: str = ""                   # for advisors: who takes the actions
    sender: str = ""                  # for relayed hive messages


@dataclass
class Incoming:
    """A reply from an agent, with the step it answers when that is known."""
    text: str                         # exactly what the agent sent, as recorded
    received: float
    client_seq: str = ""
    step: int | None = None
    episode: str | None = None
    action_text: str = ""             # the action or answer, protocol controls removed
    prediction: float | None = None
    note: str = ""                    # "announcement" for a start-up message that is not a reply


def transport_of(spec) -> str:
    connect = getattr(spec, "connect", None) or {}
    found = [t for t in ("http", "command", "python") if t in connect]
    return found[0] if found else "websocket"


class Connector:
    transport = "websocket"
    confirms_reads = False            # True when the agent acknowledges every message it has read

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.version: str | None = None

    @property
    def connected(self) -> bool:
        return True

    def status(self) -> dict:
        return {"agent": self.agent_id, "transport": self.transport, "connected": self.connected,
                "version": self.version}

    def begin_episode(self, episode: str, record_dir: Path | None = None) -> None:
        pass

    def deliver(self, msg: Message) -> tuple[list[int], str]:
        raise NotImplementedError

    def drain(self) -> list[Incoming]:
        return []

    def drain_traces(self) -> list[tuple[float, dict]]:
        return []

    def has_read(self, episode: str, step: int) -> bool:
        return False

    def end_episode(self) -> None:
        pass


# -- the chat channel ---------------------------------------------------------------------------
def chunks(tag: str, body: str) -> list[str]:
    """Split a long message into parts that each carry the tag and fit the agent's frame limit."""
    whole = f"{tag} {body}"
    if len(whole.encode("utf-8")) <= CHUNK_BYTES:
        return [whole]
    limit = CHUNK_BYTES - 200
    parts: list[str] = []
    cur = ""
    for line in body.split("\n"):
        while len(line.encode("utf-8")) > limit:          # a single line longer than a part
            if cur:
                parts.append(cur)
                cur = ""
            n = limit
            while len(line[:n].encode("utf-8")) > limit:
                n = int(n * 0.9)
            parts.append(line[:n])
            line = line[n:]
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate.encode("utf-8")) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    head = tag[:-1]
    return [f"{head} part {i + 1}/{len(parts)}] {p}" for i, p in enumerate(parts)]


def strip_controls(text: str, prediction: bool = False) -> str:
    """Remove the leading episode tag and step number, and the trailing prediction when predictions
    are on, so only the action or answer is left. Later look-alikes in an answer ("t=5 s") stay."""
    import re

    t = re.sub(r"(?:^|\s)ep=[A-Za-z0-9_-]+", " ", text, count=1)
    t = TICK_CLAIM_RE.sub(" ", t, count=1)
    if prediction:
        found = list(PRED_RE.finditer(t))
        if found:
            last = found[-1]
            t = t[:last.start()] + " " + t[last.end():]
    return " ".join(t.replace('"', " ").split()).strip(" .!")


class ChatConnector(Connector):
    """OmegaClaw's websocket chat channel. Messages are tagged text:

        [omegaeval ep=3f9a1c1 t=12]          an observation that expects a reply
        [omegaeval ep=3f9a1c1 t=12 info]     information to read, no reply expected

    and a reply starts with the same episode tag and step: "ep=3f9a1c1 t=12 move N". The tags close
    three OmegaClaw traps: its loop ignores a message identical to the previous one, its send skill
    drops a reply identical to the previous reply, and the tags match each loop turn in its log to
    the step it read, whatever the clocks say."""
    transport = "websocket"

    def __init__(self, link, predictions: bool = False):
        super().__init__(link.agent_id)
        self.link = link
        self.predictions = predictions

    @property
    def connected(self) -> bool:
        return self.link.connected

    def status(self) -> dict:
        return {**self.link.status(), "transport": self.transport}

    @property
    def version(self) -> str | None:              # the "Omega version=" message OmegaClaw sends on start
        return self.link.version

    @version.setter
    def version(self, value) -> None:
        pass

    def begin_episode(self, episode: str, record_dir: Path | None = None) -> None:
        self.link.begin_episode()

    def end_episode(self) -> None:
        self.link.end_episode()

    @staticmethod
    def tag(msg: Message) -> str:
        if msg.kind == "end":
            return f"[omegaeval ep={msg.episode} END]"
        if msg.kind == "hive":
            return "[omegaeval hive]"
        return f"[omegaeval ep={msg.episode} t={msg.step}{'' if msg.expects_reply else ' info'}]"

    def render(self, msg: Message) -> str:
        if msg.kind in ("end", "hive"):
            return f"{msg.sender}: {msg.text}" if msg.kind == "hive" else msg.text
        hint = ""
        if msg.expects_reply:
            if msg.role != "actor":
                hint = f"Advise: ep={msg.episode} t={msg.step} <suggestion>"
            else:
                hint = f"Reply: ep={msg.episode} t={msg.step} {'<your answer>' if msg.free_text else '<action>'}"
                hint += " p=<0..1>" if msg.predictions else ""
        if msg.kind != "brief":
            body = msg.text
            if msg.expects_reply:
                body += (f"\nActions: {' | '.join(msg.actions)}" if msg.actions else "") + f"\n{hint}"
            return body
        lines = [f"Benchmark episode started: {msg.benchmark} (task {msg.task}, seed {msg.seed}).", msg.instructions]
        if msg.role == "actor":
            lines.append("How to act: reply with your send skill, one message per step, starting with the episode "
                         "tag and step number exactly as shown in the Reply line.")
            if msg.predictions:
                lines.append("Add your probability of solving this episode at the end, for example p=0.5.")
        else:
            lines.append(f"You are advising. {msg.actor} takes the actions. Messages you send are relayed to the hive.")
        if msg.actions:
            lines.append(f"Actions: {' | '.join(msg.actions)}")
        lines.append(("Observation: " if msg.expects_reply else "Information: ") + msg.text)
        if hint:
            lines.append(hint)
        return "\n".join(x for x in lines if x)

    def deliver(self, msg: Message) -> tuple[list[int], str]:
        tag, body = self.tag(msg), self.render(msg)
        return [self.link.send(part) for part in chunks(tag, body)], f"{tag} {body}"

    def drain(self) -> list[Incoming]:
        import re

        out = []
        for t_recv, text, cseq in self.link.drain():
            if text.startswith(("Omega version=", "Omega unknown")):
                out.append(Incoming(text=text, received=t_recv, client_seq=cseq, note="announcement"))
                continue                          # a start-up announcement: recorded, never an action
            claim = TICK_CLAIM_RE.search(text)
            ep_claim = re.search(r"(?:^|\s)ep=([A-Za-z0-9_-]+)", text)
            pred = list(PRED_RE.finditer(text)) if self.predictions else []
            out.append(Incoming(text=text, received=t_recv, client_seq=cseq,
                                step=int(claim.group(1)) if claim else None,
                                episode=ep_claim.group(1) if ep_claim else None,
                                action_text=strip_controls(text, prediction=self.predictions),
                                prediction=float(pred[-1].group(1)) if pred else None))
        return out

    def drain_traces(self) -> list[tuple[float, dict]]:
        return self.link.drain_traces()


# -- request and response ---------------------------------------------------------------------------
def payload(agent_id: str, msg: Message) -> dict:
    d = {"protocol": PROTOCOL, "agent": agent_id, **asdict(msg)}
    return {k: v for k, v in d.items() if v not in ("", None) or k in ("text",)}


def parse_reply(raw) -> tuple[str | None, float | None, list[dict]]:
    """An agent's answer to one message: a reply string, or a dict with reply, p and trace."""
    if raw is None:
        return None, None, []
    if isinstance(raw, str):
        return (raw.strip() or None), None, []
    if not isinstance(raw, dict):
        return str(raw), None, []
    reply = raw.get("reply")
    p = raw.get("p")
    try:
        p = None if p is None else max(0.0, min(1.0, float(p)))
    except (TypeError, ValueError):
        p = None
    trace = raw.get("trace") or []
    return (str(reply).strip() or None) if reply is not None else None, p, \
        [e for e in trace if isinstance(e, dict)] if isinstance(trace, list) else []


class _Session:
    """One episode's queues. A call still running when its episode ends keeps writing here, where
    nothing reads any more, so it cannot leak into the next episode."""

    def __init__(self) -> None:
        self.jobs: "queue.Queue[tuple[int, Message, dict] | None]" = queue.Queue()
        self.inbox: "queue.Queue[Incoming]" = queue.Queue()
        self.traces: "queue.Queue[tuple[float, dict]]" = queue.Queue()
        self.read: set[tuple[str, int]] = set()
        self.closed = threading.Event()


class StructuredConnector(Connector):
    """Sends each message as JSON and binds the answer to its step. One worker thread per episode
    keeps messages in order; the answer to an information message confirms that the agent read it.
    An agent still answering when its episode ends gets END_GRACE_S, then is left behind."""
    confirms_reads = True

    def __init__(self, agent_id: str, spec, timeout: float = 600.0):
        super().__init__(agent_id)
        self.spec = spec
        self.timeout = timeout
        self._s = _Session()
        self._seq = 0
        self._worker: threading.Thread | None = None
        self.last_error: str | None = None
        self.calls = 0

    def status(self) -> dict:
        return {**super().status(), "calls": self.calls, "last_error": self.last_error}

    def begin_episode(self, episode: str, record_dir: Path | None = None) -> None:
        self._s = _Session()
        self._worker = threading.Thread(target=self._work, args=(self._s,), name=f"agent-{self.agent_id}",
                                        daemon=True)
        self._worker.start()

    def end_episode(self) -> None:
        self._s.jobs.put(None)
        if self._worker is not None:
            self._worker.join(timeout=END_GRACE_S)
            if self._worker.is_alive():
                log.warning("agent %s is still answering after its episode ended; leaving it behind", self.agent_id)
        self._s.closed.set()
        self._worker = None

    def deliver(self, msg: Message) -> tuple[list[int], str]:
        self._seq += 1
        body = payload(self.agent_id, msg)
        self._s.jobs.put((self._seq, msg, body))
        return [self._seq], json.dumps(body, ensure_ascii=False)

    def drain(self) -> list[Incoming]:
        return _drain(self._s.inbox)

    def drain_traces(self) -> list[tuple[float, dict]]:
        return _drain(self._s.traces)

    def has_read(self, episode: str, step: int) -> bool:
        return (episode, step) in self._s.read

    def call(self, body: dict):
        raise NotImplementedError

    def _work(self, s: _Session) -> None:
        while True:
            job = s.jobs.get()
            if job is None or s.closed.is_set():
                return
            seq, msg, body = job
            try:
                raw = self.call(body)
                self.calls += 1
                self.last_error = None
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:500]
                log.warning("agent %s did not answer step %s: %s", self.agent_id, msg.step, self.last_error)
                s.traces.put((time.time(), {"type": "trace", "step": msg.step,
                                            "events": [{"kind": "error", "message": self.last_error}]}))
                continue
            reply, p, events = parse_reply(raw)
            now = time.time()
            if isinstance(raw, dict) and isinstance(raw.get("concepts"), list):
                s.traces.put((now, {"type": "trace", "kind": "concepts", "step": msg.step, "concepts": raw["concepts"]}))
            if events:
                s.traces.put((now, {"type": "trace", "step": msg.step, "events": events}))
            if msg.kind in ("info", "brief", "observation"):
                s.read.add((msg.episode, msg.step))
            if reply and msg.expects_reply:
                s.inbox.put(Incoming(text=reply, received=now, client_seq=str(seq), step=msg.step,
                                     episode=msg.episode, action_text=" ".join(reply.split()), prediction=p))


def _drain(q: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


class HttpConnector(StructuredConnector):
    """POSTs each message as JSON to the agent's URL. The response body is the answer: JSON with
    reply, p and trace, or plain text taken as the reply. The agent's token, if any, is sent as a
    bearer token so the agent can tell the harness from anyone else."""
    transport = "http"

    def call(self, body: dict):
        url = self.spec.connect["http"]
        headers = {"Content-Type": "application/json", "User-Agent": f"omegaeval/{__version__}"}
        if getattr(self.spec, "token", ""):
            headers["Authorization"] = f"Bearer {self.spec.token}"
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = resp.read(MAX_REPLY_BYTES + 1)
            if len(data) > MAX_REPLY_BYTES:
                raise ValueError(f"the answer is over {MAX_REPLY_BYTES >> 20} MiB")
            text = data.decode("utf-8", errors="replace")
            if "json" in (resp.headers.get("Content-Type") or "") or text.lstrip().startswith(("{", "null")):
                return json.loads(text) if text.strip() else None
            return text


# Agent processes started for an episode never outlive the harness, even one stopped mid-episode.
_children: set[subprocess.Popen] = set()
_children_lock = threading.Lock()


def _signal(proc: subprocess.Popen, sig: int) -> None:
    try:
        if POSIX:
            os.killpg(proc.pid, sig)          # the agent and every process it started
        else:
            proc.terminate()
    except OSError:
        pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _stop(proc: subprocess.Popen | None) -> None:
    """Close the agent's input and give it five seconds to exit. Then stop whatever is left of its
    process group, such as a server a wrapper script started: politely first, then by force."""
    if proc is None:
        return
    try:
        if proc.stdin:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    if POSIX:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if not _group_alive(proc.pid):
                break
            _signal(proc, sig)
            deadline = time.monotonic() + 5
            while _group_alive(proc.pid) and time.monotonic() < deadline:
                proc.poll()                   # reap the agent itself once it exits
                time.sleep(0.05)
    elif proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    proc.poll()
    with _children_lock:
        _children.discard(proc)


@atexit.register
def _stop_children() -> None:
    with _children_lock:
        procs = list(_children)
    for proc in procs:
        if proc.poll() is None:
            _signal(proc, signal.SIGTERM)


def _read_answers(proc: subprocess.Popen, answers: queue.Queue, traces: queue.Queue) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            obj = line
        if isinstance(obj, dict) and obj.get("type") == "trace":
            traces.put((time.time(), obj))
        else:
            answers.put(obj)
    answers.put(None)


def _keep_stderr(proc: subprocess.Popen, path: Path | None) -> None:
    """Keep what the agent writes to standard error, up to STDERR_BYTES, and keep draining the pipe
    after that so the agent never blocks on it."""
    assert proc.stderr is not None
    kept = 0
    fh = open(path, "a", encoding="utf-8") if path is not None else None
    try:
        for line in proc.stderr:
            if fh is None or kept > STDERR_BYTES:
                continue
            kept += len(line)
            fh.write(line if kept <= STDERR_BYTES else f"[omegaeval: stopped keeping standard error at "
                                                         f"{STDERR_BYTES >> 20} MiB]\n")
            fh.flush()
    finally:
        if fh is not None:
            fh.close()


class CommandConnector(StructuredConnector):
    """Starts the agent's command once per episode and speaks JSON Lines with it: one message per
    line on its standard input, one answer per line on its standard output. Lines it writes with
    "type": "trace" are trace reports; what it writes to standard error is kept as evidence. At the
    end of the episode its input is closed, and it is stopped with its process group if it lingers."""
    transport = "command"

    def __init__(self, agent_id: str, spec, timeout: float = 600.0):
        super().__init__(agent_id, spec, timeout)
        self.proc: subprocess.Popen | None = None
        self._answers: "queue.Queue[dict | str | None]" = queue.Queue()

    @property
    def connected(self) -> bool:
        return self.proc is None or self.proc.poll() is None

    def begin_episode(self, episode: str, record_dir: Path | None = None) -> None:
        super().begin_episode(episode, record_dir)
        cmd = self.spec.connect["command"]
        argv = cmd if isinstance(cmd, list) else shlex.split(cmd)
        if record_dir is not None:
            record_dir.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1,
                                     cwd=self.spec.connect.get("cwd"), start_new_session=POSIX)
        with _children_lock:
            _children.add(self.proc)
        self._answers = queue.Queue()
        threading.Thread(target=_read_answers, args=(self.proc, self._answers, self._s.traces), daemon=True).start()
        threading.Thread(target=_keep_stderr, args=(self.proc, record_dir / "stderr.log" if record_dir else None),
                         daemon=True).start()

    def call(self, body: dict):
        proc, answers = self.proc, self._answers
        if proc is None or proc.poll() is not None:
            raise RuntimeError(f"the agent's process exited with code {proc.returncode if proc else '?'}")
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(body, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        try:
            answer = answers.get(timeout=self.timeout)
        except queue.Empty:
            raise TimeoutError(f"no answer within {self.timeout:.0f} s") from None
        if answer is None:
            raise RuntimeError("the agent's process closed its output")
        return answer

    def end_episode(self) -> None:
        self._s.jobs.put(None)
        if self._worker is not None:
            self._worker.join(timeout=END_GRACE_S)        # time to take the end message
        _stop(self.proc)                                   # also frees a call still waiting on the process
        self.proc = None
        super().end_episode()


class PythonConnector(StructuredConnector):
    """Loads module:Class, makes one instance per episode, and calls its act(message). act returns
    the reply as a string, or a dict with reply, p and trace, or None for no reply. It runs inside
    the harness, so an act() that never returns cannot be stopped: use command to isolate an agent."""
    transport = "python"

    def begin_episode(self, episode: str, record_dir: Path | None = None) -> None:
        import sys

        cwd = self.spec.connect.get("cwd")
        if cwd and cwd not in sys.path:
            sys.path.insert(0, cwd)               # where the agent's module lives
        module, _, name = self.spec.connect["python"].partition(":")
        cls = getattr(importlib.import_module(module), name)
        self.agent = cls(**(self.spec.connect.get("options") or {}))
        super().begin_episode(episode, record_dir)

    def call(self, body: dict):
        return self.agent.act(body)

    def end_episode(self) -> None:
        super().end_episode()
        close = getattr(getattr(self, "agent", None), "close", None)
        if callable(close):
            close()


def preflight(spec) -> tuple[bool | None, str]:
    """Whether the harness can reach an http, command or python agent right now: True, False, or
    None when an http agent is not listening yet. Checked before any attempt is claimed."""
    import shutil
    import socket
    import sys
    from urllib.parse import urlparse

    connect = spec.connect
    transport = transport_of(spec)
    if transport == "http":
        u = urlparse(connect["http"])
        try:
            socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=2).close()
            return True, f"{connect['http']} is listening"
        except OSError as exc:
            return None, f"nothing answers at {connect['http']} ({type(exc).__name__}); start the agent first"
    if transport == "command":
        cmd = connect["command"]
        argv = cmd if isinstance(cmd, list) else shlex.split(cmd)
        found = bool(argv) and (shutil.which(argv[0]) or (Path(connect.get("cwd") or ".") / argv[0]).exists())
        return bool(found), f"{argv[0] if argv else 'the command'} {'found' if found else 'not found on PATH'}"
    if transport == "python":
        module, _, name = connect["python"].partition(":")
        cwd = connect.get("cwd")
        if cwd and cwd not in sys.path:
            sys.path.insert(0, cwd)
        try:
            ok = callable(getattr(getattr(importlib.import_module(module), name), "act", None))
        except Exception as exc:
            where = f" (looked in {cwd} and the installed packages)" if cwd else ""
            return False, f"cannot load {connect['python']}: {type(exc).__name__}: {exc}{where}"
        return ok, f"{connect['python']} {'has act()' if ok else 'has no act() method'}"
    return True, "reached over the websocket gateway"


def connector_for(spec, gateway=None, predictions: bool = False, timeout: float = 600.0) -> Connector:
    transport = transport_of(spec)
    if transport == "http":
        return HttpConnector(spec.id, spec, timeout)
    if transport == "command":
        return CommandConnector(spec.id, spec, timeout)
    if transport == "python":
        return PythonConnector(spec.id, spec, timeout)
    if gateway is None or spec.id not in gateway.links:
        raise KeyError(f"agent {spec.id!r} has no websocket connection registered")
    return ChatConnector(gateway.links[spec.id], predictions)


# -- reported traces -----------------------------------------------------------------------------------
def reported_iterations(env_events: list[dict], agent: str) -> list[dict]:
    """Trace events an agent reported, as the same per-turn records a parsed OmegaClaw log gives,
    so the console's lanes and the Result Record work for any agent."""
    out = []
    ep = next((e.get("ep_tag") for e in env_events if e.get("kind") == "episode_start"), None)
    for e in env_events:
        if e.get("kind") != "agent_trace" or e.get("agent") != agent:
            continue
        frame = e.get("trace") or {}
        events = frame.get("events")
        if not isinstance(events, list):
            continue
        it = {"i": len(out) + 1, "t0": e.get("received"), "t1": e.get("received"), "t_source": "reported",
              "idle": False, "human_msg": None,
              "ep_ticks": [{"ep": ep, "t": frame["step"]}] if isinstance(frame.get("step"), int) else [],
              "prompt": None, "llm": [], "calls": [], "errors": [], "warnings": [], "source": "reported"}
        for ev in events:
            if not isinstance(ev, dict):
                continue
            kind = ev.get("kind")
            if kind == "llm":
                it["llm"].append({"provider": ev.get("provider"), "model": ev.get("model"),
                                  "finish_reason": ev.get("finish_reason"),
                                  "prompt_tokens": _int(ev.get("input_tokens")),
                                  "cached_tokens": _int(ev.get("cached_tokens")),
                                  "completion_tokens": _int(ev.get("output_tokens")),
                                  "reasoning_tokens": _int(ev.get("reasoning_tokens")), "usage_logged": True})
            elif kind == "tool":
                it["calls"].append({"skill": str(ev.get("name") or "tool"), "lane": "tool",
                                    "args": [_text(ev.get("args"))], "result": _text(ev.get("result"))})
            elif kind == "memory":
                op = str(ev.get("op") or "add")
                it["calls"].append({"skill": f"memory {op}", "lane": "memory", "store": str(ev.get("store") or "memory"),
                                    "op": op, "args": [_text(ev.get("text"))], "result": _text(ev.get("result"))})
            elif kind == "reasoning":
                it["calls"].append({"skill": "reasoning", "lane": "reasoning", "engine": str(ev.get("engine") or "llm"),
                                    "premises": [_term(p) for p in ev.get("premises") or []],
                                    "conclusions": [_term(c) for c in ev.get("conclusions") or []],
                                    "args": [], "result": ""})
            elif kind == "context":
                it["prompt"] = {"chars": _int(ev.get("chars")), "sections": ev.get("sections") or {},
                                "input": _text(ev.get("input"))}
            elif kind == "error":
                it["errors"].append({"type": str(ev.get("type") or "error"), "message": _text(ev.get("message"))})
        out.append(it)
    return out


def _term(v) -> dict:
    """A premise or conclusion as the console draws it: a term, with a truth value when one is given."""
    if isinstance(v, dict):
        return {"term": _text(v.get("term") or v.get("text")), "f": v.get("f"), "c": v.get("c")}
    return {"term": _text(v), "f": None, "c": None}


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _text(v, limit: int = 4000) -> str:
    if v is None:
        return ""
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s[:limit]
