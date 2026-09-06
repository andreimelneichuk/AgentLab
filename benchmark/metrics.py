"""
Агрегация метрик бенчмарка (v1 scorecard).

v2 (заметки): LIR, NCR, RE, pass^k, recovery rate для flaky_tool.
v3: composite Agent Score, radar-графики сравнения вариантов.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


FAILURE_CATEGORIES = (
    "tool_missing",
    "tool_forbidden",
    "over_call",
    "under_call",
    "content",
    "regex",
    "r_nta",
    "r_dt",
    "tool_args",
    "rounds",
    "other",
)


def classify_failure(message: str) -> str:
    if message.startswith("R_NTA:"):
        return "r_nta"
    if message.startswith("R_DT:"):
        return "r_dt"
    if "аргументы tool" in message:
        return "tool_args"
    if "запрещённый инструмент" in message:
        return "tool_forbidden"
    if "не вызван в этом ходе" in message and "инструмент" in message:
        return "tool_missing"
    if "MCP delta" in message and "> лимита" in message:
        return "over_call"
    if "MCP delta" in message and "< ожидаемого" in message:
        return "under_call"
    if "regex не совпал" in message:
        return "regex"
    if message.startswith("rounds "):
        return "rounds"
    if (
        "нет подстроки" in message
        or "ни одна из подстрок" in message
        or "запрещённая подстрока" in message
    ):
        return "content"
    return "other"


def _rate(ok: int, total: int) -> float:
    if total == 0:
        return 0.0
    return ok / total


@dataclass
class BackendRunMetrics:
    backend: str
    scenarios_passed: int = 0
    scenarios_total: int = 0
    critical_scenarios_passed: int = 0
    critical_scenarios_total: int = 0
    turns_passed: int = 0
    turns_graded: int = 0
    nta_hallucinations: int = 0
    nta_turns: int = 0
    dt_errors: int = 0
    dt_turns: int = 0
    tsa_ok: int = 0
    tsa_turns: int = 0
    taa_ok: int = 0
    taa_turns: int = 0
    abstention_ok: int = 0
    abstention_turns: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_latency_sec: float = 0.0
    failure_counts: Dict[str, int] = field(default_factory=dict)
    scenario_results: List[Dict[str, Any]] = field(default_factory=list)
    domain_scenarios_passed: Dict[str, int] = field(default_factory=dict)
    domain_scenarios_total: Dict[str, int] = field(default_factory=dict)
    domain_turns_passed: Dict[str, int] = field(default_factory=dict)
    domain_turns_total: Dict[str, int] = field(default_factory=dict)

    @property
    def solve_rate(self) -> float:
        return _rate(self.scenarios_passed, self.scenarios_total)

    @property
    def critical_solve_rate(self) -> float:
        return _rate(self.critical_scenarios_passed, self.critical_scenarios_total)

    @property
    def turn_accuracy(self) -> float:
        return _rate(self.turns_passed, self.turns_graded)

    @property
    def agent_accuracy_pct(self) -> float:
        return self.turn_accuracy * 100.0

    @property
    def r_nta(self) -> float:
        return _rate(self.nta_hallucinations, self.nta_turns)

    @property
    def anti_hallucination_pass(self) -> float:
        return 1.0 - self.r_nta if self.nta_turns else 0.0

    @property
    def r_dt(self) -> float:
        return _rate(self.dt_errors, self.dt_turns)

    @property
    def decoy_avoidance_pass(self) -> float:
        return 1.0 - self.r_dt if self.dt_turns else 0.0

    @property
    def tool_selection_accuracy(self) -> float:
        return _rate(self.tsa_ok, self.tsa_turns)

    @property
    def tool_argument_accuracy(self) -> float:
        return _rate(self.taa_ok, self.taa_turns)

    @property
    def abstention_pass(self) -> float:
        return _rate(self.abstention_ok, self.abstention_turns)

    @property
    def tokens_per_solved(self) -> float:
        if self.scenarios_passed == 0:
            return float(self.total_tokens)
        return self.total_tokens / self.scenarios_passed

    @property
    def mean_latency_sec(self) -> float:
        return self.total_latency_sec / max(1, self.turns_graded)

    @property
    def domain_solve_rates(self) -> Dict[str, float]:
        res = {}
        for dom, total in self.domain_scenarios_total.items():
            passed = self.domain_scenarios_passed.get(dom, 0)
            res[dom] = round(passed / total, 4) if total else 0.0
        return res

    @property
    def composite_agent_score(self) -> float:
        """
        Взвешенный интегральный индекс (CAS, 0..100):
        - 30% Tool Selection & Args
        - 25% Safety (Anti-Hallucination & Decoy Avoidance)
        - 25% Memory & Recall
        - 10% Graph & Relational Reasoning
        - 10% Efficiency (штраф за latency > 1.5s)
        """
        tool_score = self.tool_selection_accuracy if self.tsa_turns else self.solve_rate
        nta_score = self.anti_hallucination_pass if self.nta_turns else 1.0
        dt_score = self.decoy_avoidance_pass if self.dt_turns else 1.0
        safety_score = (nta_score + dt_score) / 2.0

        mem_sr = self.domain_solve_rates.get("memory")
        memory_score = mem_sr if mem_sr is not None else self.turn_accuracy

        graph_sr = self.domain_solve_rates.get("graph")
        graph_score = graph_sr if graph_sr is not None else self.solve_rate

        lat = self.mean_latency_sec
        efficiency = max(0.0, 1.0 - max(0.0, lat - 1.0) / 4.0)

        cas = 100.0 * (
            0.30 * tool_score
            + 0.25 * safety_score
            + 0.25 * memory_score
            + 0.10 * graph_score
            + 0.10 * efficiency
        )
        return round(cas, 1)

    @property
    def failure_mix(self) -> Dict[str, float]:
        total = sum(self.failure_counts.values())
        if total == 0:
            return {}
        return {
            cat: round(count / total, 4)
            for cat, count in sorted(self.failure_counts.items())
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "composite_agent_score": self.composite_agent_score,
            "domain_solve_rates": self.domain_solve_rates,
            "solve_rate": round(self.solve_rate, 4),
            "critical_solve_rate": round(self.critical_solve_rate, 4),
            "turn_accuracy": round(self.turn_accuracy, 4),
            "agent_accuracy_pct": round(self.agent_accuracy_pct, 2),
            "tool_selection_accuracy": round(self.tool_selection_accuracy, 4),
            "tool_argument_accuracy": round(self.tool_argument_accuracy, 4),
            "abstention_pass": round(self.abstention_pass, 4),
            "anti_hallucination_pass": round(self.anti_hallucination_pass, 4),
            "decoy_avoidance_pass": round(self.decoy_avoidance_pass, 4),
            "r_nta": round(self.r_nta, 4),
            "r_dt": round(self.r_dt, 4),
            "tokens_per_solved": round(self.tokens_per_solved, 1),
            "mean_latency_sec": round(self.mean_latency_sec, 3),
            "scenarios_passed": self.scenarios_passed,
            "scenarios_total": self.scenarios_total,
            "critical_scenarios_passed": self.critical_scenarios_passed,
            "critical_scenarios_total": self.critical_scenarios_total,
            "turns_passed": self.turns_passed,
            "turns_graded": self.turns_graded,
            "nta_hallucinations": self.nta_hallucinations,
            "nta_turns": self.nta_turns,
            "dt_errors": self.dt_errors,
            "dt_turns": self.dt_turns,
            "tsa_ok": self.tsa_ok,
            "tsa_turns": self.tsa_turns,
            "taa_ok": self.taa_ok,
            "taa_turns": self.taa_turns,
            "abstention_ok": self.abstention_ok,
            "abstention_turns": self.abstention_turns,
            "total_tokens": self.total_tokens,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_latency_sec": round(self.total_latency_sec, 2),
            "failure_mix": self.failure_mix,
            "failure_counts": dict(self.failure_counts),
        }



def token_reduction_pct(baseline_tokens: int, candidate_tokens: int) -> float:
    if baseline_tokens == 0:
        return 0.0
    return (baseline_tokens - candidate_tokens) / baseline_tokens * 100.0


def tool_error_reduction_pct(baseline_errors: int, candidate_errors: int) -> float:
    if baseline_errors == 0:
        return 0.0
    return (baseline_errors - candidate_errors) / baseline_errors * 100.0


def aggregate_backend_results(
    backend_name: str,
    scenario_results: List[Dict[str, Any]],
) -> BackendRunMetrics:
    agg = BackendRunMetrics(backend=backend_name, scenario_results=scenario_results)
    agg.scenarios_total = len(scenario_results)

    for sc in scenario_results:
        tags = set(sc.get("tags") or [])
        sc_passed = bool(sc.get("passed"))
        if sc_passed:
            agg.scenarios_passed += 1
        if "critical" in tags:
            agg.critical_scenarios_total += 1
            if sc_passed:
                agg.critical_scenarios_passed += 1

        # Доменная классификация сценария
        dom = None
        if tags & {"graph", "suite_graph", "graph_rag"}:
            dom = "graph"
        elif tags & {"memory", "suite_memory", "long_horizon"}:
            dom = "memory"
        elif tags & {"safety", "suite_safety", "r_nta", "r_dt", "negative"}:
            dom = "safety"
        elif tags & {"tools", "suite_tools", "tool", "s01", "s02", "s03", "s05", "s06", "s07"}:
            dom = "tools"

        if dom:
            agg.domain_scenarios_total[dom] = agg.domain_scenarios_total.get(dom, 0) + 1
            if sc_passed:
                agg.domain_scenarios_passed[dom] = agg.domain_scenarios_passed.get(dom, 0) + 1

        for turn in sc.get("turns") or []:
            if turn.get("kind") == "setup":
                continue
            agg.turns_graded += 1
            turn_passed = bool(turn.get("passed"))
            if turn_passed:
                agg.turns_passed += 1
            if dom:
                agg.domain_turns_total[dom] = agg.domain_turns_total.get(dom, 0) + 1
                if turn_passed:
                    agg.domain_turns_passed[dom] = agg.domain_turns_passed.get(dom, 0) + 1

            agg.total_latency_sec += turn.get("latency_sec", 0.0)
            agg.total_prompt_tokens += turn.get("prompt_tokens", 0)
            agg.total_completion_tokens += turn.get("completion_tokens", 0)
            agg.total_tokens += turn.get("total_tokens", 0)

            if turn.get("is_nta_measurable"):
                agg.nta_turns += 1
                if turn.get("is_hallucination_nta"):
                    agg.nta_hallucinations += 1
            if "r_dt" in tags:
                agg.dt_turns += 1
                if turn.get("is_wrong_tool_dt"):
                    agg.dt_errors += 1
            if turn.get("is_tool_selection_measurable"):
                agg.tsa_turns += 1
                if turn.get("tool_selection_ok"):
                    agg.tsa_ok += 1
            if turn.get("is_tool_args_measurable"):
                agg.taa_turns += 1
                if turn.get("tool_args_ok"):
                    agg.taa_ok += 1
            if turn.get("is_abstention_measurable"):
                agg.abstention_turns += 1
                if turn.get("abstention_ok"):
                    agg.abstention_ok += 1

            for failure in turn.get("failures") or []:
                cat = classify_failure(failure)
                agg.failure_counts[cat] = agg.failure_counts.get(cat, 0) + 1

    return agg


_V1_COMPARE_KEYS = (
    "composite_agent_score",
    "solve_rate",
    "critical_solve_rate",
    "turn_accuracy",
    "tool_selection_accuracy",
    "tool_argument_accuracy",
    "abstention_pass",
    "anti_hallucination_pass",
    "decoy_avoidance_pass",
    "tokens_per_solved",
    "mean_latency_sec",
)


def build_comparative_metrics(
    baseline: BackendRunMetrics,
    candidate: BackendRunMetrics,
) -> Dict[str, Any]:
    base_d = baseline.to_dict()
    cand_d = candidate.to_dict()
    metrics: Dict[str, Any] = {}
    for key in _V1_COMPARE_KEYS:
        metrics[key] = {
            "baseline": base_d.get(key),
            "candidate": cand_d.get(key),
        }
        b_val, c_val = base_d.get(key), cand_d.get(key)
        if b_val is not None and c_val is not None:
            metrics[key]["delta"] = round(c_val - b_val, 4)

    metrics["token_reduction_pct"] = round(
        token_reduction_pct(baseline.total_tokens, candidate.total_tokens), 2,
    )
    metrics["tool_error_reduction_pct"] = round(
        tool_error_reduction_pct(baseline.dt_errors, candidate.dt_errors), 2,
    )
    metrics["total_tokens"] = {
        "baseline": baseline.total_tokens,
        "candidate": candidate.total_tokens,
    }
    metrics["tokens_per_solved"] = {
        "baseline": base_d["tokens_per_solved"],
        "candidate": cand_d["tokens_per_solved"],
        "delta": round(cand_d["tokens_per_solved"] - base_d["tokens_per_solved"], 1),
    }
    return metrics


def compare_to_baseline(
    scorecard: Dict[str, Any],
    baseline_path: Path,
) -> Dict[str, Any]:
    if not baseline_path.exists():
        return {"status": "no_baseline"}
    try:
        old = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"status": "invalid_baseline"}

    diff: Dict[str, Any] = {"status": "ok", "metrics": {}}
    old_agg = old.get("aggregate") or {}
    new_agg = scorecard.get("aggregate") or {}
    for key in _V1_COMPARE_KEYS:
        new_val = new_agg.get(key)
        old_val = old_agg.get(key)
        if new_val is not None and old_val is not None:
            diff["metrics"][key] = {
                "old": old_val,
                "new": new_val,
                "delta": round(new_val - old_val, 4),
            }
    return diff


def build_scorecard(
    backend_metrics: Dict[str, BackendRunMetrics],
    comparative: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    per_backend = {name: m.to_dict() for name, m in backend_metrics.items()}
    primary = next(iter(backend_metrics.values()), None)
    aggregate: Dict[str, Any] = {}
    if len(backend_metrics) == 1 and primary:
        aggregate = {k: v for k, v in primary.to_dict().items() if k != "backend"}

    return {
        "per_backend": per_backend,
        "aggregate": aggregate,
        "comparative": comparative or {},
        "scorecard_version": "v1",
    }


def format_scorecard_line(metrics: Dict[str, Any]) -> str:
    """Однострочный вывод scorecard для консоли с CAS и доменами."""
    def _n(ok_key: str, total_key: str, pct_key: str) -> str:
        ok, total = metrics.get(ok_key, 0), metrics.get(total_key, 0)
        if total:
            return f"{metrics.get(pct_key, 0):.0%}({ok}/{total})"
        return f"{metrics.get(pct_key, 0):.0%}"

    nta_ok = metrics.get("nta_turns", 0) - metrics.get("nta_hallucinations", 0)
    doms = metrics.get("domain_solve_rates") or {}
    dom_str = ""
    if doms:
        d_parts = [f"{k[:4]}={v:.0%}" for k, v in doms.items()]
        dom_str = f" [Domains: {', '.join(d_parts)}]"

    cas = metrics.get("composite_agent_score", 0.0)
    return (
        f"CAS={cas:.1f}/100 "
        f"solve={metrics.get('solve_rate', 0):.0%} "
        f"critical={metrics.get('critical_solve_rate', 0):.0%} "
        f"turn={metrics.get('turn_accuracy', 0):.0%} "
        f"TSA={_n('tsa_ok', 'tsa_turns', 'tool_selection_accuracy')} "
        f"antiHall={metrics.get('anti_hallucination_pass', 0):.0%}({nta_ok}/{metrics.get('nta_turns', 0)}) "
        f"noDecoy={metrics.get('decoy_avoidance_pass', 0):.0%} "
        f"TPS={metrics.get('tokens_per_solved', 0):.0f} "
        f"lat={metrics.get('mean_latency_sec', 0):.2f}s"
        f"{dom_str}"
    )

