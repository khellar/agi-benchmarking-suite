"""keydoor: the reference environment that ships with the harness.

A seeded maze with a key and a locked exit, played one move at a time with partial
observation: the agent sees the walls around its own cell and what lies on it or
next to it. It exists to exercise every part of the harness end to end. It is not
AGI Maze; AGI Maze plugs in through the same contract.
"""
from __future__ import annotations

import random
from collections import deque

from .base import Environment, Observation, Receipt

DIRS = {"N": (-1, 0), "E": (0, 1), "S": (1, 0), "W": (0, -1)}
TASKS = {"small": 7, "classic": 9, "large": 13}


def generate(size: int, seed: int) -> tuple[list[list[str]], tuple, tuple, tuple]:
    """Perfect maze by iterative depth-first carving. Returns grid, start, key, exit."""
    rng = random.Random(seed)
    grid = [["#"] * size for _ in range(size)]
    start = (1, 1)
    grid[1][1] = "."
    stack = [start]
    while stack:
        r, c = stack[-1]
        options = []
        for dr, dc in DIRS.values():
            nr, nc = r + 2 * dr, c + 2 * dc
            if 0 < nr < size - 1 and 0 < nc < size - 1 and grid[nr][nc] == "#":
                options.append((nr, nc, dr, dc))
        if not options:
            stack.pop()
            continue
        nr, nc, dr, dc = rng.choice(options)
        grid[r + dr][c + dc] = "."
        grid[nr][nc] = "."
        stack.append((nr, nc))
    dist_start = bfs(grid, start)
    key = max(dist_start, key=lambda p: (dist_start[p], p))
    dist_key = bfs(grid, key)
    exit_ = max((p for p in dist_key if p not in (start, key)), key=lambda p: (dist_key[p], p))
    return grid, start, key, exit_


def bfs(grid, src) -> dict:
    dist = {src: 0}
    q = deque([src])
    while q:
        r, c = q.popleft()
        for dr, dc in DIRS.values():
            n = (r + dr, c + dc)
            if grid[n[0]][n[1]] != "#" and n not in dist:
                dist[n] = dist[(r, c)] + 1
                q.append(n)
    return dist


def shortest_path(grid, src, dst) -> list[str]:
    prev = {src: None}
    q = deque([src])
    while q:
        cur = q.popleft()
        if cur == dst:
            break
        for d, (dr, dc) in DIRS.items():
            n = (cur[0] + dr, cur[1] + dc)
            if grid[n[0]][n[1]] != "#" and n not in prev:
                prev[n] = (cur, d)
                q.append(n)
    moves, node = [], dst
    while prev[node] is not None:
        node, d = prev[node]
        moves.append(d)
    return moves[::-1]


