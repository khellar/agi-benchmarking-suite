"""The environment adapter contract.

An environment is anything that implements this class. Its public methods produce
what the agent is shown, as text. Its privileged methods are called only by the
harness, outside the agent's view.

Agents play through their chat channel, so an adapter never talks to an agent. It
returns text, the gateway delivers it, and the agent's reply comes back as text.

Three shapes cover most benchmarks:

    interactive   a game: every observation offers actions and expects one back
    qa            a question set: one observation per episode, answered in free text
    memory        information delivered over several messages that expect no reply,
                  then a question about it

Frames returned by render() are drawn by the console. Known types:

    grid       {"type":"grid", "rows":[str], "agent":[r,c], "legend":{char: label}}
    pixels     {"type":"pixels", "rows":[[int]] or [str of hex digits], "palette":[css color]}
    tiles      {"type":"tiles", "rows":[str], "legend":{char: label}, "colors":{char: css},
                "marks":[{"r":int, "c":int, "label":str}]}
    card       {"type":"card", "title":str, "question":str, "choices":[str], "answer":str|None}
    timeline   {"type":"timeline", "items":[{"label":str, "date":str, "kind":"info"|"question",
                "delivered":bool}], "current":int}

A frame shows only what the agent has been shown. Anything privileged, such as a gold
answer or which messages hold the evidence, goes in score()["metrics"], which the
console reveals only after the episode ends. With no renderer, the console shows the
transcript of messages instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Observation:
    text: str                                   # what the agent reads
    actions: list[str]                          # canonical actions offered now; empty for free-text answers
    public: dict[str, Any] = field(default_factory=dict)   # structured public state, for the record
    expects_action: bool = True                 # False: information to read, no reply expected
    free_text: bool = False                     # True: any non-empty reply is an answer


@dataclass
class Receipt:
    observation: Observation
    valid: bool                                 # the reply was an acceptable action or answer
    effect: str                                 # short text: "moved", "answer recorded", "delivered"
    done: bool
    info: dict[str, Any] = field(default_factory=dict)


class Environment:
    id: str = "base"
    version: str = "0.0.0"

    def __init__(self, **options: Any) -> None:
        self.options = options

    # -- public ---------------------------------------------------------------
    def describe(self) -> dict:
        """Declared once. Keys the harness reads:
        id, version, name, kind, tasks, observation, actions, utility {meaning, direction},
        brief, renderer, oracle, floor_policy, scoring ("deterministic" | "judge" | "mixed"),
        tool_policy {"forbid": [skill names], "allow": [skill names] or None, "void_on_violation": bool}."""
        raise NotImplementedError

    def reset(self, task: str, seed: int) -> Observation:
        raise NotImplementedError

    def parse_action(self, text: str) -> str | None:
        """Map the agent's free text to one canonical action or answer, or None."""
        raise NotImplementedError

    def step(self, action: str | None) -> Receipt:
        """action is canonical, or None for a reply that did not parse."""
        raise NotImplementedError

    def delivered(self) -> Receipt:
        """Called after an observation with expects_action=False has reached the agent."""
        raise NotImplementedError(f"{self.id} sends no information-only messages")

    def render(self) -> dict | None:
        """A JSON frame of what the agent has been shown so far. Never privileged state."""
        return None

    # -- privileged (harness only) -----------------------------------------------
    def score(self) -> dict:
        """success, utility, terminal, steps and metrics for the episode so far.
        success and utility may be None with needs_judge=True when only a judge can decide."""
        raise NotImplementedError

    def floor(self) -> float | None:
        """Utility of the declared trivial policy on this instance."""
        return None

    def ceiling(self) -> dict | None:
        """{utility, steps} of an oracle on this instance, or None."""
        return None

    def max_steps(self) -> int:
        raise NotImplementedError

    def solution(self) -> list[str] | None:
        """Privileged: the oracle's replies, one per observation that expects an action."""
        return None

    def fingerprint(self) -> dict | None:
        """What the episodes depend on besides this code and its options, such as a data file.
        It is hashed into the run id, so a new version of the data never shares a run id."""
        return None


EnvFactory = Callable[[], Environment]


def load_adapter(spec: str) -> type[Environment]:
    """'package.module:ClassName' -> class."""
    import importlib

    module, _, name = spec.partition(":")
    cls = getattr(importlib.import_module(module), name)
    if not (isinstance(cls, type) and issubclass(cls, Environment)):
        raise TypeError(f"{spec} is not an omegaeval Environment")
    return cls
