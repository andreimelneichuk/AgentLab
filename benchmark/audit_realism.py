#!/usr/bin/env python3
"""Аудит реалистичности user-промптов в benchmark/scenarios/**/*.yaml.

Запуск: python -m benchmark.audit_realism
"""
from __future__ import annotations

import os
import sys

# При прямом запуске `python benchmark/audit_realism.py` каталог benchmark попадает
# в sys.path и перекрывает stdlib-модуль types.
_here = os.path.abspath(__file__)
if os.path.basename(os.path.dirname(_here)) == "benchmark":
    _root = os.path.dirname(os.path.dirname(_here))
    _bench = os.path.dirname(_here)
    sys.path[:] = [_root] + [p for p in sys.path if os.path.abspath(p) != _bench]

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

import yaml

SCENARIOS_ROOT = Path(__file__).resolve().parent / "scenarios"

TOOL_NAMES = [
    "benchmark_probe",
    "get_policy_fact",
    "python_doc_lookup",
    "decoy_python_lookup",
    "decoy_policy_fact",
    "random_marker_probe",
    "flaky_tool",
    "empty_search",
    "inventory_lookup",
    "decoy_inventory_lookup",
    "invoice_get",
    "weather_city",
    "translate_text",
    "calc_expression",
    "employee_lookup",
    "leave_balance",
    "org_chart_dept",
    "decoy_employee_search",
    "graph_query",
    "customer_get",
    "ticket_create",
    "sales_quote",
    "decoy_customer_lookup",
    "benchmark_sse_probe",
    "sse_audit_log",
    "decoy_sse_cache",
]

META_PATTERNS = [
    r"без\s+tool",
    r"вызови",
    r"\bMCP\b",
    r"из\s+памяти",
    r"не\s+выдумывай",
    r"дословно",
]

FORMAT_RULE_WORDS = ["DELTA", "BANANA", "MAPLE", "COCONUT"]

EXCLUDE_FILES = {
    "instruction.yaml",
    "long_horizon.yaml",
    "s09_instruction.yaml",
    "creative_b.yaml",
}

EXCLUDE_TAGS = {"instruction", "long_horizon", "creative_b"}

PENALTY_TOOL_NAME = 0.45
PENALTY_META = 0.35
PENALTY_FORMAT_RULE = 0.55
PENALTY_WRONG_CUSTOMER_ID = 0.30

THRESHOLD_LOW = 0.7


@dataclass
class PromptHit:
    file: Path
    line: int
    scenario_id: str
    text: str
    score: float
    category: str
    reasons: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


def _compile_patterns() -> tuple[list[re.Pattern[str]], list[re.Pattern[str]], re.Pattern[str]]:
    tool_patterns = [re.compile(re.escape(name), re.IGNORECASE) for name in TOOL_NAMES]
    meta_patterns = [re.compile(pat, re.IGNORECASE) for pat in META_PATTERNS]
    wrong_id_pattern = re.compile(r"\bCUS-(?!T)\d+", re.IGNORECASE)
    return tool_patterns, meta_patterns, wrong_id_pattern


def _find_line_for_text(lines: list[str], text: str, start_at: int = 0) -> int:
    """Находит номер строки, где начинается user-промпт."""
    snippet = text.strip().splitlines()[0][:60]
    for idx in range(start_at, len(lines)):
        if snippet and snippet in lines[idx]:
            return idx + 1
    for idx in range(start_at, len(lines)):
        if "user:" in lines[idx]:
            return idx + 1
    return start_at + 1


def score_prompt(text: str) -> tuple[float, str, list[str]]:
    """Возвращает (score 0..1, category, reasons)."""
    reasons: list[str] = []
    score = 1.0
    category = "business"

    tool_patterns, meta_patterns, wrong_id_pattern = _compile_patterns()

    tools_found = sorted({TOOL_NAMES[i] for i, pat in enumerate(tool_patterns) if pat.search(text)})
    for tool in tools_found:
        score -= PENALTY_TOOL_NAME
        reasons.append(f"tool_name:{tool}")

    for pat in meta_patterns:
        if pat.search(text):
            label = pat.pattern.replace("\\b", "").replace("\\s+", " ")
            score -= PENALTY_META
            reasons.append(f"meta:{label}")

    format_hits = [word for word in FORMAT_RULE_WORDS if re.search(rf"\b{word}\b", text)]
    if format_hits:
        category = "instruction_following"
        score -= PENALTY_FORMAT_RULE * len(format_hits)
        reasons.append(f"format_rule:{','.join(format_hits)}")

    if wrong_id_pattern.search(text):
        score -= PENALTY_WRONG_CUSTOMER_ID
        reasons.append("wrong_id:CUS-")

    score = max(0.0, min(1.0, score))
    return score, category, reasons


def _iter_user_prompts(scenario: dict[str, Any]) -> Iterator[tuple[str, str]]:
    setup = scenario.get("setup")
    if isinstance(setup, dict) and setup.get("user"):
        yield "setup", str(setup["user"]).strip()

    for turn in scenario.get("turns") or []:
        if isinstance(turn, dict) and turn.get("user"):
            yield "turn", str(turn["user"]).strip()


