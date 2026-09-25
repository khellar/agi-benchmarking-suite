"""ChatMemory: a chat history delivered over several messages, then one question about it.

Use it for long-term memory benchmarks. LongMemEval, for example:

    [environments.longmemeval]
    adapter = "omegaeval.envs.chatmemory:ChatMemory"
    options = { path = "/data/longmemeval_s_cleaned.json", name = "LongMemEval (S, cleaned)", judge_all = true }

Each record is one case. Native fields:

    id, sessions: [[{role, content}]], dates: [str], question, answer, question_date,
    type, evidence_sessions: [int]

LongMemEval's own field names are also read: question_id, question_type,
haystack_sessions, haystack_dates, haystack_session_ids, answer_session_ids, and
has_answer on turns. That mapping follows the format published in the LongMemEval
repository and has not yet been run against the dataset file. Only role and content of
each turn reach the agent.

Each session is sent as one message that expects no reply. The harness waits until the
agent's log shows it has read it, then sends the next. The question comes last and is
answered in free text. The agent passes only by keeping what it read: for OmegaClaw the
history outgrows the prompt window, so it has to remember and query.

Grading: the reply passes when it contains the gold answer after normalization. A reply
that does not contain it is held as needs_judge, and so is every abstention question,
because only a judge can grade "I don't think you told me" against the gold explanation.
With judge_all, every answer is held for a judge, as LongMemEval's own evaluation does,
and the containment check is kept as a pregrade. Which sessions hold the evidence is
privileged: it is revealed in score() after the episode.
"""
from __future__ import annotations

import random

from . import grading
from .base import Environment, Observation, Receipt
from .qa import data_fingerprint, jsonl_index, read_line_at, read_records, tool_policy

NAMES = ["Maya", "Ines", "Priya", "Leila", "Sofia", "Amara", "Noor", "Elif"]
PETS = ["Biscuit", "Pepper", "Juno", "Mochi", "Ziggy", "Olive", "Tango", "Nimbus"]
CITIES = ["Lisbon", "Porto", "Tallinn", "Kyoto", "Valencia", "Tbilisi", "Oaxaca", "Hobart"]
FILLER = [
    ("user", "Can you suggest a quick weeknight dinner?"),
    ("assistant", "A tray of roasted vegetables with chickpeas takes about thirty minutes."),
    ("user", "What is a good way to stretch after a run?"),
    ("assistant", "Hold a calf stretch and a hamstring stretch for about thirty seconds each."),
    ("user", "Any tips for keeping houseplants alive?"),
    ("assistant", "Water when the top inch of soil is dry and give them bright, indirect light."),
    ("user", "How do I make my coffee less bitter?"),
    ("assistant", "Use slightly cooler water and a coarser grind."),
]
FACTS = {
    "sister": ("My sister {v} just started a new job.", "What is my sister's name?", NAMES),
    "dog": ("We adopted a dog last week, we named him {v}.", "What is my dog's name?", PETS),
    "city": ("I finally moved, I live in {v} now.", "Which city do I live in now?", CITIES),
}
UPDATES = {
    "sister": "Small correction: my sister goes by {v} these days.",
    "dog": "We ended up renaming the dog {v}.",
    "city": "Plans changed again, I moved to {v}.",
}


def sample_cases(task: str, seed: int) -> dict:
    """A generated memory case for exercising the harness. Not a benchmark."""
    rng = random.Random(f"memory-sample:{task}:{seed}")
    key = rng.choice(sorted(FACTS))
    stmt, question, pool = FACTS[key]
    first, second = rng.sample(pool, 2)
    n = 6
    sessions = [[{"role": r, "content": c} for r, c in rng.sample(FILLER, 4)] for _ in range(n)]
    i = rng.randrange(0, n - 2)
    sessions[i].insert(rng.randrange(0, 3), {"role": "user", "content": stmt.format(v=first)})
    evidence = [i]
    answer = first
    if task == "update":
        j = rng.randrange(i + 1, n)
        sessions[j].insert(rng.randrange(0, 3), {"role": "user", "content": UPDATES[key].format(v=second)})
        evidence.append(j)
        answer = second
    dates = [f"2026-0{1 + k // 2}-{10 + k:02d}" for k in range(n)]
    return {"id": f"{task}-{seed}", "type": "knowledge-update" if task == "update" else "single-session",
            "sessions": sessions, "dates": dates, "question": question, "answer": answer,
            "question_date": "2026-05-01", "evidence_sessions": evidence}


