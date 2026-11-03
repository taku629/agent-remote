"""Put Agent Remote in front of ANY stdio MCP server.

    python -m agent_remote.mcp_proxy --remote http://127.0.0.1:8000 --goal "tidy my repo" \
        -- npx -y @modelcontextprotocol/server-filesystem ~/project

Point your MCP client (Claude Desktop, Cursor, Cline...) at this command instead of
the real server. Every `tools/call` is held until the phone (or a house rule) says yes.
Denied calls come back to the agent as a tool error that explains why, so the agent
can change course instead of crashing. Everything else is passed through untouched.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading

import httpx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remote", default="http://127.0.0.1:8000")
    ap.add_argument("--token", default="")
    ap.add_argument("--agent", default="mcp-agent")
    ap.add_argument("--goal", default="")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        ap.error("give the upstream MCP server command after --")

    up = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    out_lock = threading.Lock()

    def to_client(obj: dict) -> None:
        with out_lock:
            sys.stdout.write(json.dumps(obj) + "\n")
            sys.stdout.flush()

    def pump_up() -> None:
        assert up.stdout
        for line in up.stdout:
            with out_lock:
                sys.stdout.write(line)
                sys.stdout.flush()

    headers = {"x-remote-token": a.token} if a.token else {}
    http = httpx.Client(timeout=None, headers=headers, trust_env="127.0.0.1" not in a.remote and "localhost" not in a.remote)

    def guard(msg: dict) -> None:
        p = msg.get("params", {})
        try:
            rec = http.post(f"{a.remote}/api/calls", json={
                "agent": a.agent, "goal": a.goal, "tool": p.get("name", "?"), "args": p.get("arguments", {})}).json()
        except Exception as e:  # noqa: BLE001  fail closed
            rec = {"status": "denied", "reason": f"Agent Remote unreachable: {e}"}
        if rec.get("status") == "allowed":
            p["arguments"] = rec.get("args", p.get("arguments", {}))
            msg["params"] = p
            assert up.stdin
            up.stdin.write(json.dumps(msg) + "\n")
            up.stdin.flush()
        else:
            text = f"The human denied this action via Agent Remote. Reason: {rec.get('reason', 'denied')}."
            if rec.get("feedback"):
                text += f" Their guidance: {rec['feedback']}"
            to_client({"jsonrpc": "2.0", "id": msg.get("id"),
                       "result": {"isError": True, "content": [{"type": "text", "text": text}]}})

    pump = threading.Thread(target=pump_up, daemon=True)
    pump.start()
    guards: list[threading.Thread] = []
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("method") == "tools/call":
            t = threading.Thread(target=guard, args=(msg,), daemon=True)
            t.start()
            guards.append(t)
        else:
            assert up.stdin
            up.stdin.write(line if line.endswith("\n") else line + "\n")
            up.stdin.flush()

    # client closed stdin: let pending approvals finish, then shut the upstream down cleanly
    for t in guards:
        t.join()
    assert up.stdin
    up.stdin.close()
    up.wait(timeout=10)
    pump.join(timeout=5)


if __name__ == "__main__":
    main()
