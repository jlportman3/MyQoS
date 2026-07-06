#!/usr/bin/env python3
"""libby_web.py — local Libby web backend (FastAPI), separate from lqos-api.

Serves the same-origin chat page and a real HTTP SSE endpoint whose wire format
is byte-for-byte what LibreQoS' shipped chatbot.js renderer (Xr/Zr) consumes:

    data: {"choices":[{"delta":{"reasoning":"..."}}]}\n
    data: {"choices":[{"delta":{"content":"Hello"}}]}\n
    data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n
    data: [DONE]\n

It reuses /opt/libreqos-mcp/libby.py (build_tools / call_tool) for the
read-only lqos-api tool layer, runs the qwen tool-call loop itself against the
local LiteLLM endpoint with stream=true, and — before the final answer — pulls
the top-4 doc chunks via libby_rag.retrieve() and prepends them to the system
context, instructing the model to cite the doc sources.

Read-only. Binds 0.0.0.0:9201. LITELLM_KEY comes from the environment and is
NEVER written to the wire, logs, or error bubbles.
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse

# --- reuse the existing agent plumbing from libby.py ------------------------
MCP_DIR = os.environ.get("LIBBY_MCP_DIR", "/opt/libreqos-mcp")
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)

import libby  # build_tools(), call_tool(), SYSTEM, MODEL, LITELLM_URL, http_json

try:
    import libby_rag  # retrieve(query, k) -> [{text, source, score}]
except Exception:  # pragma: no cover - RAG is optional / degrades gracefully
    libby_rag = None

# --- config -----------------------------------------------------------------
HTML_PATH = os.environ.get("LIBBY_HTML", os.path.join(MCP_DIR, "libby_local.html"))
LITELLM_URL = getattr(libby, "LITELLM_URL", "http://10.0.60.48:4000")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "")  # secret — never echoed
MODEL = getattr(libby, "MODEL", "qwen3.6-35b-a3b")
BASE_SYSTEM = getattr(libby, "SYSTEM", "You are Libby, a LibreQoS network-operations assistant.")
MAX_ROUNDS = int(os.environ.get("LIBBY_MAX_ROUNDS", "6"))
RAG_K = int(os.environ.get("LIBBY_RAG_K", "4"))

app = FastAPI(title="Libby (local)")

# Tools are discovered once from the read-only lqos-api OpenAPI spec.
try:
    _TOOLS, _ROUTES = libby.build_tools()
except Exception as e:  # keep serving even if lqos-api is momentarily down
    sys.stderr.write(f"[libby_web] build_tools failed at startup: {e}\n")
    _TOOLS, _ROUTES = [], {}


# --- SSE helpers ------------------------------------------------------------
def sse_chunk(delta: dict, finish_reason=None) -> str:
    """One `data: {chat.completions chunk}` line, newline-terminated."""
    choice = {"delta": delta, "finish_reason": finish_reason}
    return "data: " + json.dumps({"choices": [choice]}) + "\n"


def sse_content(text: str) -> str:
    return sse_chunk({"content": text})


def sse_reasoning(text: str) -> str:
    return sse_chunk({"reasoning": text})


def sse_stop() -> str:
    return sse_chunk({}, finish_reason="stop")


def sse_error(msg: str) -> str:
    """Renderer draws any line starting with `[error]` as a muted bubble."""
    return "[error] " + msg.replace("\n", " ") + "\n"


# --- RAG context ------------------------------------------------------------
def build_system_with_rag(question: str) -> str:
    """Prepend top-k doc chunks and a citation instruction to the base system."""
    if not libby_rag:
        return BASE_SYSTEM
    try:
        hits = libby_rag.retrieve(question, k=RAG_K) or []
    except Exception as e:
        sys.stderr.write(f"[libby_web] retrieve failed: {e}\n")
        hits = []
    if not hits:
        return BASE_SYSTEM
    blocks = []
    for i, h in enumerate(hits, 1):
        src = h.get("source", "unknown")
        txt = (h.get("text") or "").strip()
        blocks.append(f"[Doc {i}] source: {src}\n{txt}")
    docs = "\n\n".join(blocks)
    return (
        BASE_SYSTEM
        + "\n\nRelevant LibreQoS documentation excerpts are provided below. "
        "Use them when they help answer the question, and cite the source "
        "(the `source:` value) of any doc you rely on, e.g. (source: <name>).\n\n"
        "=== DOCUMENTATION CONTEXT ===\n" + docs + "\n=== END DOCUMENTATION ==="
    )


# --- LiteLLM streaming call -------------------------------------------------
def litellm_stream(messages, tools, tool_choice="auto"):
    """Yield parsed streaming chunks (dicts) from LiteLLM chat.completions.

    Uses stream=true so we can forward content/reasoning deltas live and
    accumulate any tool_call deltas for the tool loop. `tool_choice="none"`
    keeps the tool DEFINITIONS present (so a history containing tool_calls/tool
    messages stays valid) while forbidding new tool calls — used to force a
    final text answer.
    """
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.2,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
    headers = {"Content-Type": "application/json"}
    if LITELLM_KEY:
        headers["Authorization"] = "Bearer " + LITELLM_KEY
    req = urllib.request.Request(
        LITELLM_URL + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                yield json.loads(data)
            except Exception:
                continue


def _new_slot():
    return {"id": None, "type": "function", "function": {"name": "", "arguments": ""}}


def accumulate_tool_calls(store: dict, delta_tcs: list):
    """Merge streamed tool_call deltas into `store` (keyed by call ordinal).

    Standard OpenAI streaming gives each call a stable `index`; the first delta
    carries id+name, later deltas carry only argument fragments. qwen via this
    LiteLLM proxy often OMITS `index` or reuses 0 for several calls, which the
    naive merge collapsed into one slot (garbage name, "{}{...}" args -> 400).
    So: a delta bringing a fresh function name onto an already-named slot starts
    a NEW slot; arg-only continuation deltas append to the latest slot.
    """
    for tc in delta_tcs:
        fn = tc.get("function") or {}
        idx = tc.get("index")
        if idx is None:
            idx = max(store) if store else 0
        # Fresh name landing on a slot that already has a name => a second call
        # collapsed onto this index. Split it into a new slot.
        if fn.get("name") and store.get(idx, {}).get("function", {}).get("name"):
            idx = (max(store) + 1) if store else 0
        slot = store.setdefault(idx, _new_slot())
        if tc.get("id"):
            slot["id"] = tc["id"]
        if fn.get("name"):
            slot["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]


def _parse_args(raw):
    """Best-effort parse of a tool-call arguments string into a dict.

    Tolerates qwen's occasional concatenated/garbage output (e.g. '{}{...}')
    by taking the first valid JSON object. Returns {} on failure."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:
        pass
    try:
        v, _ = json.JSONDecoder().raw_decode(raw)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


