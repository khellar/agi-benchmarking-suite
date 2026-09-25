"""QADataset: a set of questions, one per episode, answered in free text.

Use it for exam-style benchmarks. Humanity's Last Exam, for example:

    [environments.hle]
    adapter = "omegaeval.envs.qa:QADataset"

    [environments.hle.options]
    path = "/data/hle.jsonl"          # the test split as JSON Lines
    name = "Humanity's Last Exam (text-only)"
    judge_all = true                  # HLE is graded by a judge model
    allow_tools = ["send", "remember", "query", "pin", "episodes", "metta"]

Records are JSON or JSON Lines. Field names accepted, first match wins:

    id          id, question_id, uid
    question    question, prompt, problem
    choices     choices, options            (a list; optional)
    answer      answer, gold, target, solution
    type        answer_type, type           ("multipleChoice" or "exactMatch"; inferred when absent)
    category    category, subject, raw_subject
    image       image, image_url            (rows with an image are skipped: a chat channel carries text)

A multiple-choice question without a choices field may carry its options in the text, as
Humanity's Last Exam does: an "Answer Choices:" line followed by "A. ...", "B. ...". Those
options are read from the text, and the question is sent as written.

Grading is deterministic where it can be. Multiple choice compares the option letter.
Short answers compare after normalization, including numbers such as 3/4 and 0.75. A
short answer that does not match is not marked wrong: it is held as needs_judge,
because only a judge can say whether "Paris, France" answers "Paris". With judge_all,
every answer is held for a judge, as benchmarks graded by a judge model require, and the
deterministic grade is kept as a pregrade.

Tool rules: forbid_tools names skills the agent must not use. allow_tools names the only
skills it may use, which also catches skills added by plugins; send is always allowed.

The seed picks the question: seed 1 is the first question of the task, seed 2 the second.
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Callable

from . import grading
from .base import Environment, Observation, Receipt

PRIMES = [53, 59, 61, 67, 71, 73, 79, 83, 89, 97]
COMPOSITES = [51, 57, 63, 69, 77, 81, 87, 91, 93, 95]
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_OPTION = re.compile(r"^\s*\(?([A-Z])[.)]\s+(\S.*?)\s*$")
_HEADER = re.compile(r"^\s*(?:answer\s+choices|answer\s+options|choices|options)\s*:?\s*$", re.I)


def sample_questions(n: int = 200) -> list[dict]:
    """Generated arithmetic questions for exercising the harness. Not a benchmark."""
    out = []
    for i in range(n):
        rng = random.Random(f"qa-sample:{i}")
        if i % 2 == 0:
            a, b = rng.randint(12, 99), rng.randint(12, 99)
            out.append({"id": f"arith-{i}", "question": f"What is {a} × {b}?", "answer": str(a * b),
                        "answer_type": "exactMatch", "category": "arithmetic"})
        else:
            prime = rng.choice(PRIMES)
            choices = [prime] + rng.sample(COMPOSITES, 3)
            rng.shuffle(choices)
            gold = "ABCD"[choices.index(prime)]
            out.append({"id": f"prime-{i}", "question": "Which of these numbers is prime?",
                        "choices": [str(c) for c in choices], "answer": gold,
                        "answer_type": "multipleChoice", "category": "primes"})
    return out


def embedded_choices(question: str) -> list[str] | None:
    """Options written into the question text, or None.

    After an "Answer Choices:" line, every "A. ..." line starts an option and other lines
    continue it. Without that line, only a run of option lines at the very end counts.
    The letters must run A, B, C... and there must be at least two.
    """
    lines = question.splitlines()
    head = next((i for i, line in enumerate(lines) if _HEADER.match(line)), None)
    found: list[str] = []
    if head is not None:
        for line in lines[head + 1:]:
            m = _OPTION.match(line)
            if m and len(found) < 26 and m.group(1) == LETTERS[len(found)]:
                found.append(m.group(2))
            elif line.strip() and found:
                found[-1] += " " + line.strip()
            elif line.strip():
                return None                      # text between the header and option A
    else:
        tail = [line for line in lines if line.strip()]
        run: list[str] = []
        for line in reversed(tail):
            m = _OPTION.match(line)
            if not m:
                break
            run.insert(0, line)
        for line in run:
            m = _OPTION.match(line)
            if len(found) < 26 and m.group(1) == LETTERS[len(found)]:
                found.append(m.group(2))
            else:
                return None
    return found if len(found) >= 2 else None


def _first(rec: dict, *names, default=None):
    for n in names:
        if n in rec and rec[n] not in (None, ""):
            return rec[n]
    return default


_RECORDS: dict[tuple, list] = {}


def read_records(path: str | Path) -> list[dict]:
    """Records of a JSON or JSON Lines file, parsed once per version of the file and shared.
    Callers must not modify them."""
    p = Path(path)
    st = p.stat()
    key = (str(p.resolve()), st.st_mtime_ns, st.st_size)
    if key in _RECORDS:
        return _RECORDS[key]
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".jsonl" or (text.lstrip().startswith("{") and "\n{" in text):
        data = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("data") or data.get("questions") or data.get("records") or []
        data = list(data)
    for old in [k for k in _RECORDS if k[0] == key[0]] + list(_RECORDS)[:-3]:
        _RECORDS.pop(old, None)                        # keep the latest version of at most four files
    _RECORDS[key] = data
    return data


_LINES: dict[tuple, list] = {}


def jsonl_index(path: str | Path, fields: Callable[[dict], dict]) -> list[dict]:
    """One small entry per line of a JSON Lines file: its byte offset and length plus whatever
    fields() picks from the record. Lets an adapter list a very large file and read one record
    at a time. Built once per version of the file."""
    p = Path(path)
    st = p.stat()
    key = (str(p.resolve()), st.st_mtime_ns, st.st_size, getattr(fields, "__qualname__", ""))
    if key in _LINES:
        return _LINES[key]
    out, offset = [], 0
    with open(p, "rb") as fh:
        for line in fh:
            if line.strip():
                out.append({"offset": offset, "length": len(line), **fields(json.loads(line))})
            offset += len(line)
    for old in [k for k in _LINES if k[0] == key[0]]:
        _LINES.pop(old, None)
    _LINES[key] = out
    return out


def read_line_at(path: str | Path, offset: int, length: int) -> dict:
    with open(path, "rb") as fh:
        fh.seek(offset)
        return json.loads(fh.read(length))


_FINGERPRINTS: dict[tuple, dict] = {}


def data_fingerprint(path, records, sample) -> dict:
    """sha256 of the data an adapter reads, cached per file size and modification time."""
    import hashlib

    if records is not None:
        blob = json.dumps(records, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return {"records": len(records), "sha256": hashlib.sha256(blob).hexdigest()}
    if sample or not path:
        return {"generated": True}
    p = Path(path)
    st = p.stat()
    key = (str(p.resolve()), st.st_size, st.st_mtime_ns)
    if key not in _FINGERPRINTS:
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        _FINGERPRINTS[key] = {"file": p.name, "bytes": st.st_size, "sha256": h.hexdigest()}
    return dict(_FINGERPRINTS[key])


def tool_policy(forbid, allow, void_on_violation) -> dict:
    return {"forbid": sorted(set(forbid or [])), "allow": sorted(set(allow) | {"send"}) if allow else None,
            "void_on_violation": bool(void_on_violation)}


class QADataset(Environment):
    id = "qa"
    version = "0.2.0"
    RECORDS: list[dict] | None = None     # subclasses may inline their questions here

    def __init__(self, path: str | None = None, name: str | None = None, sample: bool = False,
                 forbid_tools: list[str] | None = None, allow_tools: list[str] | None = None,
                 void_on_violation: bool = True, judge_on_mismatch: bool = True, judge_all: bool = False,
                 skip_images: bool = True, **options):
        super().__init__(path=path, name=name, sample=sample, forbid_tools=forbid_tools, allow_tools=allow_tools,
                         judge_all=judge_all, **options)
        self.path, self.sample = path, sample
        self.name = name
        self.forbid_tools = list(forbid_tools or [])
        self.allow_tools = list(allow_tools) if allow_tools else None
        self.void_on_violation = void_on_violation
        self.judge_on_mismatch = judge_on_mismatch
        self.judge_all = judge_all
        self.skip_images = skip_images
        self._records: list[dict] | None = None
        self.skipped_images = 0

    # -- data ------------------------------------------------------------------
    def records(self) -> list[dict]:
        if self._records is not None:
            return self._records
        if self.RECORDS is not None:
            raw = [dict(r) for r in self.RECORDS]
        elif self.sample:
            raw = sample_questions()
        elif self.path:
            raw = read_records(self.path)
        else:
            raise ValueError(f"{self.id}: set options.path to a JSON or JSONL file of questions")
        out = []
        for i, r in enumerate(raw):
            if self.skip_images and _first(r, "image", "image_url"):
                self.skipped_images += 1
                continue
            question = str(_first(r, "question", "prompt", "problem", default=""))
            choices = _first(r, "choices", "options", default=None)
            if isinstance(choices, dict):
                choices = [choices[k] for k in sorted(choices)]
            kind = _first(r, "answer_type", "type", default=None) or ("multipleChoice" if choices else "exactMatch")
            embedded = False
            if not choices and kind == "multipleChoice":
                choices = embedded_choices(question)
                embedded = bool(choices)
            out.append({"id": str(_first(r, "id", "question_id", "uid", default=f"q{i + 1}")),
                        "question": question,
                        "choices": [str(c) for c in choices] if choices else None,
                        "embedded": embedded,
                        "answer": str(_first(r, "answer", "gold", "target", "solution", default="")),
                        "type": kind,
                        "category": str(_first(r, "category", "subject", "raw_subject", default="all"))})
        if not out:
            raise ValueError(f"{self.id}: no usable questions")
        self._records = out
        return out

    def _pool(self, task: str) -> list[dict]:
        recs = self.records()
        pool = recs if task in ("all", "", None) else [r for r in recs if r["category"] == task]
        if not pool:
            raise ValueError(f"{self.id}: no questions in task {task!r}")
        return pool

    # -- public ---------------------------------------------------------------------
    def describe(self) -> dict:
        recs = self.records()
        cats = sorted({r["category"] for r in recs})
        return {
            "id": self.id, "version": self.version,
            "name": self.name or ("Sample arithmetic questions (for testing the harness)" if self.sample else self.id),
            "kind": "qa", "tasks": ["all"] + (cats if 1 < len(cats) <= 60 else []),
            "observation": "text: one question, with lettered choices when it is multiple choice",
            "actions": "free text: the answer, or the option letter",
            "utility": {"meaning": ("1 if a judge finds the answer correct, 0 if not" if self.judge_all else
                                    "1 if the answer matches the gold answer, 0 if it does not; "
                                    "held for a judge when a short answer does not match exactly"),
                        "direction": "higher"},
            "brief": "Answer the question. Reply with the answer only.",
            "renderer": "card", "oracle": True,
            "floor_policy": "a random option for multiple choice, no answer otherwise",
            "scoring": "judge" if self.judge_all else ("mixed" if self.judge_on_mismatch else "deterministic"),
            "tool_policy": tool_policy(self.forbid_tools, self.allow_tools, self.void_on_violation),
            "questions": len(recs), "skipped_images": self.skipped_images,
            "multiple_choice": sum(1 for r in recs if r["type"] == "multipleChoice"),
            "choices_read_from_text": sum(1 for r in recs if r["embedded"]),
            "source": self.path or ("generated sample" if self.sample else "inline records"),
        }

    def reset(self, task: str, seed: int) -> Observation:
        pool = self._pool(task)
        self.task, self.seed = task, seed
        self.rec = pool[(int(seed) - 1) % len(pool)]
        self.answer: str | None = None
        self.steps = 0
        self.invalid = 0
        return self._obs("")

    def parse_action(self, text: str) -> str | None:
        t = " ".join(str(text).split()).strip()
        for prefix in ("answer:", "final answer:", "the answer is"):
            if t.lower().startswith(prefix):
                t = t[len(prefix):].strip()
        return t or None

    def step(self, action: str | None) -> Receipt:
        self.steps += 1
        if not action:
            self.invalid += 1
            done = self.steps >= self.max_steps()
            return Receipt(self._obs("Your reply had no answer. "), False, "no answer in the reply", done,
                           {"steps": self.steps})
        self.answer = action
        return Receipt(self._obs(""), True, "answer recorded", True, {"steps": self.steps})

    def render(self) -> dict:
        return {"type": "card", "title": self.rec["category"], "question": self.rec["question"],
                "choices": self.rec["choices"], "answer": self.answer}

    # -- privileged -------------------------------------------------------------------
    def _grade(self, ans: str) -> tuple[bool | None, dict]:
        """True, False, or None when only a judge can tell; plus how it was decided."""
        rec = self.rec
        gold = rec["answer"].strip()
        if rec["choices"]:
            picked = grading.letter_choice(ans, rec["choices"])
            if len(gold) != 1:
                idx = [i for i, c in enumerate(rec["choices"]) if grading.exact_match(gold, c)]
                gold = LETTERS[idx[0]] if idx else gold
            return (picked is not None and picked == gold.upper()), {"grading": "option letter", "picked": picked}
        if rec["type"] == "multipleChoice" and len(gold) == 1 and gold.isalpha():
            picked = grading.letter_choice(ans, [""] * 26, strict=True)
            if picked is not None:
                return picked == gold.upper(), {"grading": "option letter", "picked": picked}
            return None, {"grading": "no option letter found and the options are unknown; needs a judge"}
        if grading.exact_match(ans, gold):
            return True, {"grading": "exact match"}
        return None, {"grading": "no exact match; needs a judge"}

    def score(self) -> dict:
        rec, ans = self.rec, self.answer
        metrics = {"gold": rec["answer"], "answer": ans, "question_id": rec["id"], "answer_type": rec["type"],
                   "question": rec["question"], "invalid_actions": self.invalid}
        if ans is None:
            return {"success": False, "utility": 0.0, "terminal": "no_answer" if self.steps else "running",
                    "steps": self.steps, "metrics": metrics}
        ok, how = self._grade(ans)
        metrics.update(how)
        if self.judge_all:
            metrics.update(pregrade={True: "match", False: "mismatch", None: "undecided"}[ok],
                           grading="every answer goes to a judge on this benchmark")
            ok = None
        elif ok is None and not self.judge_on_mismatch:
            metrics["grading"] = "exact match failed"
            ok = False
        if ok is None:
            return {"success": None, "utility": None, "terminal": "needs_judge", "needs_judge": True,
                    "steps": self.steps, "metrics": metrics}
        return {"success": ok, "utility": 1.0 if ok else 0.0, "terminal": "answered", "steps": self.steps,
                "metrics": metrics}

    def fingerprint(self) -> dict:
        return data_fingerprint(self.path, self.RECORDS, self.sample)

    def floor(self) -> float:
        return 1.0 / len(self.rec["choices"]) if self.rec["choices"] else 0.0

    def ceiling(self) -> dict:
        return {"utility": 1.0, "steps": 1}

    def max_steps(self) -> int:
        return 3

    def solution(self) -> list[str]:
        return [self.rec["answer"]]

    # -- internals ------------------------------------------------------------------------
    def _obs(self, prefix: str) -> Observation:
        rec = self.rec
        text = prefix + rec["question"]
        if rec["choices"] and not rec["embedded"] and "A." not in rec["question"]:
            text += "\nChoices:\n" + "\n".join(f"{LETTERS[i]}. {c}" for i, c in enumerate(rec["choices"]))
        if rec["choices"] or rec["type"] == "multipleChoice":
            text += "\nReply with the letter of your answer."
        return Observation(text, [], {"question_id": rec["id"], "category": rec["category"],
                                      "multiple_choice": rec["type"] == "multipleChoice" or bool(rec["choices"])},
                           True, True)
