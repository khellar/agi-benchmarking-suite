"""Reference agents that play through the same websocket protocol as OmegaClaw.

They connect to the gateway exactly as channels/wschat.py does: bearer token, a resume
frame after connecting, user_message in, agent_message out. That keeps the floor and
ceiling references on the same code path as the agents they bound.

    random   picks uniformly from the offered actions, or a random option letter for a
             multiple-choice question; says it does not know otherwise (floor, public view only)
    oracle   replays the environment's privileged solution (ceiling)

Both reply only to messages that carry a Reply line. Information-only messages are read
and not answered, as the protocol asks.
"""
from __future__ import annotations

import json
import random
import re
import secrets
import threading
import uuid

from .config import AgentSpec

REPLY_RE = re.compile(r"^(?:Reply|Advise): (ep=\S+ t=\d+) ", re.MULTILINE)
TAG_RE = re.compile(r"\[omegaeval ep=(\w+) t=(\d+)\]")
END_RE = re.compile(r"\[omegaeval ep=\w+ END\]")
ACTIONS_RE = re.compile(r"^Actions: (.+)$", re.MULTILINE)
CHOICE_RE = re.compile(r"^([A-Z])\. ", re.MULTILINE)


class Policy:
    name = "policy"
    label = ""
    privilege = "public"

    def prepare(self, env) -> None:
        pass

    def act(self, k: int, actions: list[str], text: str) -> str | None:
        """k counts the observations that asked for a reply in this episode, from 0."""
        raise NotImplementedError


class RandomPolicy(Policy):
    name, label, privilege = "random", "random policy (floor reference)", "public"

    def prepare(self, env) -> None:
        self.rng = random.Random(f"random:{getattr(env, 'id', '')}:{getattr(env, 'task', '')}:"
                                 f"{getattr(env, 'seed', 0)}")

    def act(self, k, actions, text):
        if actions:
            return self.rng.choice(actions)
        letters = CHOICE_RE.findall(text)
        if letters:
            return self.rng.choice(letters)
        return "I don't know"


class OraclePolicy(Policy):
    name, label, privilege = "oracle", "oracle policy with the privileged solution (ceiling reference)", "privileged"

    def prepare(self, env) -> None:
        plan = env.solution()
        if not plan:
            raise TypeError(f"{env.id} offers no privileged solution for an oracle")
        self.plan = list(plan)

    def act(self, k, actions, text):
        return self.plan[k] if k < len(self.plan) else None


POLICIES = {"random": RandomPolicy, "oracle": OraclePolicy}


class BaselineAgent(threading.Thread):
    def __init__(self, agent_id: str, url: str, token: str, policy: Policy):
        super().__init__(name=f"baseline-{agent_id}", daemon=True)
        self.agent_id, self.url, self.token, self.policy = agent_id, url, token, policy
        self.ready = threading.Event()
        self._halt = threading.Event()
        self.error: str | None = None
        self._episode = None
        self._k = 0

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        from websockets.sync.client import connect

        try:
            with connect(self.url, additional_headers={"Authorization": f"Bearer {self.token}"},
                         open_timeout=15) as ws:
                ws.send(json.dumps({"type": "resume", "last_seen_seq": None}))
                self.ready.set()
                while not self._halt.is_set():
                    try:
                        raw = ws.recv(timeout=0.5)
                    except TimeoutError:
                        continue
                    frame = json.loads(raw)
                    if frame.get("type") != "user_message":
                        continue
                    reply = self._decide(frame.get("text", ""))
                    if reply:
                        ws.send(json.dumps({"type": "agent_message", "client_seq": uuid.uuid4().hex,
                                            "text": reply}))
        except Exception as exc:
            self.error = str(exc)
            self.ready.set()

    def _decide(self, text: str) -> str | None:
        if END_RE.search(text):
            self._episode = None
            return None
        m = REPLY_RE.search(text)
        if not m:
            return None                     # information only, or advice relayed from a hive
        prefix = m.group(1)
        episode = prefix.split()[0]
        if episode != self._episode:
            self._episode, self._k = episode, 0
        a = ACTIONS_RE.search(text)
        actions = [x.strip() for x in a.group(1).split("|")] if a else []
        reply = self.policy.act(self._k, actions, text)
        self._k += 1
        return f"{prefix} {reply}" if reply else None


def spec_for(policy_name: str) -> tuple[AgentSpec, Policy]:
    policy = POLICIES[policy_name]()
    spec = AgentSpec(id=f"baseline-{policy_name}", token=secrets.token_hex(16), label=policy.label,
                     kind="baseline", config={"policy": policy_name, "privilege": policy.privilege})
    return spec, policy
