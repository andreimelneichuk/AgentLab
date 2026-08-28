"""Unit-тесты нормализации MCP конфига."""
from agent_core import _normalize_global_server, build_mcp_client_config


def test_normalize_http_adds_mcp_path():
    cfg = _normalize_global_server("t", {"type": "http", "url": "http://localhost:19100/mcp-streamable"})
    assert cfg is not None
    assert cfg["transport"] == "streamable_http"
    assert cfg["url"].endswith("/mcp")


def test_build_mcp_empty():
    assert build_mcp_client_config({}) == {}


def test_build_mcp_from_config():
    config = {
        "mcp_servers": {
            "bench": {"type": "http", "url": "http://127.0.0.1:19100/mcp-streamable"},
        }
    }
    servers = build_mcp_client_config(config)
    assert "bench" in servers
    assert servers["bench"]["transport"] == "streamable_http"
