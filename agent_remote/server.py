"""Agent Remote server: the hub between AI agents and the human's phone.

Agents (via the MCP proxy, the Python SDK or plain HTTP) POST each tool call to
/api/calls and wait for a verdict. The phone UI (PWA served at /) streams pending
calls over Server-Sent Events and answers Allow / Deny / Fix / Kill.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import llm

WEB = Path(__file__).parent / "web"
TOKEN = os.getenv("REMOTE_TOKEN", "")  # optional shared secret for agents + phone
LANG = os.getenv("REMOTE_LANG", "English")
TIMEOUT_S = int(os.getenv("REMOTE_TIMEOUT_S", "600"))
DEMO = os.getenv("DEMO_MODE", "") == "1"
PORT = os.getenv("PORT", "8000")

app = FastAPI(title="Agent Remote")


class State:
    def __init__(self) -> None:
        self.calls: dict[str, dict[str, Any]] = {}
        self.rules: list[dict[str, Any]] = []
        self.killed: bool = False
        self.listeners: set[asyncio.Queue] = set()
        self.waiters: dict[str, asyncio.Event] = {}
        self.tools_seen: set[str] = set()

    async def broadcast(self, kind: str, data: Any) -> None:
        for q in list(self.listeners):
            q.put_nowait({"kind": kind, "data": data})


S = State()


def _auth(req: Request) -> None:
    if not TOKEN:
        return
    got = req.headers.get("x-remote-token") or req.query_params.get("token")
    if got != TOKEN:
        raise HTTPException(401, "bad token")


# --------------------------------------------------------------------------- rules

def _rule_matches(rule: dict[str, Any], call: dict[str, Any], risk: str) -> bool:
    tool = call["tool"].lower()
    if rule.get("tools") and not any(t.lower() in tool for t in rule["tools"]):
        return False
    blob = json.dumps(call.get("args", {}), ensure_ascii=False)
    for pat in rule.get("arg_patterns") or []:
        try:
            if not re.search(pat, blob, re.I):
                return False
        except re.error:
            if pat.lower() not in blob.lower():
                return False
    mr = rule.get("min_risk")
    if mr in llm.RISK_LEVELS and llm.RISK_LEVELS.index(risk) < llm.RISK_LEVELS.index(mr):
        return False
    return True


def _decide_by_rules(call: dict[str, Any], risk: str) -> dict[str, Any] | None:
    # most restrictive matching rule wins: block > ask > allow
    order = {"block": 0, "ask": 1, "allow": 2}
    hits = [r for r in S.rules if r.get("enabled", True) and _rule_matches(r, call, risk)]
    if not hits:
        return None
    return sorted(hits, key=lambda r: order[r["action"]])[0]


# --------------------------------------------------------------------------- API: agents

class CallIn(BaseModel):
    agent: str = "agent"
    goal: str = ""
    tool: str
    args: dict[str, Any] = {}
    wait: bool = True


@app.post("/api/calls")
async def submit_call(body: CallIn, req: Request):
    _auth(req)
    S.tools_seen.add(body.tool)
    cid = uuid.uuid4().hex[:8]
    call = {"tool": body.tool, "args": body.args}
    rec: dict[str, Any] = {
        "id": cid, "agent": body.agent, "goal": body.goal, "tool": body.tool,
        "args": body.args, "status": "analyzing", "created": time.time(),
    }
    S.calls[cid] = rec
    await S.broadcast("call", rec)

    if S.killed:
        rec.update(status="denied", reason="Kill switch is on", decided_by="kill switch")
        await S.broadcast("call", rec)
        return rec

    rec["analysis"] = await asyncio.to_thread(llm.explain, call, body.goal, LANG)
    risk = rec["analysis"]["risk"]
    rule = _decide_by_rules(call, risk)
    if rule and rule["action"] == "block":
        rec.update(status="denied", reason=f"House rule: {rule['text']}", decided_by="rule")
    elif rule and rule["action"] == "allow":
        rec.update(status="allowed", reason=f"House rule: {rule['text']}", decided_by="rule")
    elif not rule and risk == "low":
        rec.update(status="allowed", reason="Low risk, auto-allowed", decided_by="auto")
    else:
        rec["status"] = "pending"
        if rule:
            rec["rule"] = rule["text"]
    await S.broadcast("call", rec)

    if rec["status"] == "pending" and body.wait:
        return await _wait(cid)
    return rec


async def _wait(cid: str):
    ev = S.waiters.setdefault(cid, asyncio.Event())
    try:
        await asyncio.wait_for(ev.wait(), TIMEOUT_S)
    except asyncio.TimeoutError:
        rec = S.calls[cid]
        rec.update(status="denied", reason="No answer in time", decided_by="timeout")
        await S.broadcast("call", rec)
    return S.calls[cid]


@app.get("/api/calls/{cid}/wait")
async def wait_call(cid: str, req: Request):
    _auth(req)
    if cid not in S.calls:
        raise HTTPException(404)
    if S.calls[cid]["status"] != "pending":
        return S.calls[cid]
    return await _wait(cid)


# --------------------------------------------------------------------------- API: phone

class DecisionIn(BaseModel):
    verdict: str  # allow | deny | fix
    note: str = ""


@app.post("/api/calls/{cid}/decision")
async def decide(cid: str, body: DecisionIn, req: Request):
    _auth(req)
    rec = S.calls.get(cid)
    if not rec:
        raise HTTPException(404)
    if rec["status"] != "pending":
        raise HTTPException(409, f"already {rec['status']}")
    if body.verdict == "allow":
        rec.update(status="allowed", decided_by="human", reason=body.note or "Approved on phone")
    elif body.verdict == "deny":
        rec.update(status="denied", decided_by="human", reason=body.note or "Denied on phone")
    elif body.verdict == "fix":
        if not body.note.strip():
            raise HTTPException(400, "say what to change")
        try:
            out = await asyncio.to_thread(llm.amend, {"tool": rec["tool"], "args": rec["args"]}, body.note)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"could not rewrite: {e}") from e
        rec.update(original_args=rec["args"], args=out["args"], status="allowed", decided_by="human (fixed)",
                   reason=out.get("note") or body.note, fix=body.note)
    else:
        raise HTTPException(400, "verdict must be allow, deny or fix")
    if body.note and body.verdict != "fix":
        rec["feedback"] = body.note  # passed back to the agent as guidance
    await S.broadcast("call", rec)
    S.waiters.setdefault(cid, asyncio.Event()).set()
    return rec


class KillIn(BaseModel):
    on: bool


@app.post("/api/kill")
async def kill(body: KillIn, req: Request):
    _auth(req)
    S.killed = body.on
    if body.on:
        for rec in S.calls.values():
            if rec["status"] in ("pending", "analyzing"):
                rec.update(status="denied", reason="Kill switch", decided_by="kill switch")
                S.waiters.setdefault(rec["id"], asyncio.Event()).set()
                await S.broadcast("call", rec)
    await S.broadcast("kill", {"on": S.killed})
    return {"on": S.killed}


class RuleIn(BaseModel):
    text: str


@app.post("/api/rules")
async def add_rule(body: RuleIn, req: Request):
    _auth(req)
    compiled = await asyncio.to_thread(llm.compile_rule, body.text, sorted(S.tools_seen))
    rule = {"id": uuid.uuid4().hex[:6], "text": body.text, "enabled": True, **compiled}
    S.rules.append(rule)
    await S.broadcast("rules", S.rules)
    return rule


@app.delete("/api/rules/{rid}")
async def del_rule(rid: str, req: Request):
    _auth(req)
    S.rules = [r for r in S.rules if r["id"] != rid]
    await S.broadcast("rules", S.rules)
    return {"ok": True}


@app.get("/api/state")
async def state(req: Request):
    _auth(req)
    calls = sorted(S.calls.values(), key=lambda r: r["created"], reverse=True)[:100]
    return {"calls": calls, "rules": S.rules, "killed": S.killed, "model": llm.MODEL, "demo": DEMO}


_demo_running = {"on": False}


@app.post("/api/demo")
async def run_demo(req: Request):
    """Public demo: launch the scripted cleanup-bot against this very server."""
    _auth(req)
    if not DEMO:
        raise HTTPException(404)
    if _demo_running["on"]:
        raise HTTPException(409, "a demo agent is already running")
    if S.killed:
        raise HTTPException(409, "kill switch is on")
    import subprocess
    import sys
    _demo_running["on"] = True
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "DEMO_STEP_DELAY": "1.5"}
    proc = subprocess.Popen([sys.executable, str(root / "demo" / "demo_agent.py"), "--scripted",
                             "--remote", f"http://127.0.0.1:{PORT}", "--token", TOKEN], env=env)

    async def reap():
        await asyncio.to_thread(proc.wait)
        _demo_running["on"] = False

    asyncio.create_task(reap())
    return {"started": True}


@app.get("/api/stream")
async def stream(req: Request):
    _auth(req)
    q: asyncio.Queue = asyncio.Queue()
    S.listeners.add(q)

    async def gen():
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), 15)
                    yield f"event: {msg['kind']}\ndata: {json.dumps(msg['data'], ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                if await req.is_disconnected():
                    break
        finally:
            S.listeners.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/healthz")
async def health():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


app.mount("/", StaticFiles(directory=WEB), name="web")
