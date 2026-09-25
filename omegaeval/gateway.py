"""Serve OmegaClaw's websocket chat channel to many agents at once.

OmegaClaw ships a websocket client channel (channels/wschat.py, `commchannel=websocket`).
This module is the server side of that protocol, so an agent needs no code change to
play: point its WS_URL at the gateway and give it the token from omegaeval.toml.

Frames, as documented in channels/wschat.py:
    server -> agent  {"type": "user_message", "seq": int, "text": str}
    agent  -> server {"type": "resume", "last_seen_seq": int | null}   (after every connect)
    agent  -> server {"type": "agent_message", "client_seq": str, "text": str}
    server -> agent  {"type": "ack", "seq": null, "client_seq": str}

One more frame, for agents other than OmegaClaw that can report their own cognition:
    agent  -> server {"type": "trace", "kind": "concepts", "step": int,
                      "concepts": [{"id": str, "label": str, "grounding": [...], "confidence": float}]}
It is recorded with the episode and fills the console's Concepts lane. OmegaClaw does
not send it: it has no concept store.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time

log = logging.getLogger("omegaeval.gateway")


class AgentLink:
    """One agent's connection and message history."""

    def __init__(self, agent_id: str, token: str):
        self.agent_id = agent_id
        self.token = token
        self.lock = threading.Lock()
        self.ws = None
        self.next_seq = 1
        self.sent: list[tuple[int, str, float]] = []
        self.inbox: "queue.Queue[tuple[float, str, str]]" = queue.Queue()
        self.traces: "queue.Queue[tuple[float, dict]]" = queue.Queue()
        self.replay_floor = None          # first seq of the active episode; older messages are never replayed
        self.connected_at: float | None = None
        self.last_seen: float | None = None
        self.version: str | None = None   # "Omega version=..." that Omega sends when it starts
        self.remote: str | None = None
        self.connections = 0

    @property
    def connected(self) -> bool:
        return self.ws is not None

    def status(self) -> dict:
        return {"agent": self.agent_id, "connected": self.connected, "connections": self.connections,
                "connected_at": self.connected_at, "last_seen": self.last_seen, "version": self.version,
                "remote": self.remote, "messages_sent": len(self.sent)}

    def begin_episode(self) -> None:
        with self.lock:
            self.replay_floor = self.next_seq
        self.drain()

    def end_episode(self) -> None:
        with self.lock:
            self.replay_floor = None

    def drain(self) -> list[tuple[float, str, str]]:
        out = []
        while True:
            try:
                out.append(self.inbox.get_nowait())
            except queue.Empty:
                return out

    def drain_traces(self) -> list[tuple[float, dict]]:
        out = []
        while True:
            try:
                out.append(self.traces.get_nowait())
            except queue.Empty:
                return out

    def send(self, text: str) -> int:
        with self.lock:
            seq = self.next_seq
            self.next_seq += 1
            self.sent.append((seq, text, time.time()))
            ws = self.ws
        if ws is not None:
            try:
                ws.send(json.dumps({"type": "user_message", "seq": seq, "text": text}))
            except Exception as exc:  # delivered later by resume replay
                log.warning("send to %s failed, will replay on reconnect: %s", self.agent_id, exc)
        return seq

    def _replay(self, ws, last_seen) -> None:
        with self.lock:
            floor = self.replay_floor
            pending = [(s, t) for s, t, _ in self.sent
                       if floor is not None and s >= floor and (last_seen is None or s > last_seen)]
        for s, t in pending:
            ws.send(json.dumps({"type": "user_message", "seq": s, "text": t}))


class Gateway:
    def __init__(self, host: str = "0.0.0.0", port: int = 8770):
        self.host, self.port = host, port
        self.links: dict[str, AgentLink] = {}
        self._by_token: dict[str, AgentLink] = {}
        self._server = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def add(self, agent_id: str, token: str) -> AgentLink:
        if not token:
            raise ValueError(f"agent {agent_id!r} needs a token")
        if token in self._by_token and self._by_token[token].agent_id != agent_id:
            raise ValueError(f"agents {agent_id!r} and {self._by_token[token].agent_id!r} share a token")
        link = self.links.get(agent_id) or AgentLink(agent_id, token)
        link.token = token
        self.links[agent_id] = link
        self._by_token[token] = link
        return link

    # -- server ------------------------------------------------------------------
    def start(self) -> "Gateway":
        self._error: Exception | None = None
        self._thread = threading.Thread(target=self._serve, name="omegaeval-gateway", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10) or self._error is not None:
            reason = f": {self._error}" if self._error is not None else ""
            raise RuntimeError(f"the gateway cannot listen on {self.host}:{self.port}{reason}")
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()

    def _serve(self) -> None:
        from websockets.sync.server import serve

        try:
            with serve(self._handler, self.host, self.port, max_size=2 ** 20) as server:
                self._server = server
                if self.port == 0:
                    self.port = server.socket.getsockname()[1]
                self._ready.set()
                server.serve_forever()
        except OSError as exc:                     # the port is taken, usually by another omegaeval serve
            self._error = exc
            self._ready.set()

    def _identify(self, ws) -> AgentLink | None:
        headers = ws.request.headers
        auth = headers.get("Authorization", "") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        link = self._by_token.get(token)
        path = (ws.request.path or "").split("?")[0].rstrip("/")
        if link and path.startswith("/agents/") and path.split("/")[-1] != link.agent_id:
            return None
        return link

    def _handler(self, ws) -> None:
        link = self._identify(ws)
        if link is None:
            log.warning("rejected connection from %s: unknown token or path", ws.remote_address)
            ws.close(1008, "unauthorized")
            return
        with link.lock:
            old = link.ws
            link.ws = ws
            link.connections += 1
            link.connected_at = time.time()
            link.remote = str(ws.remote_address)
        if old is not None:
            try:
                old.close(1000, "replaced by a new connection")
            except Exception:
                pass
        log.info("agent %s connected from %s", link.agent_id, link.remote)
        try:
            for raw in ws:
                self._on_frame(link, ws, raw)
        except Exception as exc:
            log.info("agent %s disconnected: %s", link.agent_id, exc)
        finally:
            with link.lock:
                if link.ws is ws:
                    link.ws = None

    def _on_frame(self, link: AgentLink, ws, raw) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(frame, dict):
            return
        link.last_seen = time.time()
        kind = frame.get("type")
        if kind == "resume":
            link._replay(ws, frame.get("last_seen_seq"))
        elif kind == "agent_message":
            text = frame.get("text")
            client_seq = str(frame.get("client_seq", ""))
            if not isinstance(text, str):
                return
            try:
                ws.send(json.dumps({"type": "ack", "seq": None, "client_seq": client_seq}))
            except Exception:
                pass
            if text.startswith("Omega version=") or text.startswith("Omega unknown"):
                link.version = text.strip()
            link.inbox.put((time.time(), text, client_seq))
        elif kind == "trace" and isinstance(frame.get("kind"), str):
            link.traces.put((time.time(), frame))
