#!/usr/bin/env python3
"""
Бенчмарк агента: сценарии, метрики R_NTA/R_DT/Solve rate.

Перед запуском (из корня experiments/):
  python -m benchmark.mcp_mock_server

Примеры:
  python -m benchmark.compare --variant original
  python -m benchmark.compare --variant original --variant my-tweak --metrics comparative
  python -m benchmark.compare --variant original --tags critical
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import httpx
import yaml

from benchmark.agent_loader import load_agent_module
from benchmark.basic_http import BasicHttpSession
from benchmark.metrics import (
    aggregate_backend_results,
    build_comparative_metrics,
    build_scorecard,
    compare_to_baseline,
    format_scorecard_line,
)
from benchmark.report import write_markdown_report
from benchmark.judge import TurnJudge
from benchmark.scoring import check_turn, compare_answers, turn_expectation_from_raw
from benchmark.webhook_mock import start_webhook_server

logger = logging.getLogger("benchmark.compare")

REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Бенчмарк агента Basic")
    p.add_argument(
        "--variant",
        action="append",
        default=[],
        metavar="DIR",
        help="Папка варианта с agent_core (можно несколько). По умолчанию: original",
    )
    p.add_argument("-c", "--config", type=Path, default=REPO_ROOT / "config.benchmark.yml")
    p.add_argument("-s", "--scenarios", type=Path, default=REPO_ROOT / "benchmark/scenarios")
    p.add_argument("--backends", default="", help="basic_loop,basic_http (внутри одного variant)")
    p.add_argument("--baseline", help="Backend для A/B (baseline)")
    p.add_argument("--candidate", help="Backend для A/B (candidate)")
    p.add_argument("--metrics", choices=["standard", "comparative"], default="standard")
    p.add_argument("--tags", default="", help="Фильтр сценариев по тегам (через запятую)")
    p.add_argument("--limit", type=int, default=0, help="Макс. число сценариев (0 = все)")
    p.add_argument(
        "--prompt",
        type=Path,
        default=None,
        help="Промпт (по умолчанию: <variant>/prompts/system_master.txt)",
    )
    p.add_argument("-m", "--model", help="Фиксированный model_alias")
    p.add_argument("-o", "--output", type=Path, help="JSON-отчёт")
    p.add_argument("--mcp-stats-url", default="http://127.0.0.1:19100/stats")
    p.add_argument("--reset-mcp-stats", action="store_true", default=True)
    p.add_argument("--min-similarity", type=float, default=0.35)
    p.add_argument(
        "--no-judge",
        action="store_true",
        help="Отключить LLM-судью для мягких FAIL checker'а",
    )
    p.add_argument("--save-baseline", action="store_true", help="Сохранить scorecard в baselines/latest.json")
    p.add_argument(
        "--baseline-path",
        type=Path,
        default=REPO_ROOT / "benchmark/baselines/latest.json",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def resolve_variants(args: argparse.Namespace) -> List[Path]:
    raw = args.variant or ["original"]
    return [Path(v) for v in raw]


def resolve_run_targets(
    args: argparse.Namespace,
    variants: List[Path],
) -> List[tuple[str, Path, str]]:
    """
    Список (label, variant_dir, session_kind).
    Несколько --variant → сравнение папок (label = имя папки).
    Один variant + --backends → сравнение backend внутри варианта.
    """
    if len(variants) >= 2:
        return [(v.name, v.resolve(), "basic_loop") for v in variants]

    variant = variants[0].resolve()
    if args.baseline and args.candidate:
        return [
            (args.baseline, variant, args.baseline),
            (args.candidate, variant, args.candidate),
        ]

    backends = (
        [b.strip() for b in args.backends.split(",") if b.strip()]
        or ["basic_loop"]
    )
    return [(name, variant, name) for name in backends]


def load_scenarios(path: Path, tag_filter: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    scenarios: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    if path.is_dir():
        files = sorted(path.rglob("*.yaml")) + sorted(path.rglob("*.yml"))
    else:
        files = [path]

    for fp in files:
        data = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        for sc in data.get("scenarios") or []:
            sid = sc.get("id")
            if sid and sid in seen_ids:
                continue
            if sid:
                seen_ids.add(sid)
            scenarios.append(sc)

    if tag_filter:
        scenarios = [
            sc for sc in scenarios
            if any(t in (sc.get("tags") or []) for t in tag_filter)
        ]

    return scenarios


async def fetch_mcp_stats(url: str) -> Dict[str, int]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        return dict(r.json().get("tool_calls") or {})


async def reset_mcp_stats(url: str) -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(url.replace("/stats", "/stats/reset"))


def mcp_delta(before: Dict[str, int], after: Dict[str, int], tool_name: Optional[str]) -> int:
    if tool_name:
        return after.get(tool_name, 0) - before.get(tool_name, 0)
    return sum(after.values()) - sum(before.values())


async def invoke_run_turn(session: Any, user_message: str, scenario_tags: List[str]) -> Any:
    """Вызов run_turn с опциональными kwargs (scenario_tags и др.) для variant-агентов."""
    kwargs: Dict[str, Any] = {}
    try:
        params = inspect.signature(session.run_turn).parameters
        if "scenario_tags" in params:
            kwargs["scenario_tags"] = scenario_tags
    except (TypeError, ValueError):
        pass
    return await session.run_turn(user_message, **kwargs)


def make_session(
    session_kind: str,
    agent_mod: Any,
    resources: Any,
    config: Dict[str, Any],
    model_alias: Optional[str],
) -> Union[Any, BasicHttpSession]:
    if session_kind == "basic_http":
        gb_cfg = config.get("basic_http") or {}
        return BasicHttpSession(
            base_url=gb_cfg.get("base_url", "http://127.0.0.1:9094"),
            webhook_url=gb_cfg.get("webhook_url", "http://127.0.0.1:19110"),
            configuration_id=gb_cfg.get("configuration_id", "DEFAULT_AI_ASSISTANT"),
            api_key=gb_cfg.get("api_key"),
            message_timeout_s=float(gb_cfg.get("message_timeout_s", 120)),
        )
    if session_kind == "basic_loop":
        return agent_mod.BasicLoopSession(resources, model_alias=model_alias)
    if session_kind == "basic_orchestrator":
        session_cls = getattr(agent_mod, "BasicOrchestratorSession", None)
        if session_cls is None:
            raise ValueError(
                f"Вариант {agent_mod.__file__} не экспортирует BasicOrchestratorSession"
            )
        return session_cls(resources, model_alias=model_alias)
    raise ValueError(f"Неизвестный session_kind: {session_kind!r}")


async def run_scenario_backend(
    backend_name: str,
    session: Any,
    resources: Any,
    scenario: Dict[str, Any],
    mcp_stats_url: str,
    *,
    judge: Optional[TurnJudge] = None,
) -> Dict[str, Any]:
    if hasattr(session, "reset"):
        session.reset()

    tags = list(scenario.get("tags") or [])
    known_tools = [t.name for t in resources.tools]
    turn_results: List[Dict[str, Any]] = []

    setup = scenario.get("setup") or {}
    if setup.get("user"):
        before = await fetch_mcp_stats(mcp_stats_url)
        result = await invoke_run_turn(session, setup["user"], tags)
        after = await fetch_mcp_stats(mcp_stats_url)
        turn_results.append({
            "kind": "setup",
            "user": setup["user"],
            "answer": result.answer,
            "tool_calls": result.tool_calls,
            "mcp_delta": mcp_delta(before, after, None),
            "latency_sec": result.latency_sec,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
        })

    for idx, raw_turn in enumerate(scenario.get("turns") or []):
        if raw_turn.get("new_session") and hasattr(session, "reset"):
            session.reset()
        exp = turn_expectation_from_raw(raw_turn)
        before = await fetch_mcp_stats(mcp_stats_url)
        result = await invoke_run_turn(session, exp.user, tags)
        after = await fetch_mcp_stats(mcp_stats_url)
        delta = mcp_delta(before, after, exp.expect_tool_called)
        check = check_turn(
            idx, exp, result.answer, result.tool_calls, delta,
            rounds=result.rounds,
            scenario_tags=tags,
            known_tools=known_tools,
            tool_call_details=result.tool_call_details,
        )
        checker_passed = check.passed
        judge_verdict = None
        final_passed = checker_passed
        if not checker_passed and judge is not None:
            judge_verdict = await judge.review_turn(
                scenario_id=scenario["id"],
                turn_index=idx,
                exp=exp,
                answer=result.answer,
                tools_called=result.tool_calls,
                tool_delta=delta,
                check=check,
            )
            if judge_verdict.eligible and judge_verdict.passed:
                final_passed = True
        turn_results.append({
            "turn_index": idx,
            "user": exp.user,
            "answer": result.answer,
            "tool_calls": result.tool_calls,
            "tool_call_details": result.tool_call_details,
            "rounds": result.rounds,
            "mcp_delta": delta,
            "checker_passed": checker_passed,
            "passed": final_passed,
            "failures": check.failures if not final_passed else [],
            "judge_eligible": judge_verdict.eligible if judge_verdict else False,
            "judge_passed": judge_verdict.passed if judge_verdict and judge_verdict.eligible else None,
            "judge_reason": judge_verdict.reason if judge_verdict and judge_verdict.eligible else None,
            "is_hallucination_nta": check.is_hallucination_nta,
            "is_wrong_tool_dt": check.is_wrong_tool_dt,
            "is_nta_measurable": check.is_nta_measurable,
            "is_tool_selection_measurable": check.is_tool_selection_measurable,
            "tool_selection_ok": check.tool_selection_ok,
            "is_tool_args_measurable": check.is_tool_args_measurable,
            "tool_args_ok": check.tool_args_ok,
            "is_abstention_measurable": check.is_abstention_measurable,
            "abstention_ok": check.abstention_ok,
            "history_length": len(session.history_dicts),
            "latency_sec": result.latency_sec,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
        })

    passed = all(t.get("passed", True) for t in turn_results if t.get("kind") != "setup")
    return {
        "backend": backend_name,
        "passed": passed,
        "tags": tags,
        "turns": turn_results,
    }


def resolve_prompt_path(args: argparse.Namespace, variant_dir: Path) -> Path:
    if args.prompt:
        return args.prompt
    return variant_dir / "prompts" / "system_master.txt"


def resolve_mcp_config_path(scenario: Dict[str, Any]) -> Optional[Path]:
    scenario_mcp = scenario.get("mcp_config")
    if not scenario_mcp:
        return None
    path = Path(scenario_mcp)
    if path.is_absolute():
        return path
    return REPO_ROOT / path


async def run_compare(args: argparse.Namespace) -> int:
    if not args.config.exists():
        logger.error("Конфиг не найден: %s", args.config)
        return 1

    variants = resolve_variants(args)
    for v in variants:
        if not v.exists():
            logger.error("Вариант не найден: %s", v)
            return 1

    targets = resolve_run_targets(args, variants)
    allowed_session = {"basic_loop", "basic_http", "basic_orchestrator"}
    for _label, _variant, kind in targets:
        if kind not in allowed_session:
            logger.error("Неизвестный backend/session: %s", kind)
            return 1

    base_config = load_config_yaml(args.config)
    tag_filter = [t.strip() for t in args.tags.split(",") if t.strip()] or None
    scenarios = load_scenarios(args.scenarios, tag_filter)
    if args.limit and args.limit > 0:
        scenarios = scenarios[: args.limit]

    if "basic_http" in {kind for _, _, kind in targets}:
        wh = (base_config.get("basic_http") or {}).get("webhook_port", 19110)
        start_webhook_server(port=int(wh))

    if args.reset_mcp_stats:
        try:
            await reset_mcp_stats(args.mcp_stats_url)
        except Exception as exc:
            logger.warning("MCP stats reset failed: %s", exc)

    if not scenarios:
        logger.error("Нет сценариев (проверь путь и --tags)")
        return 1

    backend_labels = [label for label, _, _ in targets]
    report: Dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "config": str(args.config),
        "variants": [str(v) for v in variants],
        "scenarios_path": str(args.scenarios),
        "backends": backend_labels,
        "tag_filter": tag_filter,
        "scenarios": [],
    }

    model_alias = args.model or (base_config.get("llm_defaults") or {}).get("model_alias")
    judge: Optional[TurnJudge] = None
    judge_cfg = (base_config.get("benchmark") or {}).get("judge") or {}
    if not args.no_judge and judge_cfg.get("enabled", True) and model_alias:
        try:
            judge = await TurnJudge.create(base_config, model_alias, judge_cfg)
            print("LLM-судья включён (мягкие FAIL checker → judge)", flush=True)
        except Exception as exc:
            logger.warning("Judge init failed, continuing without judge: %s", exc)

    backend_scenario_results: Dict[str, List[Dict[str, Any]]] = {b: [] for b in backend_labels}

    for scenario in scenarios:
        sid = scenario["id"]
        print(f"\n=== {sid}: {scenario.get('description', '')} ===", flush=True)

        mcp_file = resolve_mcp_config_path(scenario)
        cfg = deep_merge_config(base_config, scenario.get("config_override") or {})
        backend_results: Dict[str, Any] = {}
        for label, variant_dir, session_kind in targets:
            agent_mod = load_agent_module(variant_dir)
            prompt_path = resolve_prompt_path(args, variant_dir)

            resources = await agent_mod.AgentResources.create(
                cfg, mcp_file=mcp_file, prompt_path=prompt_path,
            )

            if args.reset_mcp_stats:
                try:
                    await reset_mcp_stats(args.mcp_stats_url)
                except Exception:
                    pass

            t0 = time.monotonic()
            try:
                session = make_session(session_kind, agent_mod, resources, cfg, args.model)
                result = await run_scenario_backend(
                    label, session, resources, scenario, args.mcp_stats_url, judge=judge,
                )
                result["variant"] = str(variant_dir)
                result["duration_sec"] = round(time.monotonic() - t0, 2)
                status = "PASS" if result["passed"] else "FAIL"
                print(f"  [{label}] {status} ({result['duration_sec']}s)", flush=True)
                for turn in result["turns"]:
                    if turn.get("kind") == "setup":
                        continue
                    mark = "ok" if turn.get("passed") else "FAIL"
                    print(f"    turn {turn['turn_index']}: {mark}", flush=True)
                    for f in turn.get("failures") or []:
                        print(f"      - {f}", flush=True)
                    if turn.get("judge_eligible"):
                        jp = turn.get("judge_passed")
                        mark_j = "PASS" if jp else "FAIL"
                        print(
                            f"      ~ судья: {mark_j} — {turn.get('judge_reason', '')}",
                            flush=True,
                        )
            except Exception as exc:
                result = {
                    "backend": label,
                    "variant": str(variant_dir),
                    "passed": False,
                    "error": str(exc),
                    "tags": scenario.get("tags") or [],
                    "turns": [],
                }
                print(f"  [{label}] ERROR: {exc}", flush=True)

            backend_results[label] = result
            backend_scenario_results[label].append({
                "id": sid,
                "passed": result.get("passed", False),
                "tags": scenario.get("tags") or [],
                "turns": result.get("turns") or [],
            })

        pairwise: List[Dict[str, Any]] = []
        if len(backend_labels) >= 2:
            a_name, b_name = backend_labels[0], backend_labels[1]
            a_turns = [t for t in backend_results[a_name].get("turns", []) if t.get("kind") != "setup"]
            b_turns = [t for t in backend_results[b_name].get("turns", []) if t.get("kind") != "setup"]
            for i, (ta, tb) in enumerate(zip(a_turns, b_turns)):
                cmp = compare_answers(ta.get("answer", ""), tb.get("answer", ""), args.min_similarity)
                cmp["turn_index"] = i
                pairwise.append(cmp)
                mark = "≈" if cmp["passed"] else "≠"
                print(f"  compare {a_name} vs {b_name} turn {i}: {mark} sim={cmp['similarity']}", flush=True)

        all_passed = all(backend_results[b].get("passed") for b in backend_labels)
        report["scenarios"].append({
            "id": sid,
            "description": scenario.get("description"),
            "tags": scenario.get("tags") or [],
            "passed": all_passed,
            "backends": backend_results,
            "pairwise_similarity": pairwise,
        })

    backend_metrics = {
        name: aggregate_backend_results(name, results)
        for name, results in backend_scenario_results.items()
    }

    comparative = None
    if args.metrics == "comparative" and len(backend_labels) >= 2:
        comparative = build_comparative_metrics(
            backend_metrics[backend_labels[0]],
            backend_metrics[backend_labels[1]],
        )

    scorecard = build_scorecard(backend_metrics, comparative)
    baseline_diff = compare_to_baseline(scorecard, args.baseline_path)

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["all_passed"] = all(s["passed"] for s in report["scenarios"])
    report["scorecard"] = scorecard
    report["baseline_diff"] = baseline_diff

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    variant_suffix = "_".join(backend_labels) if len(backend_labels) <= 2 else "multi"
    out = args.output or REPO_ROOT / "benchmark/results" / f"report_{variant_suffix}_{ts}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path = out.with_suffix(".md")
    write_markdown_report(report, scorecard, md_path, baseline_diff=baseline_diff)

    if args.save_baseline:
        args.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        args.baseline_path.write_text(json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8")

    sc = scorecard.get("per_backend") or {}
    print("\n## Scorecard (v1)", flush=True)
    for name, m in sc.items():
        print(f"  {name}: {format_scorecard_line(m)}", flush=True)
    if comparative:
        print(
            f"  comparative: token_reduction={comparative.get('token_reduction_pct')}% "
            f"tool_error_reduction={comparative.get('tool_error_reduction_pct')}%",
            flush=True,
        )

    print(f"\nОтчёт: {out}", flush=True)
    print(f"Markdown: {md_path}", flush=True)
    print(f"Итог: {'PASS' if report['all_passed'] else 'FAIL'}", flush=True)
    return 0 if report["all_passed"] else 1


def load_config_yaml(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def deep_merge_config(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge_config(out[key], val)
        else:
            out[key] = val
    return out


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(run_compare(args))


if __name__ == "__main__":
    import sys
    sys.exit(main())