def _is_business_scenario(rel_file: str, tags: Iterable[str]) -> bool:
    if Path(rel_file).name in EXCLUDE_FILES:
        return False
    tag_set = {str(t) for t in tags}
    return not tag_set.intersection(EXCLUDE_TAGS)


def scan_scenarios(root: Path = SCENARIOS_ROOT) -> list[PromptHit]:
    hits: list[PromptHit] = []
    yaml_files = sorted(root.rglob("*.yaml"))

    for yaml_path in yaml_files:
        rel = yaml_path.relative_to(root).as_posix()
        raw = yaml_path.read_text(encoding="utf-8")
        lines = raw.splitlines()
        data = yaml.safe_load(raw) or {}
        scenarios = data.get("scenarios") or []

        line_cursor = 0
        for scenario in scenarios:
            if not isinstance(scenario, dict):
                continue
            scenario_id = str(scenario.get("id", "unknown"))
            tags = [str(t) for t in scenario.get("tags") or []]

            for _kind, prompt in _iter_user_prompts(scenario):
                score, category, reasons = score_prompt(prompt)
                line_no = _find_line_for_text(lines, prompt, start_at=line_cursor)
                line_cursor = max(0, line_no - 1)

                hits.append(
                    PromptHit(
                        file=yaml_path,
                        line=line_no,
                        scenario_id=scenario_id,
                        text=prompt,
                        score=score,
                        category=category,
                        reasons=reasons,
                        tags=tags,
                    )
                )
    return hits


def _avg(scores: list[float]) -> float:
    return sum(scores) / len(scores) if scores else 0.0


def build_report(hits: list[PromptHit], root: Path = SCENARIOS_ROOT) -> str:
    all_scores = [h.score for h in hits]
    business_hits = [
        h
        for h in hits
        if _is_business_scenario(h.file.relative_to(root).as_posix(), h.tags)
    ]
    business_scores = [h.score for h in business_hits]

    lines: list[str] = []
    lines.append("=== Realism audit ===")
    lines.append(f"Prompts scanned: {len(hits)}")
    lines.append(f"Overall average: {_avg(all_scores) * 100:.1f}%")
    lines.append(f"Business average (excl. instruction/long_horizon files & tags): {_avg(business_scores) * 100:.1f}%")
    lines.append("")

    per_file: dict[str, list[float]] = {}
    for h in hits:
        rel = h.file.relative_to(root).as_posix()
        per_file.setdefault(rel, []).append(h.score)

    lines.append("Per-file breakdown:")
    for rel in sorted(per_file):
        scores = per_file[rel]
        lines.append(f"  {rel}: {len(scores)} prompts, avg {_avg(scores) * 100:.1f}%")
    lines.append("")

    low = [h for h in hits if h.score < THRESHOLD_LOW]
    low_business = [h for h in low if h.category == "business"]
    low_instruction = [h for h in low if h.category == "instruction_following"]

    lines.append(f"Prompts below {THRESHOLD_LOW} ({len(low)}):")
    if low_business:
        lines.append("  Business (should be fixed):")
        for h in sorted(low_business, key=lambda x: (x.file.as_posix(), x.line)):
            rel = h.file.relative_to(root).as_posix()
            preview = h.text.replace("\n", " ")[:80]
            reason = ", ".join(h.reasons) if h.reasons else "—"
            lines.append(f"    {rel}:{h.line} score={h.score:.2f} [{reason}] {preview!r}")
    if low_instruction:
        lines.append("  Instruction-following (format rules — expected low realism):")
        for h in sorted(low_instruction, key=lambda x: (x.file.as_posix(), x.line)):
            rel = h.file.relative_to(root).as_posix()
            preview = h.text.replace("\n", " ")[:80]
            reason = ", ".join(h.reasons) if h.reasons else "—"
            lines.append(f"    {rel}:{h.line} score={h.score:.2f} [{reason}] {preview!r}")
    if not low:
        lines.append("  (none)")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit realism of benchmark user prompts")
    parser.add_argument(
        "--scenarios-dir",
        type=Path,
        default=SCENARIOS_ROOT,
        help="Path to benchmark/scenarios",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
    args = parser.parse_args(argv)

    hits = scan_scenarios(args.scenarios_dir)
    report = build_report(hits, args.scenarios_dir)
    print(report)

    if args.json:
        import json

        business = [
            h for h in hits if _is_business_scenario(h.file.relative_to(args.scenarios_dir).as_posix(), h.tags)
        ]
        payload = {
            "overall_avg": _avg([h.score for h in hits]),
            "business_avg": _avg([h.score for h in business]),
            "low_count": sum(1 for h in hits if h.score < THRESHOLD_LOW),
        }
        print(json.dumps(payload, ensure_ascii=False))

    business_avg = _avg(
        [
            h.score
            for h in hits
            if _is_business_scenario(h.file.relative_to(args.scenarios_dir).as_posix(), h.tags)
        ]
    )
    return 0 if business_avg >= 0.90 else 1


if __name__ == "__main__":
    sys.exit(main())
