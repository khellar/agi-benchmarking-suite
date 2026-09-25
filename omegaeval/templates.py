"""Templates written by `omegaeval new-benchmark`. Each one passes `omegaeval conform` as written."""
from __future__ import annotations

INTERACTIVE = '''"""__NAME__: TODO one line on what this benchmark measures.

An interactive benchmark: every observation offers actions and expects one back.
Replace the counting game below with your environment. For a game that runs in its own
server, such as FreeCiv or ARC-AGI-3, reset() starts or joins a game, step() sends the
action and reads the new state, and render() returns a "tiles" or "pixels" frame.
"""
from omegaeval.envs.base import Environment, Observation, Receipt


class __CLS__(Environment):
    id = "__NAME__"
    version = "0.1.0"

    def describe(self):
        return {
            "id": self.id, "version": self.version, "name": "TODO human-readable name",
            "kind": "interactive", "tasks": ["easy", "hard"], "default_task": "easy",
            "observation": "text: the current total and the target",
            "actions": "add 1, add 2",
            "utility": {"meaning": "1 if the total reaches the target exactly, else 0", "direction": "higher"},
            "brief": "Reach the target total exactly by adding 1 or 2 each step.",
            "renderer": None,            # or "grid", "tiles", "pixels", "card", "timeline"
            "oracle": True, "floor_policy": "noop (never reaches the target)",
            "scoring": "deterministic",
            "tool_policy": {"forbid": []},   # skills the agent must not use, e.g. ["websearch"]
        }

    # -- public: what the agent sees ---------------------------------------------
    def reset(self, task, seed):
        self.task, self.seed = task, seed
        self.target = (5 + seed % 5) if task == "easy" else (12 + seed % 9)
        self.total, self.steps, self.invalid, self.overshot = 0, 0, 0, False
        return self._obs("episode start")

    def parse_action(self, text):
        t = " ".join(text.lower().split())
        return {"add 1": "add 1", "1": "add 1", "add 2": "add 2", "2": "add 2"}.get(t)

    def step(self, action):
        self.steps += 1
        if action not in ("add 1", "add 2"):
            self.invalid += 1
            return Receipt(self._obs("not an available action"), False, "not an available action",
                           self.steps >= self.max_steps(), {"total": self.total})
        self.total += int(action[-1])
        self.overshot = self.total > self.target
        done = self.total >= self.target or self.steps >= self.max_steps()
        effect = "overshot the target" if self.overshot else f"added {action[-1]}"
        return Receipt(self._obs(effect), True, effect, done, {"total": self.total})

    def render(self):
        return None                      # the console shows the transcript of messages instead

    # -- privileged: the harness only ------------------------------------------------
    def score(self):
        ok = self.total == self.target
        if ok:
            terminal = "solved"
        elif self.overshot:
            terminal = "failed"
        elif self.steps >= self.max_steps():
            terminal = "budget_exhausted"
        else:
            terminal = "running"
        return {"success": ok, "utility": 1.0 if ok else 0.0, "terminal": terminal, "steps": self.steps,
                "metrics": {"invalid_actions": self.invalid, "total": self.total, "target": self.target}}

    def floor(self):
        return 0.0

    def ceiling(self):
        return {"utility": 1.0, "steps": len(self.solution())}

    def max_steps(self):
        return self.target + 5

    def solution(self):
        twos, ones = divmod(self.target, 2)
        return ["add 2"] * twos + ["add 1"] * ones

    def _obs(self, effect):
        return Observation(f"Last action: {effect}. Total: {self.total}. Target: {self.target}.",
                           ["add 1", "add 2"], {"total": self.total, "target": self.target})
'''

QA = '''"""__NAME__: TODO one line on what this benchmark measures.

A question set: one question per episode, answered in free text. Seed 1 is the first
question, seed 2 the second. To load a file instead of the inline records, delete
RECORDS and configure the benchmark in omegaeval.toml:

    [environments.__NAME__]
    adapter = "omegaeval.envs.qa:QADataset"
    options = { path = "/data/questions.jsonl", forbid_tools = ["websearch", "shell"] }
"""
from omegaeval.envs.qa import QADataset


class __CLS__(QADataset):
    id = "__NAME__"
    RECORDS = [
        {"id": "q1", "question": "TODO: What is the capital of France?", "answer": "Paris",
         "category": "geography"},
        {"id": "q2", "question": "TODO: Which of these numbers is prime?", "choices": ["21", "23", "25", "27"],
         "answer": "B", "category": "math"},
    ]

    def __init__(self, **options):
        options.setdefault("name", "TODO human-readable name")
        options.setdefault("forbid_tools", ["websearch", "shell"])   # or allow_tools = [the only skills allowed]
        super().__init__(**options)
'''

MEMORY = '''"""__NAME__: TODO one line on what this benchmark measures.

A memory benchmark: earlier conversations arrive one message at a time and need no
reply, then a question about them. To load a file instead of the inline records, delete
RECORDS and configure the benchmark in omegaeval.toml:

    [environments.__NAME__]
    adapter = "omegaeval.envs.chatmemory:ChatMemory"
    options = { path = "/data/cases.json" }
"""
from omegaeval.envs.chatmemory import ChatMemory


class __CLS__(ChatMemory):
    id = "__NAME__"
    RECORDS = [
        {"id": "c1", "type": "recall",
         "sessions": [[{"role": "user", "content": "TODO: I just adopted a cat, her name is Io."},
                       {"role": "assistant", "content": "Congratulations on the new cat!"}],
                      [{"role": "user", "content": "Any tips for sleeping better?"},
                       {"role": "assistant", "content": "Keep a regular schedule and a dark room."}]],
         "dates": ["2026-03-01", "2026-03-08"],
         "question": "What is my cat's name?", "answer": "Io", "evidence_sessions": [0]},
        {"id": "c2", "type": "recall",
         "sessions": [[{"role": "user", "content": "What should I cook tonight?"},
                       {"role": "assistant", "content": "A quick vegetable curry."}],
                      [{"role": "user", "content": "TODO: I start my new job at the harbour office on Monday."},
                       {"role": "assistant", "content": "Good luck with the new job!"}]],
         "dates": ["2026-04-02", "2026-04-09"],
         "question": "Where is my new job?", "answer": "harbour office", "evidence_sessions": [1]},
    ]

    def __init__(self, **options):
        options.setdefault("name", "TODO human-readable name")
        super().__init__(**options)
'''

TEMPLATES = {"interactive": INTERACTIVE, "qa": QA, "memory": MEMORY}


def class_name(name: str) -> str:
    parts = [p for p in name.replace("_", "-").split("-") if p]
    cls = "".join(p[:1].upper() + p[1:] for p in parts) or "Benchmark"
    return cls if cls[0].isalpha() else f"B{cls}"


def render(name: str, kind: str) -> str:
    return TEMPLATES[kind].replace("__NAME__", name).replace("__CLS__", class_name(name))