def _normalize_case(r: dict, i: int) -> dict:
    sessions = r.get("sessions") or r.get("haystack_sessions") or []
    dates = r.get("dates") or r.get("haystack_dates") or []
    evidence = r.get("evidence_sessions")
    if evidence is None and r.get("answer_session_ids") and r.get("haystack_session_ids"):
        ids = list(r["haystack_session_ids"])
        evidence = [ids.index(s) for s in r["answer_session_ids"] if s in ids]
    if evidence is None:
        evidence = [k for k, s in enumerate(sessions) if any(t.get("has_answer") for t in s)]
    cid = str(r.get("id") or r.get("question_id") or f"case{i + 1}")
    ctype = str(r.get("type") or r.get("question_type") or "all")
    if cid.endswith("_abs"):
        ctype = "abstention"
    return {"id": cid, "type": ctype, "sessions": sessions, "dates": [str(d) for d in dates],
            "question": str(r.get("question", "")), "answer": str(r.get("answer", "")),
            "question_date": str(r.get("question_date", "")), "evidence_sessions": list(evidence)}


def _case_fields(r: dict) -> dict:
    cid = str(r.get("id") or r.get("question_id") or "")
    ctype = str(r.get("type") or r.get("question_type") or "all")
    return {"id": cid, "type": "abstention" if cid.endswith("_abs") else ctype}


