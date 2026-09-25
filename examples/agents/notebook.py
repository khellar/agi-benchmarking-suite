"""A small agent that speaks all three request-and-response transports. Use it as a template.

It keeps a notebook of everything it reads, answers arithmetic, picks prime numbers, recalls facts
it was told, and otherwise takes the first offered action. It reports what it did as trace events,
so the console's lanes fill in.

    python   connect = { python = "notebook:Notebook", cwd = "examples/agents" }
    command  connect = { command = ["python3", "examples/agents/notebook.py"] }
    http     python3 examples/agents/notebook.py --http 9100
             connect = { http = "http://127.0.0.1:9100/" }

Every message is a JSON object with protocol, episode, step, kind (brief, observation, info, end or
hive), text, actions, expects_reply and, on the first message, instructions. The answer is a JSON
object with reply, optionally p and trace; an information message is answered with {} once read.
"""
from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Notebook:
    def __init__(self, **options):
        self.pages: list[str] = []

    def act(self, message: dict) -> dict:
        kind = message.get("kind")
        text = message.get("text") or ""
        if kind in ("brief", "observation", "info"):
            self.pages.append(text)
        reading = kind in ("brief", "info") and not message.get("expects_reply")
        trace = [{"kind": "memory", "store": "notebook", "op": "add", "text": text[:200]}] if reading else []
        if not message.get("expects_reply"):
            return {"trace": trace}
        reply, how = self.answer(text, message.get("actions") or [])
        trace.append({"kind": "reasoning", "engine": "notebook", "premises": [text[:200]], "conclusions": [how]})
        return {"reply": reply, "trace": trace}

    def answer(self, text: str, actions: list[str]) -> tuple[str, str]:
        m = re.search(r"What is (\d+) × (\d+)\?", text)
        if m:
            return str(int(m.group(1)) * int(m.group(2))), "multiplied"
        for letter, n in re.findall(r"^([A-Z])\. (\d+)$", text, re.M):
            if int(n) > 1 and all(int(n) % d for d in range(2, int(int(n) ** 0.5) + 1)):
                return letter, f"{n} is prime"
        q = text.lower()
        topic = next((t for t, words in TOPICS.items() if any(w in q for w in words)), None)
        if topic and "question" in q:
            for page in reversed(self.pages[:-1]):             # the latest mention wins, so updates count
                hits = re.findall(FACTS[topic], page)
                if hits:
                    return hits[-1], "recalled from the notebook"
        return (actions[0] if actions else "I don't know"), "first offered action"


TOPICS = {"sister": ("sister",), "dog": ("dog",), "city": ("city", "live")}
FACTS = {"sister": r"sister (?:goes by )?([A-Z]\w+)", "dog": r"(?:named him|renaming the dog) ([A-Z]\w+)",
         "city": r"(?:live in|moved to) ([A-Z]\w+)"}


def serve_stdio() -> None:
    agent = Notebook()
    for line in sys.stdin:
        if line.strip():
            print(json.dumps(agent.act(json.loads(line))), flush=True)


def serve_http(port: int) -> None:
    agents: dict[str, Notebook] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            agent = agents.setdefault(message.get("episode", ""), Notebook())
            body = json.dumps(agent.act(message)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--http":
        serve_http(int(sys.argv[2]))
    else:
        serve_stdio()
