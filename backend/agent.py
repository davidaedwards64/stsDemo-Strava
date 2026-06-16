"""Agentic loop: per-request Strava MCP session → Claude → SSE stream."""

import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import anthropic
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from backend.auth.okta_sts import exchange_id_token_for_strava_token

logger = logging.getLogger(__name__)

MCP_URL = "https://mcp.strava.com/mcp"
MODEL = "claude-opus-4-7"
MAX_ITERATIONS = 10


def _sse(event: str, data: Any) -> str:
    payload = json.dumps(data) if not isinstance(data, str) else data
    return f"event: {event}\ndata: {payload}\n\n"


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        leaves = []
        for e in exc.exceptions:
            leaves.extend(_leaf_exceptions(e))
        return leaves
    return [exc]


def _format_error(exc: BaseException) -> str:
    leaves = _leaf_exceptions(exc)
    parts = []
    for e in leaves:
        if isinstance(e, anthropic.APIStatusError):
            parts.append(f"Claude API error: {e.message}")
        else:
            parts.append(f"{type(e).__name__}: {e}")
    return "; ".join(parts)


async def run_agent(
    user_message: str,
    user_id_token: str | None = None,
    cache_key: str | None = None,
    history: list[dict] | None = None,
) -> AsyncIterator[str]:
    try:
        # Resolve Strava token via Okta STS exchange
        if user_id_token:
            yield _sse("status", {"text": "Authenticating with Strava..."})
            sts_result = await exchange_id_token_for_strava_token(
                user_id_token, cache_key=cache_key
            )
            if sts_result["status"] == "interaction_required":
                yield _sse("interaction_required", {"uri": sts_result.get("interaction_uri", "")})
                return
            if sts_result["status"] != "success":
                yield _sse("error", {"text": f"Token exchange failed: {sts_result.get('error', sts_result['status'])}"})
                return
            token = sts_result["access_token"]
            yield _sse("token_meta", {
                "step": "sts",
                "expires_in": sts_result.get("expires_in", 3600),
                "cached": sts_result.get("cached", False),
                "token_prefix": (token[:14] + "...") if token else "",
            })
        else:
            token = os.environ.get("STRAVA_MCP_TOKEN", "")
            if not token:
                yield _sse("error", {"text": "No Strava token available. Please sign in."})
                return

        mcp_headers = {"Authorization": f"Bearer {token}"}

        yield _sse("status", {"text": "Connecting to Strava MCP..."})

        async with streamablehttp_client(MCP_URL, headers=mcp_headers) as (read, write, _):
            async with ClientSession(read, write) as mcp:
                await mcp.initialize()

                tools_result = await mcp.list_tools()
                claude_tools = [
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "input_schema": t.inputSchema,
                    }
                    for t in tools_result.tools
                ]

                yield _sse("status", {"text": f"Ready ({len(claude_tools)} tools available)"})
                yield _sse("token_meta", {
                    "step": "mcp",
                    "tool_count": len(claude_tools),
                    "tools": [t["name"] for t in claude_tools],
                })

                system_prompt = (
                    "You are a personal training assistant with access to the user's Strava data. "
                    "Help them understand their training performance, activity history, segments, and progress.\n"
                    "Rules:\n"
                    "- Be concise and data-driven.\n"
                    "- Use tools to discover available data before answering questions about it.\n"
                    "- Never ask the user for IDs or details you can discover with tools.\n"
                    "- If a tool call returns an error, report it fully — do not ask for more information to retry.\n"
                    "- When presenting activity data, include key metrics: distance, time, pace/speed, elevation, heart rate where available."
                )

                client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
                messages: list[dict] = list(history or []) + [{"role": "user", "content": user_message}]

                for _ in range(MAX_ITERATIONS):
                    async with client.messages.stream(
                        model=MODEL,
                        max_tokens=4096,
                        system=system_prompt,
                        tools=claude_tools,
                        messages=messages,
                    ) as stream:
                        async for text in stream.text_stream:
                            yield _sse("text", {"text": text})

                        final = await stream.get_final_message()

                    messages.append({"role": "assistant", "content": final.content})

                    if final.stop_reason != "tool_use":
                        break

                    tool_results = []
                    for block in final.content:
                        if block.type != "tool_use":
                            continue
                        yield _sse("tool", {"name": block.name, "input": block.input})
                        try:
                            result = await mcp.call_tool(block.name, block.input)
                            result_text = "\n".join(
                                getattr(c, "text", str(c)) for c in result.content
                            )
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_text,
                            })
                        except Exception as exc:
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "is_error": True,
                                "content": str(exc),
                            })

                    messages.append({"role": "user", "content": tool_results})

        yield _sse("done", {})

    except BaseException as exc:
        import traceback
        traceback.print_exc()
        yield _sse("error", {"text": _format_error(exc)})
