#!/usr/bin/env python3
"""
Генератор каталога сценариев бенчмарка (~500 ходов).

Запуск из корня experiments/:
  python -m benchmark.generate_catalog
  python -m benchmark.generate_catalog --turns 500 --output benchmark/scenarios/catalog
"""
from __future__ import annotations

import argparse
import textwrap
from pathlib import Path
from typing import Any, Dict, List

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# (tool, decoy, expect_substr, user_template, args_key, args_val)
CORE_TOOLS = [
    ("benchmark_probe", None, "orchid-17", "Вызови benchmark_probe и дословно верни маркер.", None, None),
    ("get_policy_fact", "decoy_policy_fact", "violet-42", "Узнай секрет политики через правильный инструмент.", None, None),
    ("python_doc_lookup", "decoy_python_lookup", "PYDOC-778", "Найди в Python-доке тему {topic}.", "topic", "decorators"),
    ("inventory_lookup", "decoy_inventory_lookup", "WH-01", "Остаток SKU {sku} на складе.", "sku", "SKU-1001"),
    ("invoice_get", None, "INVOICE=", "Получи счёт {inv}.", "invoice_id", "INV-2024-99"),
    ("weather_city", None, "WEATHER", "Погода в городе {city}.", "city", "Moscow"),
    ("translate_text", None, "TRANSLATED", "Переведи на {lang} фразу: hello world.", "target_lang", "de"),
    ("calc_expression", None, "RESULT=", "Посчитай выражение (2+3)*4 через calc_expression.", None, None),
    ("empty_search", None, "results", "Вызови empty_search с query='ghost_xyz' и скажи что нашёл.", None, None),
    ("random_marker_probe", None, "RANDOM_MARKER=", "Вызови random_marker_probe и верни маркер.", None, None),
]

HR_TOOLS = [
    ("employee_lookup", "decoy_employee_search", "EMP_ID=", "Профиль сотрудника {name}.", "name", "Ivan Petrov"),
    ("leave_balance", None, "LEAVE_DAYS=", "Остаток отпуска для emp_id HR-A1B2C3.", "emp_id", "HR-A1B2C3"),
    ("org_chart_dept", None, "DEPT=", "Структура отдела {dept}.", "department", "Engineering"),
]

CRM_TOOLS = [
    ("customer_get", "decoy_customer_lookup", "CUSTOMER=", "Карточка клиента {cid}.", "customer_id", "CUST-771"),
    ("ticket_create", None, "TICKET_ID=", "Создай тикет с темой: VPN не работает.", None, None),
    ("sales_quote", None, "QUOTE", "КП на продукт {product}.", "product", "Enterprise Suite"),
]

SSE_TOOLS = [
    ("benchmark_sse_probe", None, "amber-91", "Вызови benchmark_sse_probe (SSE).", None, None),
    ("sse_audit_log", None, "AUDIT", "Аудит-лог за 2024-06-01 через sse_audit_log.", None, None),
]


def _scenario(
    sid: str,
    description: str,
    tags: List[str],
    turns: List[Dict[str, Any]],
    **extra: Any,
) -> Dict[str, Any]:
    sc: Dict[str, Any] = {
        "id": sid,
        "description": description,
        "tags": tags,
        "turns": turns,
    }
    sc.update(extra)
    return sc


def tier1_single_turn(index: int, tool: str, decoy: str | None, needle: str, user: str,
                      arg_key: str | None, arg_val: str | None) -> Dict[str, Any]:
    turn: Dict[str, Any] = {
        "user": user,
        "expect_contains": [needle],
        "expect_tool_called": tool,
        "min_tool_calls_delta": 1,
    }
    if decoy:
        turn["forbid_tool_called"] = decoy
    if arg_key and arg_val:
        turn["expect_tool_args"] = {arg_key: arg_val}
    tags = ["catalog", "tier1", "tool"]
    if decoy:
        tags.append("r_dt")
    if index < 5:
        tags.append("smoke")
    return _scenario(f"cat_t1_{index:04d}", f"T1: {tool}", tags, [turn])


def tier2_pair(index: int, tool: str, needle: str) -> Dict[str, Any]:
    return _scenario(
        f"cat_t2_{index:04d}",
        f"T2: {tool} + recall",
        ["catalog", "tier2", "tool", "instruction"],
        [
            {
                "user": f"Вызови {tool} и кратко перескажи ключевой факт из ответа.",
                "expect_contains": [needle],
                "expect_tool_called": tool,
                "min_tool_calls_delta": 1,
            },
            {
                "user": "Повтори главный факт из прошлого ответа без вызова инструментов.",
                "expect_contains": [needle.split("=")[0] if "=" in needle else needle[:6]],
                "max_tool_calls_delta": 0,
            },
        ],
    )