class ChatMemory(Environment):
    id = "chatmemory"
    version = "0.2.0"
    RECORDS: list[dict] | None = None     # subclasses may inline their cases here

    def __init__(self, path: str | None = None, name: str | None = None, sample: bool = False,
                 forbid_tools: list[str] | None = None, allow_tools: list[str] | None = None,
                 void_on_violation: bool = True, judge_on_mismatch: bool = True, judge_all: bool = False,
                 **options):
        super().__init__(path=path, name=name, sample=sample, forbid_tools=forbid_tools, allow_tools=allow_tools,
                         judge_all=judge_all, **options)
        self.path, self.sample, self.name = path, sample, name
        self.forbid_tools = list(forbid_tools or [])
        self.allow_tools = list(allow_tools) if allow_tools else None
        self.void_on_violation = void_on_violation
        self.judge_on_mismatch = judge_on_mismatch
        self.judge_all = judge_all
        self._cases: list[dict] | None = None

    def cases(self) -> list[dict]:
        if self._cases is None:
            if self.RECORDS is not None:
                raw = [dict(r) for r in self.RECORDS]
            elif self.sample:
                raw = []
            elif self.path and str(self.path).endswith(".jsonl"):
                # JSON Lines: list the cases from a line index and read one case per episode,
                # so a multi-gigabyte file never has to fit in memory
                self._cases = [{"id": e["id"] or f"case{i + 1}", "type": e["type"], "lazy": (i, e["offset"], e["length"])}
                               for i, e in enumerate(jsonl_index(self.path, _case_fields))]
                return self._cases
            elif self.path:
                raw = read_records(self.path)
            else:
                raise ValueError(f"{self.id}: set options.path to a JSON or JSONL file of cases")
            self._cases = [_normalize_case(r, i) for i, r in enumerate(raw)]
        return self._cases

    # -- public ---------------------------------------------------------------------
    def describe(self) -> dict:
        types = ["recall", "update"] if self.sample else sorted({c["type"] for c in self.cases()})
        return {
            "id": self.id, "version": self.version,
            "name": self.name or ("Sample chat-memory cases (for testing the harness)" if self.sample else self.id),
            "kind": "memory", "tasks": types if self.sample else ["all"] + (types if 1 < len(types) <= 30 else []),
            "observation": "text: one earlier conversation per message, then a question about them",
            "actions": "free text: the answer",
            "utility": {"meaning": ("1 if a judge finds the answer correct, 0 if not" if self.judge_all else
                                    "1 if the reply contains the gold answer; held for a judge when it does not"),
                        "direction": "higher"},
            "brief": ("You are going to be shown earlier conversations with this user, one message at a time. "
                      "Nothing needs a reply until a question arrives. Keep what matters."),
            "renderer": "timeline", "oracle": True, "floor_policy": "no answer",
            "scoring": "judge" if self.judge_all else ("mixed" if self.judge_on_mismatch else "deterministic"),
            "tool_policy": tool_policy(self.forbid_tools, self.allow_tools, self.void_on_violation),
            "cases": None if self.sample else len(self.cases()),
            "source": self.path or ("generated sample" if self.sample else "inline records"),
        }

    def reset(self, task: str, seed: int) -> Observation:
        if self.sample:
            if task not in ("recall", "update"):
                raise ValueError("memory-sample tasks: recall, update")
            self.case = sample_cases(task, int(seed))
        else:
            pool = self.cases() if task in ("all", "", None) else [c for c in self.cases() if c["type"] == task]
            if not pool:
                raise ValueError(f"{self.id}: no cases in task {task!r}")
            self.case = pool[(int(seed) - 1) % len(pool)]
            if "lazy" in self.case:
                i, off, length = self.case["lazy"]
                self.case = _normalize_case(read_line_at(self.path, off, length), i)
        self.task, self.seed = task, seed
        self.pos = 0
        self.answer: str | None = None
        self.steps = 0
        self.invalid = 0
        return self._obs()

    def delivered(self) -> Receipt:
        self.pos += 1
        return Receipt(self._obs(), True, "delivered", False, {"delivered": self.pos})

    def parse_action(self, text: str) -> str | None:
        t = " ".join(str(text).split()).strip()
        return t or None

    def step(self, action: str | None) -> Receipt:
        self.steps += 1
        if not action:
            self.invalid += 1
            return Receipt(self._obs("Your reply had no answer. "), False, "no answer in the reply",
                           self.steps >= self.max_steps(), {"steps": self.steps})
        self.answer = action
        return Receipt(self._obs(), True, "answer recorded", True, {"steps": self.steps})

    def render(self) -> dict:
        c = self.case
        n = len(c["sessions"])
        items = [{"label": f"Conversation {k + 1}", "date": c["dates"][k] if k < len(c["dates"]) else "",
                  "kind": "info", "delivered": k < self.pos, "turns": len(c["sessions"][k])} for k in range(n)]
        items.append({"label": "Question", "date": c["question_date"], "kind": "question",
                      "delivered": self.pos >= n, "answer": self.answer})
        return {"type": "timeline", "items": items, "current": min(self.pos, n), "question": c["question"]}

    # -- privileged ---------------------------------------------------------------------
    def score(self) -> dict:
        c, ans = self.case, self.answer
        metrics = {"gold": c["answer"], "answer": ans, "case_id": c["id"], "case_type": c["type"],
                   "question": c["question"], "question_date": c["question_date"],
                   "evidence_sessions": c["evidence_sessions"], "sessions": len(c["sessions"]),
                   "messages_delivered": self.pos, "invalid_actions": self.invalid}
        if ans is None:
            return {"success": False, "utility": 0.0, "terminal": "no_answer" if self.steps else "running",
                    "steps": self.steps, "metrics": metrics}
        held = {"success": None, "utility": None, "terminal": "needs_judge", "needs_judge": True,
                "steps": self.steps, "metrics": metrics}
        if c["type"] == "abstention":
            metrics["grading"] = "abstention question; needs a judge"
            return held
        found = grading.contains_match(ans, c["answer"])
        if self.judge_all:
            metrics.update(pregrade="match" if found else "mismatch",
                           grading="every answer goes to a judge on this benchmark")
            return held
        if found:
            metrics["grading"] = "reply contains the gold answer"
            return {"success": True, "utility": 1.0, "terminal": "answered", "steps": self.steps, "metrics": metrics}
        if self.judge_on_mismatch:
            metrics["grading"] = "gold answer not found in the reply; needs a judge"
            return held
        metrics["grading"] = "gold answer not found in the reply"
        return {"success": False, "utility": 0.0, "terminal": "answered", "steps": self.steps, "metrics": metrics}

    def fingerprint(self) -> dict:
        return data_fingerprint(self.path, self.RECORDS, self.sample)

    def floor(self) -> float:
        return 0.0

    def ceiling(self) -> dict:
        return {"utility": 1.0, "steps": 1}

    def max_steps(self) -> int:
        return 3

    def solution(self) -> list[str]:
        return [self.case["answer"]]

    # -- internals -----------------------------------------------------------------------
    def _obs(self, prefix: str = "") -> Observation:
        c = self.case
        n = len(c["sessions"])
        if self.pos < n:
            k = self.pos
            date = c["dates"][k] if k < len(c["dates"]) else ""
            lines = [f"Conversation {k + 1} of {n}{', ' + date if date else ''}:"]
            lines += [f"{t.get('role', 'user')}: {t.get('content', '')}" for t in c["sessions"][k]]
            return Observation("\n".join(lines), [], {"conversation": k + 1, "of": n}, expects_action=False)
        q = f"{prefix}Question{' (asked ' + c['question_date'] + ')' if c['question_date'] else ''}: {c['question']}"
        q += "\nAnswer from what you remember of the earlier conversations."
        return Observation(q, [], {"question": True}, expects_action=True, free_text=True)
