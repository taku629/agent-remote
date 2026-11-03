"""Three-line integration for Python agents.

    from agent_remote.client import Remote
    remote = Remote("http://127.0.0.1:8000", agent="my-bot", goal="clean up the repo")

    @remote.guard
    def delete_file(path: str): ...

Each guarded call waits for Allow / Deny / Fix on the phone. A denial raises
`Denied`, whose message carries the human's reason so the agent can re-plan.
"""
from __future__ import annotations

import functools
import inspect
from typing import Any, Callable

import httpx


def _is_local(url: str) -> bool:
    return "127.0.0.1" in url or "localhost" in url


class Denied(Exception):
    pass


class Remote:
    def __init__(self, url: str = "http://127.0.0.1:8000", agent: str = "agent", goal: str = "", token: str = ""):
        self.url, self.agent, self.goal = url.rstrip("/"), agent, goal
        self.http = httpx.Client(timeout=None, headers={"x-remote-token": token} if token else {},
                                 trust_env=not _is_local(self.url))

    def ask(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        rec = self.http.post(f"{self.url}/api/calls",
                             json={"agent": self.agent, "goal": self.goal, "tool": tool, "args": args}).json()
        if rec.get("status") != "allowed":
            msg = rec.get("reason", "denied")
            if rec.get("feedback"):
                msg += f" | guidance: {rec['feedback']}"
            raise Denied(msg)
        return rec

    def guard(self, fn: Callable) -> Callable:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*a, **kw):
            bound = sig.bind(*a, **kw)
            bound.apply_defaults()
            rec = self.ask(fn.__name__, dict(bound.arguments))
            return fn(**rec.get("args", bound.arguments))

        return wrapper
