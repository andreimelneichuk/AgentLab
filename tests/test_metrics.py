"""Unit-тесты агрегации метрик v1."""
from benchmark.metrics import (
    BackendRunMetrics,
    aggregate_backend_results,
    build_comparative_metrics,
    classify_failure,
    format_scorecard_line,
    token_reduction_pct,
    tool_error_reduction_pct,
)


def test_token_reduction():
    assert token_reduction_pct(1000, 800) == 20.0
    assert token_reduction_pct(0, 100) == 0.0


def test_tool_error_reduction():
    assert tool_error_reduction_pct(10, 5) == 50.0


def test_classify_failure():
    assert classify_failure("инструмент 'x' не вызван в этом ходе") == "tool_missing"
    assert classify_failure("R_NTA: выдуман факт") == "r_nta"
    assert classify_failure("MCP delta 1 > лимита 0") == "over_call"


def test_aggregate_backend_v1():
    results = [
        {
            "passed": True,
            "tags": ["r_nta", "r_dt", "critical"],
            "turns": [
                {
                    "turn_index": 0,
                    "passed": True,
                    "is_hallucination_nta": False,
                    "is_wrong_tool_dt": False,
                    "is_nta_measurable": True,
                    "is_tool_selection_measurable": True,
                    "tool_selection_ok": True,
                    "is_tool_args_measurable": True,
                    "tool_args_ok": True,
                    "is_abstention_measurable": True,
                    "abstention_ok": True,
                    "total_tokens": 100,
                    "latency_sec": 1.0,
                    "failures": [],
                },
                {
                    "turn_index": 1,
                    "passed": False,
                    "is_hallucination_nta": True,
                    "is_wrong_tool_dt": True,
                    "is_nta_measurable": True,
                    "is_tool_selection_measurable": True,
                    "tool_selection_ok": False,
                    "is_abstention_measurable": True,
                    "abstention_ok": False,
                    "total_tokens": 50,
                    "latency_sec": 0.5,
                    "failures": [
                        "инструмент 'flaky_tool' не вызван в этом ходе",
                        "MCP delta 1 > лимита 0",
                    ],
                },
            ],
        },
        {
            "passed": False,
            "tags": ["critical"],
            "turns": [{"turn_index": 0, "passed": False, "failures": ["regex не совпал"]}],
        },
    ]
    agg = aggregate_backend_results("original", results)
    assert agg.scenarios_passed == 1
    assert agg.scenarios_total == 2
    assert agg.critical_scenarios_passed == 1
    assert agg.critical_scenarios_total == 2
    assert agg.nta_turns == 2
    assert agg.nta_hallucinations == 1
    assert agg.anti_hallucination_pass == 0.5
    assert agg.tsa_turns == 2
    assert agg.tsa_ok == 1
    assert agg.tool_selection_accuracy == 0.5
    assert agg.abstention_turns == 2
    assert agg.abstention_ok == 1
    assert agg.tokens_per_solved == 150
    assert agg.failure_counts["tool_missing"] == 1
    assert agg.failure_counts["over_call"] == 1
    assert agg.failure_counts["regex"] == 1


def test_comparative():
    base = BackendRunMetrics("base", scenarios_total=2, scenarios_passed=2, total_tokens=1000, dt_errors=4)
    cand = BackendRunMetrics("cand", scenarios_total=2, scenarios_passed=1, total_tokens=800, dt_errors=2)
    comp = build_comparative_metrics(base, cand)
    assert comp["token_reduction_pct"] == 20.0
    assert comp["tool_error_reduction_pct"] == 50.0
    assert "solve_rate" in comp


def test_format_scorecard_line():
    m = BackendRunMetrics("x", scenarios_total=1, scenarios_passed=1).to_dict()
    line = format_scorecard_line(m)
    assert "solve=" in line
    assert "TPS=" in line
    assert "CAS=" in line


def test_domain_solve_rates_and_cas():
    results = [
        {
            "id": "s_tool",
            "passed": True,
            "tags": ["suite_tools", "tool"],
            "turns": [{"turn_index": 0, "passed": True, "total_tokens": 100, "latency_sec": 1.2}],
        },
        {
            "id": "s_safety",
            "passed": True,
            "tags": ["suite_safety", "r_nta"],
            "turns": [{"turn_index": 0, "passed": True, "is_nta_measurable": True, "is_hallucination_nta": False, "total_tokens": 80, "latency_sec": 0.8}],
        },
        {
            "id": "s_memory",
            "passed": True,
            "tags": ["suite_memory", "long_horizon"],
            "turns": [{"turn_index": 0, "passed": True, "total_tokens": 120, "latency_sec": 1.0}],
        },
        {
            "id": "s_graph",
            "passed": False,
            "tags": ["suite_graph", "graph"],
            "turns": [{"turn_index": 0, "passed": False, "total_tokens": 150, "latency_sec": 1.5, "failures": ["content"]}],
        },
    ]
    agg = aggregate_backend_results("test_bot", results)
    assert agg.domain_solve_rates["tools"] == 1.0
    assert agg.domain_solve_rates["safety"] == 1.0
    assert agg.domain_solve_rates["memory"] == 1.0
    assert agg.domain_solve_rates["graph"] == 0.0
    # CAS should be weighted: 30%*1.0 + 25%*1.0 + 25%*1.0 + 10%*0.0 + 10%*eff
    assert 80.0 <= agg.composite_agent_score <= 100.0
    d = agg.to_dict()
    assert "composite_agent_score" in d
    assert "domain_solve_rates" in d

