"""Тесты загрузки сценариев и каталога."""
from pathlib import Path

import yaml

from benchmark.compare import load_scenarios
from benchmark.generate_catalog import build_catalog, count_turns

REPO = Path(__file__).resolve().parent.parent


def test_load_scenarios_recursive():
    scenarios = load_scenarios(REPO / "benchmark/scenarios")
    ids = {s["id"] for s in scenarios}
    assert "mcp_marker_basic" in ids or any("cat_t1" in i for i in ids)
    assert len(scenarios) >= 14


def test_load_scenarios_dedupe_by_id():
    scenarios = load_scenarios(REPO / "benchmark/scenarios")
    ids = [s["id"] for s in scenarios]
    assert len(ids) == len(set(ids))


def test_load_scenarios_tag_smoke():
    smoke = load_scenarios(REPO / "benchmark/scenarios", tag_filter=["smoke"])
    assert smoke
    for sc in smoke:
        assert "smoke" in (sc.get("tags") or [])


def test_catalog_turn_count():
    catalog = build_catalog(500)
    turns = count_turns(catalog)
    assert turns >= 500
    assert len(catalog) >= 100


def test_mcp_tool_registry_count():
    from benchmark.mcp_tool_registry import register_core_tools, register_hr_tools
    from mcp.server.fastmcp import FastMCP

    core = FastMCP("test-core")
    hr = FastMCP("test-hr")
    register_core_tools(core)
    register_hr_tools(hr)
    # FastMCP хранит tools во внутреннем реестре
    assert len(core._tool_manager._tools) >= 14
    assert len(hr._tool_manager._tools) >= 4