_FALLBACK = (
    "I pulled the telemetry but couldn't compose a summary for that question. "
    "Try something more specific, e.g. \"top 5 circuits by RTT\" or "
    "\"is the shaper healthy right now?\"."
)


# --- the agent loop, as an SSE generator ------------------------------------
def run_agent_sse(question: str):
    """Generator producing the exact SSE text stream the renderer consumes."""
    question = (question or "").strip()
    if not question:
        yield sse_error("empty question")
        yield "data: [DONE]\n"
        return

    system = build_system_with_rag(question)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]

    try:
        for _ in range(MAX_ROUNDS):
            tool_store = {}
            content_buf = []
            round_finish = None

            for chunk in litellm_stream(messages, _TOOLS):
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                ch = choices[0]
                delta = ch.get("delta") or {}

                reasoning = delta.get("reasoning")
                if isinstance(reasoning, str) and reasoning:
                    yield sse_reasoning(reasoning)

                content = delta.get("content")
                if isinstance(content, str) and content:
                    content_buf.append(content)
                    yield sse_content(content)

                dtc = delta.get("tool_calls")
                if dtc:
                    accumulate_tool_calls(tool_store, dtc)

                if ch.get("finish_reason"):
                    round_finish = ch["finish_reason"]

            # Resolve streamed tool calls: keep only calls naming a REAL tool,
            # and normalize each arguments string to valid JSON — so we never
            # echo malformed data back to the backend (the "{}{...}" -> HTTP 400
            # failure mode). Unknown/garbage names are dropped.
            tool_calls = []
            for i in sorted(tool_store):
                fn = tool_store[i]["function"]
                name = (fn.get("name") or "").strip()
                if name not in _ROUTES:
                    if name:
                        sys.stderr.write(f"[libby_web] dropping unknown tool '{name[:60]}'\n")
                    continue
                fn["name"] = name
                fn["arguments"] = json.dumps(_parse_args(fn.get("arguments")))
                tool_calls.append(tool_store[i])

            if not tool_calls:
                if "".join(content_buf).strip():
                    # Model produced its final answer as text this round.
                    yield sse_stop()
                    yield "data: [DONE]\n"
                    return
                # No text AND no valid tools this round (e.g. it emitted only a
                # malformed empty tool call). Nudge and let the normal loop retry
                # with tools=auto — never tool_choice="none" (this backend 400s
                # on it) and never a tools-less call (returns empty).
                messages.append({
                    "role": "user",
                    "content": ("Please answer my question directly using the "
                                "data above, or call one of the available tools "
                                "by its exact name."),
                })
                continue

            # Otherwise: execute the requested (read-only) tools and loop.
            messages.append(
                {
                    "role": "assistant",
                    "content": "".join(content_buf) or None,
                    "tool_calls": tool_calls,
                }
            )
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except Exception:
                    args = {}
                sys.stderr.write(f"[libby_web] tool: {name}({args})\n")
                result = libby.call_tool(_ROUTES, name, args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "content": json.dumps(result)[:60000],
                    }
                )

        # Ran out of tool rounds without the model ever composing an answer.
        yield sse_content(_FALLBACK)
        yield sse_stop()
        yield "data: [DONE]\n"

    except urllib.error.HTTPError as e:
        # Log the backend's reason to stderr (never to the wire) for diagnosis.
        try:
            body = e.read().decode("utf-8", "replace")[:2000]
        except Exception:
            body = "(no body)"
        sys.stderr.write(f"[libby_web] backend HTTP {e.code}: {body}\n")
        # Never surface auth headers / key; give a bounded, safe message.
        yield sse_error(f"model backend error (HTTP {e.code})")
        yield "data: [DONE]\n"
    except Exception as e:
        yield sse_error(f"backend error: {type(e).__name__}: {e}")
        yield "data: [DONE]\n"


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # disable proxy buffering so tokens flush live
}


def _sse_response(question: str) -> StreamingResponse:
    return StreamingResponse(
        run_agent_sse(question),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


# --- routes -----------------------------------------------------------------
@app.get("/")
def index():
    try:
        with open(HTML_PATH, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse(
            "<h1>Libby (local)</h1><p>Chat page not found at "
            f"{HTML_PATH}.</p>",
            status_code=500,
        )


@app.get("/health")
def health():
    return JSONResponse(
        {
            "status": "ok",
            "model": MODEL,
            "tools": len(_TOOLS),
            "rag": bool(libby_rag),
        }
    )


@app.get("/chat")
def chat_get(q: str = "", question: str = ""):
    return _sse_response(question or q)


@app.post("/chat")
async def chat_post(request: Request):
    q = ""
    try:
        body = await request.json()
        if isinstance(body, dict):
            q = body.get("question") or body.get("text") or body.get("q") or body.get("message") or ""
        elif isinstance(body, str):
            q = body
    except Exception:
        raw = (await request.body()).decode("utf-8", "replace")
        q = raw
    return _sse_response(q)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("LIBBY_PORT", "9201")))
