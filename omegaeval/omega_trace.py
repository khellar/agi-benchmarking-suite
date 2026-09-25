"""Read OmegaClaw (github.com/singnet/Omega) log output as OmegaEval trace records.

Omega's agent loop (src/loop.metta) logs each step through Python logging using
the format in config/logging.conf:

    2026-09-25 10:00:00 | INFO     | loop | (---------iteration 17)

`docker logs -t` adds an RFC 3339 prefix with nanoseconds. Every other line on
the stream (MeTTa source dumps, Prolog translation, prints) is ignored.

Lines this parser understands, and where Omega writes them:

    (---------iteration K)                       src/loop.metta   start of loop turn K
    (HUMAN-MSG: <text>)                          src/loop.metta   new inbound message this turn
    (CHARS_SENT: N "<prompt>")                   src/loop.metta   the full prompt sent to the LLM
    [LLM_USAGE] provider=.. model=.. ...         providers/lib_llm_ext.py  token usage per call
    [LLM_RAW] provider=.. model=.. raw='..'      providers/lib_llm_ext.py  raw completion (DEBUG only)
    (RESPONSE: ((skill "arg") ...))              src/loop.metta   parsed skill calls
    (RESPONSE: (RESULTS: ((COMMAND_RETURN: (call result)) ...)))   results per call

Validated against Omega commit ee0618a (2026-09-25) and the log excerpt a
maintainer posted in singnet/Omega#311. The open pull request singnet/Omega#358
moves the LLM to the tools API and will change the RESPONSE lines. Bump
PARSER_VERSION and re-run tests/test_omega_trace.py when it lands.
"""
from __future__ import annotations

import ast
import re
from datetime import datetime, timezone
from typing import Any, Iterable

PARSER_VERSION = "omega-log/0.1 (singnet/Omega ee0618a)"

