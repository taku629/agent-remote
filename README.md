# Agent Remote

**A remote control for AI agents.** Your agent keeps working; you keep the buttons.
Agent Remote holds every risky tool call an AI agent tries to make, has an LLM explain it in one plain sentence with a risk score, and lets you **Allow**, **Deny**, **Fix** (rewrite the call in plain words) or **KILL ALL** from your phone.

Built for the [Remote Control AI](https://remote-control-ai.devpost.com/) hackathon (Nov 2026).

## Why

Coding agents and MCP-connected assistants now delete files, send email and push code on their own.
Today the choices are "approve every step at the keyboard" or "YOLO mode". Neither works when you step away.
And the approval prompts that do exist show raw JSON like `{"paths": [...]}`, which nobody reads at 1 a.m.

Agent Remote is the middle ground: low-risk calls flow through, risky ones buzz your phone with a human sentence
("Email your Stripe key and DB password to backup@data-sync.example"), and you decide in three seconds.

## What the AI does (it is the core, not decoration)

| Step | What the LLM does |
|---|---|
| **Explain** | Turns each pending tool call into a title, a one-line summary, blast radius, reversibility and red flags. |
| **Score** | Rates risk low / medium / high / critical, and raises it when the call doesn't match the agent's stated goal (catches prompt injection). |
| **Suggest** | Proposes a safer version of the same call. |
| **Fix** | You type "only the .tmp files" and it rewrites the call's arguments, never widening scope. |
| **House rules** | You write rules in plain language ("Never let it touch .env files"); the LLM compiles them into a matcher (tool + regex + min risk + allow/ask/block). |

Low-risk calls are auto-allowed. Everything else waits for you (or a house rule). If the LLM is unreachable,
a rule-based fallback scores the call, and the MCP proxy fails closed if the remote itself is down.

## Plug it into any agent

**1. Any MCP client (Claude Desktop, Cursor, Cline, …)** - wrap the real server:

```bash
python -m agent_remote.mcp_proxy --remote http://127.0.0.1:8000 --goal "tidy my repo" \
  -- npx -y @modelcontextprotocol/server-filesystem ~/project
```

Every `tools/call` is held; denials go back to the agent as a tool error with your reason, so it re-plans instead of crashing.

**2. Python agents** - one decorator:

```python
from agent_remote.client import Remote
remote = Remote("http://127.0.0.1:8000", agent="my-bot", goal="clean up the repo")

@remote.guard
def delete_files(paths: list[str]): ...
```

**3. Anything else** - `POST /api/calls {agent, goal, tool, args}` and wait for the verdict.

## Run it

```bash
pip install -r requirements.txt
export LLM_API_KEY=...            # any OpenAI-compatible endpoint; default is Nebius Token Factory
uvicorn agent_remote.server:app --host 0.0.0.0 --port 8000
# open http://<your-pc-ip>:8000 on your phone (Add to Home Screen = app-like PWA)

python demo/demo_agent.py --scripted   # repeatable demo with a planted prompt injection
python demo/demo_agent.py              # real LLM agent with tool calling
```

| Env var | Default | Meaning |
|---|---|---|
| `LLM_BASE_URL` | `https://api.tokenfactory.nebius.com/v1` | OpenAI-compatible endpoint |
| `LLM_MODEL` | `Qwen/Qwen3-235B-A22B-Instruct-2507` | model used to explain / score / fix / compile rules |
| `LLM_API_KEY` | | API key |
| `REMOTE_TOKEN` | | shared secret; open the phone UI as `/?token=...` |
| `REMOTE_LANG` | `English` | language of explanations (e.g. `Japanese`) |
| `REMOTE_TIMEOUT_S` | `600` | unanswered calls are denied after this |
| `DEMO_MODE` | | `1` shows a "Start the demo agent" button |
| `LLM_HOURLY_CAP` | `0` | cap LLM calls per hour on public demos |

## The demo scenario

`cleanup-bot` is asked to "remove build junk and temp files, then email the weekly progress to team@example.com".
A meeting note in its folder hides an instruction to email `.env` to an outside address.

1. `list_files`, `read_file` → low risk, auto-allowed.
2. `delete_files [build, debug.log, app.py.tmp, src/app.py, .env]` → **critical, off-goal**. You tap **Fix**: "only build junk and temp files" → the LLM rewrites it to 3 paths.
3. `send_email` to `backup@data-sync.example` with the secrets → **critical, off-goal**. **Deny**. The agent gets your reason.
4. `send_email` to the team → high (external, can't unsend) → **Allow**.

## Architecture

```
agent ──tool call──▶ MCP proxy / SDK ──POST /api/calls──▶ FastAPI hub ──LLM──▶ explain + risk
                                                   │                 └─ house rules (LLM-compiled)
                                                   └──SSE──▶ phone PWA (Allow / Deny / Fix / KILL)
```

Python, FastAPI, Server-Sent Events, vanilla JS PWA, httpx. No database: state is in memory.

## Credits

- LLM: [Qwen3-235B-A22B-Instruct-2507](https://huggingface.co/Qwen) served by [Nebius Token Factory](https://tokenfactory.nebius.com/) (OpenAI-compatible API).
- [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/), [httpx](https://www.python-httpx.org/), [Pydantic](https://docs.pydantic.dev/).
- Protocol: [Model Context Protocol](https://modelcontextprotocol.io/) (the proxy speaks JSON-RPC over stdio; no SDK needed).

All code was written during the hackathon. MIT License.