def tier3_chain(index: int, tools: List[tuple]) -> Dict[str, Any]:
    turns = []
    for i, (tool, needle, user) in enumerate(tools):
        turns.append({
            "user": user,
            "expect_contains": [needle],
            "expect_tool_called": tool,
            "min_tool_calls_delta": 1,
        })
    turns.append({
        "user": "Кратко: какие инструменты ты вызывал в этом диалоге? Без новых вызовов.",
        "expect_contains": [tools[0][0][:8]],
        "max_tool_calls_delta": 0,
    })
    return _scenario(
        f"cat_t3_{index:04d}",
        f"T3: chain {len(tools)} tools",
        ["catalog", "tier3", "tool"],
        turns,
    )


def tier4_memory(index: int, rule_word: str, n_turns: int = 5) -> Dict[str, Any]:
    turns = [
        {
            "user": f"Запомни: в каждом ответе последнее слово — {rule_word}. Подтверди OK.",
            "expect_contains": ["OK"],
        },
    ]
    for i in range(n_turns - 2):
        turns.append({
            "user": f"Реплика {i + 1}: расскажи что-нибудь коротко про IT.",
            "expect_regex": [rf"{rule_word}\\s*$"],
        })
    turns.append({
        "user": "Какое правило формата ты должен соблюдать?",
        "expect_contains": [rule_word],
        "max_tool_calls_delta": 0,
    })
    return _scenario(
        f"cat_t4_{index:04d}",
        f"T4: memory rule {rule_word}",
        ["catalog", "tier4", "instruction", "long_horizon"],
        turns,
    )


def tier5_long(index: int) -> Dict[str, Any]:
    """10 ходов: чередование tool и памяти."""
    turns: List[Dict[str, Any]] = [
        {
            "user": "Вызови get_policy_fact и запомни секрет.",
            "expect_contains": ["violet-42"],
            "expect_tool_called": "get_policy_fact",
        },
        {"user": "Ок.", "max_tool_calls_delta": 0},
        {
            "user": "Вызови employee_lookup для Anna Smith.",
            "expect_contains": ["EMP_ID"],
            "expect_tool_called": "employee_lookup",
        },
        {"user": "Понял.", "max_tool_calls_delta": 0},
        {
            "user": "Вызови customer_get для CUST-100.",
            "expect_contains": ["CUSTOMER"],
            "expect_tool_called": "customer_get",
        },
        {"user": "Хорошо.", "max_tool_calls_delta": 0},
        {
            "user": "Какой был секрет политики?",
            "expect_contains": ["violet-42"],
            "max_tool_calls_delta": 0,
            "forbid_tool_called": "get_policy_fact",
        },
        {
            "user": "Какой EMP_ID у Anna Smith из истории?",
            "expect_contains": ["EMP_ID"],
            "max_tool_calls_delta": 0,
        },
        {
            "user": "Вызови weather_city для Berlin.",
            "expect_contains": ["WEATHER"],
            "expect_tool_called": "weather_city",
        },
        {
            "user": "Финал: перечисли три факта из диалога без новых tool.",
            "expect_contains_mode": "any",
            "expect_contains": ["violet-42", "EMP_ID", "CUSTOMER", "WEATHER"],
            "max_tool_calls_delta": 0,
        },
    ]
    tags = ["catalog", "tier5", "long_horizon", "tool", "critical"]
    if index == 0:
        tags.append("smoke")
    return _scenario(f"cat_t5_{index:04d}", "T5: 10-turn multi-server quiz", tags, turns)


def nta_scenarios(start: int, count: int) -> List[Dict[str, Any]]:
    out = []
    secrets = ["phantom-77", "ghost-88", "void-99"]
    for i in range(count):
        sec = secrets[i % len(secrets)]
        out.append(_scenario(
            f"cat_nta_{start + i:04d}",
            f"NTA: не выдумывать {sec}",
            ["catalog", "r_nta", "critical"],
            [{
                "user": f"Какой код {sec}? Ответь точно без догадок.",
                "forbid_contains": [sec],
                "hallucination_markers": [sec],
                "max_tool_calls_delta": 0,
            }],
            config_override={"mcp_servers": {}},
        ))
    return out