class KeyDoor(Environment):
    id = "keydoor"
    version = "0.1.0"

    def describe(self) -> dict:
        return {
            "id": self.id,
            "version": self.version,
            "name": "Key and locked exit (reference gridworld)",
            "kind": "interactive", "scoring": "deterministic", "tool_policy": {"forbid": []},
            "default_task": "classic",
            "tasks": list(TASKS),
            "observation": "text: position, adjacent walls, items on or next to the agent's cell",
            "actions": "move N|E|S|W, pick, open",
            "utility": {"meaning": "1 if the exit is opened with the key before the step budget runs out, else 0",
                        "direction": "higher"},
            "renderer": "grid",
            "oracle": True,
            "floor_policy": "noop (never solves)",
            "brief": ("You are in a maze. Find the key, pick it up, then reach the exit and open it. "
                      "You only see the walls around your own cell. Coordinates are (row, column); "
                      "N is row minus one."),
        }

    # -- public ---------------------------------------------------------------
    def reset(self, task: str, seed: int) -> Observation:
        if task not in TASKS:
            raise ValueError(f"unknown task {task!r}; tasks: {list(TASKS)}")
        self.task, self.seed = task, seed
        self.size = TASKS[task]
        self.grid, self.pos, self.key, self.exit = generate(self.size, seed)
        self.start = self.pos
        self.has_key = False
        self.solved = False
        self.steps = 0
        self.invalid = 0
        self.bumps = 0
        self.refusals = 0
        self.visited = {self.pos}
        self.seen = set()
        self._look()
        self._budget = max(60, 4 * self.ceiling()["steps"])
        return self._observe("episode start")

    def parse_action(self, text: str) -> str | None:
        t = " ".join(text.lower().replace("_", " ").split())
        for d in DIRS:
            if t in (f"move {d.lower()}", d.lower(), f"go {d.lower()}"):
                return f"move {d}"
        if t in ("pick", "pick up", "pickup", "take", "take key", "pick key", "pick up key"):
            return "pick"
        if t in ("open", "open exit", "open door"):
            return "open"
        return None

    def step(self, action: str | None) -> Receipt:
        self.steps += 1
        valid = action in self._actions()
        if not valid:
            self.invalid += 1
            effect = "not an available action"
        elif action.startswith("move"):
            dr, dc = DIRS[action[-1]]
            n = (self.pos[0] + dr, self.pos[1] + dc)
            if self.grid[n[0]][n[1]] == "#":
                self.bumps += 1
                effect = "bumped into a wall"
            else:
                self.pos = n
                self.visited.add(n)
                effect = "moved"
        elif action == "pick":
            if self.pos == self.key and not self.has_key:
                self.has_key = True
                effect = "picked up the key"
            else:
                self.refusals += 1
                effect = "nothing to pick up here"
        else:  # open
            if self.pos != self.exit:
                self.refusals += 1
                effect = "there is no exit here"
            elif not self.has_key:
                self.refusals += 1
                effect = "the exit is locked"
            else:
                self.solved = True
                effect = "opened the exit"
        self._look()
        done = self.solved or self.steps >= self._budget
        obs = self._observe(effect)
        return Receipt(obs, valid, effect, done,
                       {"pos": list(self.pos), "has_key": self.has_key, "steps": self.steps})

    def render(self) -> dict:
        cells = []
        for r in range(self.size):
            row = []
            for c in range(self.size):
                p = (r, c)
                if p not in self.seen:
                    row.append("?")
                elif self.grid[r][c] == "#":
                    row.append("#")
                elif p == self.key and not self.has_key:
                    row.append("K")
                elif p == self.exit:
                    row.append("E")
                else:
                    row.append("v" if p in self.visited else ".")
            cells.append("".join(row))
        return {"type": "grid", "rows": cells, "agent": list(self.pos), "has_key": self.has_key,
                "legend": {"?": "unseen", "#": "wall", ".": "seen", "v": "visited", "K": "key", "E": "exit"}}

    # -- privileged -------------------------------------------------------------
    def score(self) -> dict:
        terminal = "solved" if self.solved else ("budget_exhausted" if self.steps >= self._budget else "running")
        return {"success": self.solved, "utility": 1.0 if self.solved else 0.0, "terminal": terminal,
                "steps": self.steps,
                "metrics": {"invalid_actions": self.invalid, "bumps": self.bumps, "refusals": self.refusals,
                            "cells_visited": len(self.visited), "has_key": self.has_key,
                            "open_cells": sum(row.count(".") for row in self.grid)}}

    def floor(self) -> float:
        return 0.0

    def ceiling(self) -> dict:
        return {"utility": 1.0, "steps": len(self.solution())}

    def max_steps(self) -> int:
        return self._budget

    def solution(self) -> list[str]:
        """Privileged: the oracle's action sequence from the start."""
        to_key = shortest_path(self.grid, self.start, self.key)
        to_exit = shortest_path(self.grid, self.key, self.exit)
        return [f"move {d}" for d in to_key] + ["pick"] + [f"move {d}" for d in to_exit] + ["open"]

    # -- internals ----------------------------------------------------------------
    def _look(self) -> None:
        r, c = self.pos
        self.seen.add(self.pos)
        for dr, dc in DIRS.values():
            self.seen.add((r + dr, c + dc))

    def _actions(self) -> list[str]:
        return [f"move {d}" for d in DIRS] + ["pick", "open"]

    def _observe(self, effect: str) -> Observation:
        r, c = self.pos
        walls = [d for d, (dr, dc) in DIRS.items() if self.grid[r + dr][c + dc] == "#"]
        here, near = [], []
        if self.pos == self.key and not self.has_key:
            here.append("the key")
        if self.pos == self.exit:
            here.append("the exit")
        for d, (dr, dc) in DIRS.items():
            n = (r + dr, c + dc)
            if n == self.key and not self.has_key:
                near.append(f"key to the {d}")
            if n == self.exit:
                near.append(f"exit to the {d}")
        left = self._budget - self.steps if hasattr(self, "_budget") else None
        parts = [f"Last action: {effect}.", f"You are at ({r},{c}).",
                 f"Walls: {', '.join(walls) if walls else 'none'}."]
        if here:
            parts.append(f"Here: {', '.join(here)}.")
        if near:
            parts.append(f"Nearby: {', '.join(near)}.")
        parts.append(f"Holding the key: {'yes' if self.has_key else 'no'}.")
        if left is not None:
            parts.append(f"Steps left: {left}.")
        return Observation(" ".join(parts), self._actions(),
                           {"pos": [r, c], "walls": walls, "here": here, "near": near, "has_key": self.has_key})
