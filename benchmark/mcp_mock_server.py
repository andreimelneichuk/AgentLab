#!/usr/bin/env python3
"""
Mock MCP для бенчмарка: 4 сервера, ~28 инструментов, общий /stats.

Запуск:
  python -m benchmark.mcp_mock_server
"""
from __future__ import annotations

import contextlib
import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from benchmark.mcp_tool_registry import (
    register_core_tools,
    register_crm_tools,
    register_hr_tools,
    register_sse_tools,
    reset_stats,
    tool_calls,
    last_args,
    _lock,
)

HOST = "127.0.0.1"
HTTP_PORT = 19100

MCP_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)
logging.getLogger("mcp.server.streamable_http").setLevel(logging.CRITICAL)

streamable = FastMCP(
    "benchmark-core",
    host=HOST,
    stateless_http=True,
    json_response=True,
    transport_security=MCP_SECURITY,
)
hr_mcp = FastMCP(
    "benchmark-hr",
    host=HOST,
    stateless_http=True,
    json_response=True,
    transport_security=MCP_SECURITY,
)
crm_mcp = FastMCP(
    "benchmark-crm",
    host=HOST,
    stateless_http=True,
    json_response=True,
    transport_security=MCP_SECURITY,
)
sse = FastMCP("benchmark-sse", host=HOST, transport_security=MCP_SECURITY)

register_core_tools(streamable)
register_hr_tools(hr_mcp)
register_crm_tools(crm_mcp)
register_sse_tools(sse)


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    async with streamable.session_manager.run():
        async with hr_mcp.session_manager.run():
            async with crm_mcp.session_manager.run():
                yield


app = FastAPI(title="benchmark-mcp-mock", lifespan=_lifespan)
app.mount("/mcp-streamable", streamable.streamable_http_app())
app.mount("/mcp-hr", hr_mcp.streamable_http_app())
app.mount("/mcp-crm", crm_mcp.streamable_http_app())
app.mount("/mcp-sse", sse.sse_app())


@app.get("/stats")
async def stats():
    with _lock:
        return JSONResponse({
            "tool_calls": dict(tool_calls),
            "last_args": {k: v[-5:] for k, v in last_args.items()},
            "servers": ["core", "hr", "crm", "sse"],
            "tool_count": len(tool_calls),
        })


@app.post("/stats/reset")
async def reset_stats_endpoint():
    reset_stats()
    return {"status": "ok"}


def main() -> None:
    import uvicorn

    print(f"Benchmark MCP mock (4 servers) http://{HOST}:{HTTP_PORT}")
    print("  core  /mcp-streamable  (~14 tools)")
    print("  hr    /mcp-hr          (4 tools)")
    print("  crm   /mcp-crm         (4 tools)")
    print("  sse   /mcp-sse         (3 tools)")
    print(f"  stats http://{HOST}:{HTTP_PORT}/stats")
    uvicorn.run(app, host=HOST, port=HTTP_PORT, log_level="warning")


if __name__ == "__main__":
    main()
