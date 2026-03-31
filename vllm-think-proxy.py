#!/usr/bin/env python3
"""
vLLM Think Proxy — sits between OpenClaw and SlotE.

Listens on PROXY_PORT, forwards to BACKEND_URL.
Detects /think prefix in the last user message:
  - /think  → strips prefix, injects chat_template_kwargs: {enable_thinking: true}
  - default → passes through as-is (server default: enable_thinking: false)

Usage:
    python3 vllm-think-proxy.py [--port 8017] [--backend http://localhost:8007]
"""

import argparse
import asyncio
import json
import logging
import sys

from aiohttp import web, ClientSession, ClientTimeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("think-proxy")

PROXY_PORT = 8017
BACKEND_URL = "http://localhost:8007"
THINK_PREFIX = "//"

# Map OpenClaw reasoning_effort levels to thinking token budgets
THINKING_BUDGETS = {
    "low":    1024,
    "medium": 4096,
    "high":   16384,
}


def detect_and_strip_think(messages: list) -> tuple[list, str | None]:
    """Check last user message for /think [level] prefix.
    Returns (messages, level) where level is 'low'/'medium'/'high' or None."""
    if not messages:
        return messages, None

    def _find_prefix_in_text(text: str):
        """Search all lines for THINK_PREFIX. Returns (new_text, level) or (None, None)."""
        lines = text.split("\n")
        for li, line in enumerate(lines):
            stripped = line.strip()
            if stripped.lower().startswith(THINK_PREFIX):
                rest = stripped[len(THINK_PREFIX):]
                level = "medium"
                first_word = rest.split()[0].lower() if rest.split() else ""
                if first_word in THINKING_BUDGETS:
                    level = first_word
                    rest = rest[len(first_word):].lstrip()
                else:
                    rest = rest.lstrip()
                new_lines = lines[:li] + ([rest] if rest else []) + lines[li+1:]
                return "\n".join(new_lines), level
        return None, None

    # Find last user message
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                new_text, level = _find_prefix_in_text(content)
                if level:
                    new_messages = messages[:i] + [{**msg, "content": new_text}] + messages[i+1:]
                    log.info(f"Thinking mode ENABLED via prefix (level={level})")
                    return new_messages, level
            elif isinstance(content, list):
                # Multimodal array — find last text part and check there
                for j in range(len(content) - 1, -1, -1):
                    part = content[j]
                    if isinstance(part, dict) and part.get("type") == "text":
                        new_text, level = _find_prefix_in_text(part.get("text", ""))
                        if level:
                            new_parts = content[:j] + [{**part, "text": new_text}] + content[j+1:]
                            new_messages = messages[:i] + [{**msg, "content": new_parts}] + messages[i+1:]
                            log.info(f"Thinking mode ENABLED via prefix in multimodal (level={level})")
                            return new_messages, level
                        break
            break

    return messages, None


async def proxy_request(request: web.Request, backend: str) -> web.Response:
    path = request.path
    query = request.query_string

    # Only intercept chat completions — pass everything else through unchanged
    if path != "/v1/chat/completions":
        url = f"{backend}{path}"
        if query:
            url += f"?{query}"
        async with ClientSession(timeout=ClientTimeout(total=300)) as session:
            async with session.request(
                method=request.method,
                url=url,
                headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")},
                data=await request.read(),
            ) as resp:
                body = await resp.read()
                return web.Response(
                    status=resp.status,
                    headers={k: v for k, v in resp.headers.items() if k.lower() not in ("transfer-encoding", "content-encoding")},
                    body=body,
                )

    # Parse request body
    try:
        body = await request.json()
    except Exception as e:
        return web.Response(status=400, text=f"Invalid JSON: {e}")

    # Debug
    msgs = body.get('messages', [])
    last = msgs[-1] if msgs else {}
    content = last.get('content', '')
    if isinstance(content, list):
        content = ' | '.join(t.get('text','')[:40] for t in content if isinstance(t, dict))
    log.info(f"effort={body.get('reasoning_effort')} last_role={last.get('role')} content={str(content)[:120]}")
    # Detect thinking: ONLY via // prefix — reasoning_effort alone does not enable thinking
    messages = body.get("messages", [])
    # Only check prefix on fresh user turns — skip if last message is tool/assistant (agentic loop)
    last_role = messages[-1].get("role") if messages else None
    if last_role == "user":
        new_messages, prefix_level = detect_and_strip_think(messages)
    else:
        new_messages, prefix_level = messages, None
    reasoning_effort = body.get("reasoning_effort")
    # Remove reasoning_effort — vLLM doesn't understand it
    body.pop("reasoning_effort", None)

    if prefix_level is not None:
        body["messages"] = new_messages
        kwargs = body.get("chat_template_kwargs", {})
        kwargs["enable_thinking"] = True
        body["chat_template_kwargs"] = kwargs
        log.info(f"Thinking ENABLED (prefix_level={prefix_level}, effort={reasoning_effort})")
    else:
        log.info(f"Thinking OFF (no prefix, effort={reasoning_effort})")

    is_stream = body.get("stream", False)
    url = f"{backend}{path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}

    async with ClientSession(timeout=ClientTimeout(total=300)) as session:
        async with session.post(url, json=body, headers=headers) as resp:
            if is_stream:
                # Stream SSE response back
                response = web.StreamResponse(
                    status=resp.status,
                    headers={k: v for k, v in resp.headers.items() if k.lower() not in ("transfer-encoding", "content-encoding")},
                )
                await response.prepare(request)
                async for chunk in resp.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
            else:
                body_out = await resp.read()
                return web.Response(
                    status=resp.status,
                    headers={k: v for k, v in resp.headers.items() if k.lower() not in ("transfer-encoding", "content-encoding")},
                    body=body_out,
                )


async def main():
    parser = argparse.ArgumentParser(description="vLLM Think Proxy")
    parser.add_argument("--port", type=int, default=PROXY_PORT)
    parser.add_argument("--backend", type=str, default=BACKEND_URL)
    args = parser.parse_args()

    backend = args.backend.rstrip("/")

    app = web.Application()
    app.router.add_route("*", "/{path_info:.*}", lambda r: proxy_request(r, backend))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", args.port)
    await site.start()

    log.info(f"Think proxy listening on port {args.port} → {backend}")
    log.info(f"Trigger thinking with '{THINK_PREFIX}[high|medium|low]' prefix in any user message")

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