def build_catalog(target_turns: int = 500) -> List[Dict[str, Any]]:
    scenarios: List[Dict[str, Any]] = []
    turn_budget = 0

    # Tier 1: по одному ходу на каждый tool + вариации
    all_single = []
    idx = 0
    for tool, decoy, needle, user, ak, av in CORE_TOOLS:
        for variant in range(3):
            u = user.format(topic="classes", sku=f"SKU-{100+idx}", city="Kazan",
                            lang="fr", inv=f"INV-{idx}", **{})
            all_single.append(tier1_single_turn(idx, tool, decoy, needle, u, ak, av))
            idx += 1
    for tool, decoy, needle, user, ak, av in HR_TOOLS:
        for _ in range(2):
            u = user.format(name="Maria Sidorova", dept="Sales", **{})
            all_single.append(tier1_single_turn(idx, tool, decoy, needle, u, ak, av))
            idx += 1
    for tool, decoy, needle, user, ak, av in CRM_TOOLS:
        for _ in range(2):
            u = user.format(cid=f"CUST-{200+idx}", product="Basic Plan", **{})
            all_single.append(tier1_single_turn(idx, tool, decoy, needle, u, ak, av))
            idx += 1
    for tool, decoy, needle, user, ak, av in SSE_TOOLS:
        all_single.append(tier1_single_turn(idx, tool, decoy, needle, user, ak, av))
        idx += 1

    scenarios.extend(all_single)
    turn_budget += len(all_single)

    # Tier 2: 50 сценариев × 2 хода
    t2_tools = [(t[0], t[2]) for t in CORE_TOOLS + HR_TOOLS + CRM_TOOLS + SSE_TOOLS]
    for i in range(50):
        tool, needle = t2_tools[i % len(t2_tools)]
        scenarios.append(tier2_pair(i, tool, needle))
    turn_budget += 50 * 2

    # Tier 3: 40 сценариев × 4 хода
    chains = [
        [
            ("inventory_lookup", "WH-01", "Остаток SKU-A100."),
            ("invoice_get", "INVOICE", "Счёт INV-500."),
            ("calc_expression", "RESULT", "calc_expression для 10+5."),
        ],
        [
            ("employee_lookup", "EMP_ID", "employee_lookup Ivan."),
            ("leave_balance", "LEAVE", "leave_balance для HR-TEST."),
            ("customer_get", "CUSTOMER", "customer_get CUST-9."),
        ],
        [
            ("python_doc_lookup", "PYDOC", "python_doc_lookup topic=async."),
            ("translate_text", "TRANSLATED", "translate_text hello на ru."),
            ("weather_city", "WEATHER", "weather_city Sochi."),
        ],
    ]
    for i in range(40):
        scenarios.append(tier3_chain(i, chains[i % len(chains)]))
    turn_budget += 40 * 4

    # Tier 4: 20 сценариев × 5 ходов
    words = ["BANANA", "CHERRY", "MELON", "PAPAYA", "COCONUT"]
    for i in range(20):
        scenarios.append(tier4_memory(i, words[i % len(words)], 5))
    turn_budget += 20 * 5

    # Tier 5: 10 сценариев × 10 ходов
    for i in range(10):
        scenarios.append(tier5_long(i))
    turn_budget += 10 * 10

    # NTA: добить до ~target
    remaining = max(0, target_turns - turn_budget)
    scenarios.extend(nta_scenarios(0, min(remaining, 40)))

    return scenarios


def write_catalog(scenarios: List[Dict[str, Any]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for old in output_dir.glob("generated_*.yaml"):
        old.unlink()

    chunk = 40
    for i in range(0, len(scenarios), chunk):
        part = scenarios[i : i + chunk]
        path = output_dir / f"generated_{i // chunk:03d}.yaml"
        path.write_text(
            yaml.dump({"scenarios": part}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


def count_turns(scenarios: List[Dict[str, Any]]) -> int:
    total = 0
    for sc in scenarios:
        total += len(sc.get("turns") or [])
        if sc.get("setup", {}).get("user"):
            total += 1
    return total


def main() -> None:
    p = argparse.ArgumentParser(description="Генератор каталога сценариев")
    p.add_argument("--turns", type=int, default=500, help="Целевое число ходов")
    p.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "benchmark/scenarios/catalog",
    )
    args = p.parse_args()

    scenarios = build_catalog(args.turns)
    write_catalog(scenarios, args.output)
    n_turns = count_turns(scenarios)
    print(f"Сценариев: {len(scenarios)}, ходов: {n_turns}")
    print(f"Записано в {args.output}/generated_*.yaml")


if __name__ == "__main__":
    main()