LINE_RE = re.compile(
    r"^(?:(?P<dts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))\s+)?"
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\|\s*(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s*"
    r"\|\s*(?P<name>[^|]*?)\s*\|\s?(?P<msg>.*)$"
)
ITER_RE = re.compile(r"^\(-+iteration\s+(\d+)\)$")
EP_TAG_RE = re.compile(r"\[omegaeval ep=([A-Za-z0-9_-]+) t=(\d+)")
TICK_CLAIM_RE = re.compile(r"(?:^|\s)t=(\d+)\b")
EP_CLAIM_RE = re.compile(r"(?:^|\s)ep=([A-Za-z0-9_-]+)")
PRED_RE = re.compile(r"(?:^|\s)p=(0(?:\.\d+)?|1(?:\.0+)?|\.\d+)\b")
KV_RE = re.compile(r"(\w+)=(\S*)")
STV_RE = re.compile(r"\(stv\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\)")

# Skill name -> (lane, store, op). Mirrors src/skills.metta getStaticSkills.
SKILL_LANES: dict[str, tuple[str, str | None, str | None]] = {
    "send": ("action", None, "send"),
    "remember": ("memory", "chroma", "add"),
    "query": ("memory", "chroma", "read"),
    "episodes": ("memory", "history", "read"),
    "pin": ("memory", "working", "pin"),
    "metta": ("reasoning", None, None),
    "shell": ("tool", None, None),
    "read-file": ("tool", None, None),
    "write-file": ("tool", None, None),
    "write-file-b64": ("tool", None, None),
    "append-file": ("tool", None, None),
    "delete-file": ("tool", None, None),
    "get-io-policy": ("tool", None, None),
    "websearch": ("tool", None, None),
    "search": ("tool", None, None),
    "version": ("tool", None, None),
}
OK_TOKENS = {"REMEMBER-SUCCESS", "PIN-SUCCESS", "APPEND-FILE-SUCCESS", "None", "True", "()"}

# Prompt section markers, in the order getContext (src/loop.metta) writes them.
PROMPT_MARKERS = [
    ("prompt", "PROMPT: "),
    ("skills", " SKILLS: "),
    ("output_format", " OUTPUT_FORMAT: "),
    ("save_dir", " SAVE_PERMANENT_FILES_DIR: "),
    ("last_results", " LAST_SKILL_USE_RESULTS: "),
    ("history", " HISTORY: "),
    ("time", " TIME: "),
]
PROMPT_DELIMITER = ":-:-:-:"


def decode_safe(text: str) -> str:
    """Undo Omega's string-safe encoding (src/utils.metta)."""
    return text.replace("_quote_", '"').replace("_newline_", "\n").replace("_apostrophe_", "'")


# --------------------------------------------------------------------------- s-expressions

class Atom(str):
    """A bare symbol or number."""


class Str(str):
    """A double-quoted string, unescaped."""


class PyLit(str):
    """A Python literal such as [['2026-08-13 11:12:02', 'text']], kept verbatim."""


_ESC = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _skip_bracket(s: str, i: int) -> int:
    depth, n, quote = 0, len(s), None
    while i < n:
        ch = s[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _read(s: str, i: int) -> tuple[Any, int]:
    n = len(s)
    while i < n and s[i].isspace():
        i += 1
    if i >= n:
        return None, n
    ch = s[i]
    if ch == "(":
        items: list[Any] = []
        i += 1
        while True:
            while i < n and s[i].isspace():
                i += 1
            if i >= n:
                return items, n
            if s[i] == ")":
                return items, i + 1
            node, i = _read(s, i)
            items.append(node)
    if ch == ")":
        return Atom(")"), i + 1
    if ch == '"':
        j, buf = i + 1, []
        while j < n:
            c = s[j]
            if c == "\\" and j + 1 < n:
                buf.append(_ESC.get(s[j + 1], s[j + 1]))
                j += 2
                continue
            if c == '"':
                return Str("".join(buf)), j + 1
            buf.append(c)
            j += 1
        return Str("".join(buf)), n
    if ch == "[":
        j = _skip_bracket(s, i)
        return PyLit(s[i:j]), j
    j = i
    while j < n and not s[j].isspace() and s[j] not in '()"':
        j += 1
    return Atom(s[i:j]), j


def read_sexpr(s: str) -> Any:
    node, _ = _read(s, 0)
    return node


def to_text(node: Any) -> str:
    if isinstance(node, list):
        return "(" + " ".join(to_text(x) for x in node) + ")"
    if isinstance(node, Str):
        return '"' + node.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return str(node)


def _stv_pairs(node: Any, out: list[dict]) -> None:
    """Collect (term (stv f c)) pairs anywhere in a node."""
    if not isinstance(node, list):
        return
    if (len(node) == 2 and isinstance(node[1], list) and len(node[1]) == 3
            and isinstance(node[1][0], Atom) and node[1][0] == "stv"):
        try:
            out.append({"term": to_text(node[0]), "f": float(node[1][1]), "c": float(node[1][2])})
            return
        except ValueError:
            pass
    for child in node:
        _stv_pairs(child, out)


# --------------------------------------------------------------------------- line level

def parse_line(line: str) -> dict | None:
    """One raw log line -> {t, t_source, level, logger, msg} or None for noise."""
    m = LINE_RE.match(line.rstrip("\n"))
    if not m:
        return None
    if m.group("dts"):
        t = _iso(m.group("dts"))
        src = "docker"
    else:
        t = m.group("ts").replace(" ", "T")
        src = "logging"
    return {"t": t, "t_source": src, "level": m.group("level"),
            "logger": m.group("name").strip(), "msg": m.group("msg")}


def _iso(dts: str) -> str:
    # Docker gives nanoseconds; Python's fromisoformat takes at most microseconds.
    m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$", dts)
    if not m:
        return dts
    frac = (m.group(2) or "")[:6]
    tz = m.group(3) or ""
    if tz == "Z":
        tz = "+00:00"
    return f"{m.group(1)}{'.' + frac if frac else ''}{tz}"


def to_epoch(t: str, assume_tz: timezone = timezone.utc) -> float | None:
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assume_tz)
    return dt.timestamp()


# --------------------------------------------------------------------------- message level

def parse_prompt(msg: str) -> dict:
    """(CHARS_SENT: N "<prompt>") -> section sizes, input text and last results."""
    body = msg[len("(CHARS_SENT: "):]
    if body.endswith(")"):
        body = body[:-1]
    num, _, rest = body.partition(" ")
    rest = rest.strip()
    if rest.startswith('"'):
        node, _ = _read(rest, 0)
        text = str(node)
    else:
        text = rest
    user = ""
    if PROMPT_DELIMITER in text:
        text, _, user = text.partition(PROMPT_DELIMITER)
    positions: dict[str, int] = {}
    cursor = 0
    for key, marker in PROMPT_MARKERS:
        if key in ("history", "time"):
            continue
        k = text.find(marker, cursor)
        if k >= 0:
            positions[key] = k
            cursor = k + len(marker)
    t_pos = text.rfind(" TIME: ")
    if t_pos >= 0:
        positions["time"] = t_pos
        h_pos = text.rfind(" HISTORY: ", 0, t_pos)
        if h_pos >= 0:
            positions["history"] = h_pos
    ordered = sorted(positions.items(), key=lambda kv: kv[1])
    marker_len = dict(PROMPT_MARKERS)
    sections: dict[str, int] = {}
    texts: dict[str, str] = {}
    for idx, (key, pos) in enumerate(ordered):
        start = pos + len(marker_len[key])
        end = ordered[idx + 1][1] if idx + 1 < len(ordered) else len(text)
        chunk = decode_safe(text[start:end])
        sections[key] = len(chunk)
        texts[key] = chunk
    if "skills" in texts:
        # prompt extensions sit between SKILLS and OUTPUT_FORMAT, after a newline
        sk = texts["skills"]
        nl = sk.find("\n")
        if nl >= 0:
            sections["skills"] = nl
            sections["extensions"] = len(sk) - nl
    user = decode_safe(user.strip())
    sections["input"] = len(user)
    try:
        chars = int(num)
    except ValueError:
        chars = len(text) + len(user)
    last = texts.get("last_results", "")
    return {
        "chars": chars,
        "sections": sections,
        "input": user,
        "last_results": last[:4000],
        "last_results_truncated": len(last) > 4000,
        "time": texts.get("time", "").strip(),
    }


def _metta_details(expr: str) -> dict:
    s = expr.strip()
    if s.startswith("(|-"):
        engine = "nal"
    elif s.startswith("(|~"):
        engine = "pln"
    elif "add-atom" in s or "remove-atom" in s:
        engine = "atomspace"
    else:
        engine = "metta"
    premises: list[dict] = []
    if engine in ("nal", "pln"):
        _stv_pairs(read_sexpr(s), premises)
    return {"engine": engine, "premises": premises}


def classify_call(node: Any) -> dict:
    """One parsed skill call node -> call record."""
    if not isinstance(node, list) or not node:
        return {"skill": "?", "args": [to_text(node)], "lane": "error", "error": "UNPARSEABLE_CALL"}
    head = str(node[0])
    args = [str(a) if isinstance(a, (Str, Atom, PyLit)) else to_text(a) for a in node[1:]]
    rec: dict[str, Any] = {"skill": head, "args": args, "text": to_text(node)}
    if head == "Error":
        rec["lane"] = "error"
        rec["error"] = args[0] if args else "Error"
        return rec
    lane, store, op = SKILL_LANES.get(head, ("tool", None, None))
    rec["lane"] = lane
    if store:
        rec["store"] = store
    if op:
        rec["op"] = op
    if head == "metta" and args:
        rec.update(_metta_details(args[0]))
        if rec["engine"] == "atomspace":
            rec["store"] = "atomspace"
            rec["op"] = "write"
    if head == "send" and args:
        m = TICK_CLAIM_RE.search(args[0])
        if m:
            rec["tick_claim"] = int(m.group(1))
        e = EP_CLAIM_RE.search(args[0])
        if e:
            rec["ep_claim"] = e.group(1)
        p = PRED_RE.search(args[0])
        if p:
            rec["prediction"] = float(p.group(1))
    return rec


def parse_calls(msg: str) -> list[dict]:
    inner = msg[len("(RESPONSE: "):]
    if inner.endswith(")"):
        inner = inner[:-1]
    node = read_sexpr(inner)
    if not isinstance(node, list):
        return [{"skill": "?", "args": [inner], "lane": "error", "error": "UNPARSEABLE_RESPONSE"}]
    return [classify_call(c) for c in node]


def _conclusions(nodes: list[Any]) -> list[dict]:
    """(term (stv f c)) pairs in a result, including inside the quoted string metta returns."""
    found: list[dict] = []
    for n in nodes:
        if isinstance(n, list):
            _stv_pairs(n, found)
            continue
        s = str(n).strip()
        if len(s) > 1 and s[0] == s[-1] == '"':
            s = s[1:-1].replace('\\"', '"')
        if s.startswith("("):
            _stv_pairs(read_sexpr(s), found)
    return found


def _result_value(nodes: list[Any]) -> dict:
    if not nodes:
        return {"kind": "empty", "ok": True}
    out: dict[str, Any]
    v = nodes[0] if len(nodes) == 1 else None
    if isinstance(v, Atom) and str(v) in OK_TOKENS:
        return {"kind": "ok", "ok": True, "text": str(v)}
    if isinstance(v, list) and v and isinstance(v[0], Atom) and str(v[0]) == "ALERT_FAILED":
        return {"kind": "error", "ok": False, "error": str(v[1]) if len(v) > 1 else "ALERT_FAILED",
                "text": to_text(v)}
    if isinstance(v, PyLit):
        try:
            lit = ast.literal_eval(v)
        except (ValueError, SyntaxError):
            lit = None
        if isinstance(lit, list) and all(isinstance(x, (list, tuple)) and len(x) == 2 for x in lit):
            return {"kind": "items", "ok": True, "items": [[str(a), str(b)] for a, b in lit]}
        out = {"kind": "value", "ok": True, "text": str(v)[:6000]}
    elif isinstance(v, Str):
        out = {"kind": "text", "ok": True, "text": str(v)[:6000], "truncated": len(v) > 6000}
    else:
        text = " ".join(to_text(n) for n in nodes)
        out = {"kind": "text", "ok": True, "text": text[:6000], "truncated": len(text) > 6000}
    conclusions = _conclusions(nodes)
    if conclusions:
        out["conclusions"] = conclusions
    return out


def parse_results(msg: str) -> tuple[list[tuple[str, dict]], dict | None]:
    """(RESPONSE: (RESULTS: ...)) -> ([(call_text, result)], loop_error)."""
    inner = msg[len("(RESPONSE: "):]
    if inner.endswith(")"):
        inner = inner[:-1]
    node = read_sexpr(inner)
    if not (isinstance(node, list) and node and str(node[0]) == "RESULTS:"):
        return [], {"type": "UNPARSEABLE_RESULTS", "text": inner[:500]}
    body = node[1] if len(node) > 1 else []
    if isinstance(body, list) and body and isinstance(body[0], Atom) and str(body[0]) == "ALERT_FAILED":
        return [], {"type": str(body[1]) if len(body) > 1 else "ALERT_FAILED", "text": to_text(body)[:2000]}
    pairs: list[tuple[str, dict]] = []
    for elem in body if isinstance(body, list) else []:
        if not (isinstance(elem, list) and len(elem) >= 2 and str(elem[0]) == "COMMAND_RETURN:"):
            continue
        payload = elem[1]
        if not isinstance(payload, list) or not payload:
            continue
        call_text = to_text(payload[0])
        pairs.append((call_text, _result_value(payload[1:])))
    return pairs, None


# --------------------------------------------------------------------------- iteration assembly

class TraceBuilder:
    """Feed raw lines in order; read .startup, .iterations and .notes."""

    def __init__(self) -> None:
        self.startup: dict[str, Any] = {"skills_added": [], "plugins": [], "providers": [], "channels": [],
                                        "notes": []}
        self.iterations: list[dict] = []
        self.notes: list[dict] = []
        self.lines_read = 0
        self.lines_parsed = 0
        self._cur: dict | None = None

    # public -------------------------------------------------------------
    def feed_many(self, lines: Iterable[str], start_line: int = 0) -> None:
        for offset, line in enumerate(lines):
            self.feed(line, start_line + offset)

    def feed(self, line: str, line_no: int | None = None) -> dict | None:
        """Returns the iteration that was closed by this line, if any."""
        self.lines_read += 1
        rec = parse_line(line)
        if rec is None:
            return None
        self.lines_parsed += 1
        rec["line"] = line_no
        msg = rec["msg"]
        closed = None
        m = ITER_RE.match(msg)
        if m:
            closed = self._close()
            self._cur = {"i": int(m.group(1)), "t0": rec["t"], "t1": rec["t"], "t_source": rec["t_source"],
                         "line0": line_no, "idle": True, "human_msg": None, "ep_ticks": [],
                         "prompt": None, "llm": [], "calls": [], "errors": [], "warnings": []}
            return closed
        cur = self._cur
        if cur is None:
            self._startup(rec)
            return None
        cur["t1"] = rec["t"]
        if msg.startswith("(HUMAN-MSG: "):
            text = decode_safe(msg[len("(HUMAN-MSG: "):-1] if msg.endswith(")") else msg[len("(HUMAN-MSG: "):])
            cur["human_msg"] = text
            cur["ep_ticks"] = [{"ep": a, "t": int(b)} for a, b in EP_TAG_RE.findall(text)]
        elif msg.startswith("(CHARS_SENT: "):
            cur["prompt"] = parse_prompt(msg)
            cur["prompt"]["line"] = line_no
            cur["idle"] = False
        elif msg.startswith("[LLM_USAGE]"):
            cur["llm"].append(self._usage(msg))
        elif msg.startswith("[LLM_RAW]"):
            raw = msg.split(" raw=", 1)[1] if " raw=" in msg else ""
            try:
                raw_text = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                raw_text = raw
            if cur["llm"] and "raw" not in cur["llm"][-1]:
                cur["llm"][-1]["raw"] = raw_text
            else:
                cur.setdefault("llm_raw_pending", []).append(raw_text)
        elif msg.startswith("(RESPONSE: (RESULTS:"):
            pairs, loop_err = parse_results(msg)
            if loop_err:
                cur["errors"].append(loop_err)
            self._attach(cur, pairs)
        elif msg.startswith("(RESPONSE: "):
            cur["calls"] = parse_calls(msg)
            for c in cur["calls"]:
                if c.get("lane") == "error":
                    cur["errors"].append({"type": c.get("error", "Error"), "text": c.get("text", "")[:500]})
        elif "LLM returned an empty response" in msg:
            cur["warnings"].append({"type": "LLM_EMPTY_RESPONSE", "text": msg})
        elif rec["level"] in ("ERROR", "CRITICAL"):
            cur["errors"].append({"type": "LOG_ERROR", "logger": rec["logger"], "text": msg[:1000]})
        elif rec["level"] == "WARNING":
            cur["warnings"].append({"type": "LOG_WARNING", "logger": rec["logger"], "text": msg[:500]})
        return None

    def finish(self) -> dict | None:
        return self._close()

    def summary(self) -> dict:
        its = self.iterations
        return {
            "parser": PARSER_VERSION,
            "lines_read": self.lines_read,
            "lines_parsed": self.lines_parsed,
            "iterations": len(its),
            "llm_calls": sum(len(i["llm"]) for i in its),
            "idle_iterations": sum(1 for i in its if i["idle"]),
        }

    # internals ------------------------------------------------------------
    def _close(self) -> dict | None:
        cur, self._cur = self._cur, None
        if cur is None:
            return None
        for pending in cur.pop("llm_raw_pending", []):
            if cur["llm"] and "raw" not in cur["llm"][-1]:
                cur["llm"][-1]["raw"] = pending
        if not cur["llm"] and cur["prompt"] is not None:
            # A prompt was sent but no [LLM_USAGE] line followed (older Omega or a provider
            # without usage logging). Count the call; token fields stay absent.
            cur["llm"].append({"usage_logged": False})
        self.iterations.append(cur)
        return cur

    @staticmethod
    def _usage(msg: str) -> dict:
        out: dict[str, Any] = {"usage_logged": True}
        for k, v in KV_RE.findall(msg):
            if v in ("None", ""):
                out[k] = None
                continue
            try:
                out[k] = int(v)
            except ValueError:
                out[k] = v
        # Normalize the Responses API names (providers/openai.py) to the chat names.
        if "input_tokens" in out and "prompt_tokens" not in out:
            out["prompt_tokens"] = out.get("input_tokens")
        if "output_tokens" in out and "completion_tokens" not in out:
            out["completion_tokens"] = out.get("output_tokens")
        if "incomplete_reason" in out and "finish_reason" not in out:
            out["finish_reason"] = out.get("incomplete_reason") or out.get("status")
        return out

    @staticmethod
    def _attach(cur: dict, pairs: list[tuple[str, dict]]) -> None:
        used: set[int] = set()
        for call_text, result in pairs:
            match = None
            for idx, c in enumerate(cur["calls"]):
                if idx not in used and c.get("text") == call_text:
                    match = idx
                    break
            if match is None:
                cur["calls"].append({"skill": call_text.strip("(").split(" ", 1)[0], "args": [],
                                     "text": call_text, "lane": "tool", "unmatched_result": True,
                                     "result": result})
                continue
            used.add(match)
            call = cur["calls"][match]
            if "result" in call:
                call.setdefault("more_results", []).append(result)
            else:
                call["result"] = result
            if result.get("kind") == "error":
                cur["errors"].append({"type": result.get("error", "ALERT_FAILED"), "skill": call.get("skill"),
                                      "text": result.get("text", "")[:500]})
            if call.get("skill") == "metta" and result.get("conclusions"):
                call["conclusions"] = result["conclusions"]

    def _startup(self, rec: dict) -> None:
        msg = rec["msg"]
        s = self.startup
        if msg.startswith("Add skill: ") or "Add skill: " in msg:
            s["skills_added"].append(msg.split("Add skill: ", 1)[1].strip('"'))
        elif "registerLLMProvider: registering LLM provider" in msg:
            s["providers"].append(msg.rsplit(" ", 1)[-1])
        elif "registerCommChannel: registering communication channel" in msg:
            s["channels"].append(msg.rsplit(" ", 1)[-1])
        elif re.search(r"Plugin \S+ is loaded", msg):
            s["plugins"].append(re.search(r"Plugin (\S+) is loaded", msg).group(1))
        elif "Embedding type selected is" in msg:
            s["embedding"] = msg.rsplit(" ", 1)[-1]
        elif msg.startswith("Knowledge") or "Knowledge:" in msg:
            s["knowledge"] = msg.strip('"')
        elif rec["level"] in ("ERROR", "CRITICAL", "WARNING"):
            s["notes"].append({"level": rec["level"], "logger": rec["logger"], "text": msg[:500]})


def parse_lines(lines: Iterable[str]) -> TraceBuilder:
    tb = TraceBuilder()
    tb.feed_many(lines)
    tb.finish()
    return tb
