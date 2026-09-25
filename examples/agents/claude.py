"""A model-only baseline: Claude through the Anthropic Messages API, with no memory beyond the episode.

Benchmark it next to OmegaClaw to see what the architecture adds over the model it runs on:

    [agents.claude]
    label = "Claude Opus 5.5, no tools"
    connect = { python = "claude:Claude", cwd = "examples/agents" }

It reads ANTHROPIC_API_KEY from the environment, and OMEGAEVAL_MODEL to pick the model. Information
messages are kept in the conversation and cost nothing until a question arrives. Token usage is
reported with every call, so the console's LLM lane, the token counts and the cost fill in.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

API = "https://api.anthropic.com"
MODEL = "claude-opus-5-5"
SYSTEM = ("You are playing one episode of a benchmark. Follow its instructions. When a message asks for an "
          "action, reply with exactly one of the offered actions and nothing else. When it asks a question, reply "
          "with the answer only.")


class Claude:
    def __init__(self, model: str | None = None, max_tokens: int = 1024, **options):
        self.model = model or os.environ.get("OMEGAEVAL_MODEL", MODEL)
        self.max_tokens = max_tokens
        self.base = os.environ.get("ANTHROPIC_BASE_URL", API).rstrip("/")
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.system = SYSTEM
        self.turns: list[dict] = []

    def act(self, message: dict) -> dict:
        kind = message.get("kind")
        if kind == "brief" and message.get("instructions"):
            self.system = f"{SYSTEM}\n\n{message['instructions']}"
        if kind in ("end", None):
            return {}
        text = message.get("text") or ""
        if message.get("actions"):
            text += "\nActions: " + " | ".join(message["actions"])
        if kind == "hive":
            text = f"Another agent says: {text}"
        self._add("user", text)
        if not message.get("expects_reply"):
            return {}
        reply, usage, stop = self._complete()
        self._add("assistant", reply)
        trace = [{"kind": "llm", "provider": "anthropic", "model": self.model, "finish_reason": stop,
                  "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
                  "cached_tokens": usage.get("cache_read_input_tokens", 0)},
                 {"kind": "context", "chars": len(self.system) + sum(len(t["content"]) for t in self.turns)}]
        return {"reply": reply, "trace": trace}

    def _add(self, role: str, text: str) -> None:
        if self.turns and self.turns[-1]["role"] == role:
            self.turns[-1]["content"] += "\n\n" + text
        else:
            self.turns.append({"role": role, "content": text})

    def _complete(self) -> tuple[str, dict, str]:
        if not self.key:
            raise RuntimeError("set ANTHROPIC_API_KEY in the environment of the harness")
        body = {"model": self.model, "max_tokens": self.max_tokens, "system": self.system, "messages": self.turns}
        req = urllib.request.Request(f"{self.base}/v1/messages", data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"x-api-key": self.key, "anthropic-version": "2023-06-01",
                                              "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read())
        reply = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        return reply, data.get("usage") or {}, data.get("stop_reason") or ""


if __name__ == "__main__":                        # the command transport: JSON Lines on stdin and stdout
    agent = Claude()
    for line in sys.stdin:
        if line.strip():
            print(json.dumps(agent.act(json.loads(line))), flush=True)
