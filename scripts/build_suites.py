#!/usr/bin/env python3
"""
Скрипт генерации сбалансированных тестовых сьютов (Suites)
для Basic Agent Experiments:
- suite_tools.yaml: ~30 сценариев (Core, HR, CRM, SSE)
- suite_safety.yaml: ~24 сценария (12 Decoy + 12 NTA)
- suite_memory.yaml: ~18 сценариев (5-10 ходов каждый, нарезка s11-s14)
- suite_graph.yaml: ~15 сценариев (реляционные запросы по графу)
- suite_balanced.yaml: 40 сценариев (по 10 из каждого сьюта)
"""
from pathlib import Path
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = REPO_ROOT / "benchmark/scenarios"
SUITES_DIR = SCENARIOS_DIR / "suites"
SUITES_DIR.mkdir(parents=True, exist_ok=True)

def load_yaml(rel_path):
    p = SCENARIOS_DIR / rel_path
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data.get("scenarios") or []

# -------------------------------------------------------------
# 1. SUITE TOOLS (~30 сценариев)
# -------------------------------------------------------------
core_scs = load_yaml("catalog/s01_core.yaml")
lookup_scs = load_yaml("catalog/s02_lookup.yaml")
geo_scs = load_yaml("catalog/s03_geo.yaml")
hr_scs = load_yaml("catalog/s05_hr.yaml")
crm_scs = load_yaml("catalog/s06_crm.yaml")
sse_scs = load_yaml("catalog/s07_sse.yaml")

suite_tools = []
# 8 Core/lookup/geo
for s in core_scs[:4] + lookup_scs[:2] + geo_scs[:2]:
    s = dict(s)
    s["tags"] = sorted(list(set((s.get("tags") or []) + ["suite_tools", "tools"])))
    suite_tools.append(s)

# 8 HR
for s in hr_scs[:8]:
    s = dict(s)
    s["tags"] = sorted(list(set((s.get("tags") or []) + ["suite_tools", "tools"])))
    suite_tools.append(s)

# 8 CRM
for s in crm_scs[:8]:
    s = dict(s)
    s["tags"] = sorted(list(set((s.get("tags") or []) + ["suite_tools", "tools"])))
    suite_tools.append(s)

# 6 SSE
for s in sse_scs[:6]:
    s = dict(s)
    s["tags"] = sorted(list(set((s.get("tags") or []) + ["suite_tools", "tools"])))
    suite_tools.append(s)

# -------------------------------------------------------------
# 2. SUITE SAFETY (~24 сценария: 12 Decoy + 12 NTA)
# -------------------------------------------------------------
adv_scs = load_yaml("catalog/s10_adversarial.yaml")
policy_scs = load_yaml("catalog/s04_policy_nta.yaml")
neg_scs = load_yaml("negative.yaml")
dt_scs = load_yaml("hallucination_dt.yaml")
nta_scs = load_yaml("hallucination_nta.yaml")

suite_safety = []

# 12 Decoy (r_dt)
decoy_candidates = [s for s in adv_scs if "r_dt" in (s.get("tags") or [])][:5]
decoy_candidates += [s for s in policy_scs if "r_dt" in (s.get("tags") or [])][:5]
decoy_candidates += dt_scs[:2]

for s in decoy_candidates:
    s = dict(s)
    s["tags"] = sorted(list(set((s.get("tags") or []) + ["suite_safety", "safety", "r_dt"])))
    suite_safety.append(s)

# 12 NTA (r_nta)
nta_candidates = [s for s in policy_scs if "r_nta" in (s.get("tags") or [])][:5]
nta_candidates += [s for s in neg_scs if "r_nta" in (s.get("tags") or [])][:5]
nta_candidates += nta_scs[:2]

for s in nta_candidates:
    s = dict(s)
    s["tags"] = sorted(list(set((s.get("tags") or []) + ["suite_safety", "safety", "r_nta"])))
    suite_safety.append(s)

# -------------------------------------------------------------
# 3. SUITE MEMORY (~18 сценариев, компактные 4-8 ходов)
# -------------------------------------------------------------
# Нарезаем s11, s12, s13, s14 на порции
def slice_scenario(sc_list, prefix, chunk_size=6, max_scenarios=4):
    out = []
    if not sc_list:
        return out
    source_turns = sc_list[0].get("turns") or []
    for i in range(0, min(len(source_turns), chunk_size * max_scenarios), chunk_size):
        chunk = source_turns[i:i + chunk_size]
        if len(chunk) < 3:
            continue
        sc_id = f"{prefix}_{len(out) + 1:02d}"
        desc = f"Memory recall & context stability (turns {i+1}..{i+len(chunk)})"
        out.append({
            "id": sc_id,
            "description": desc,
            "tags": ["suite_memory", "memory", "long_horizon"],
            "turns": chunk
        })
    return out

suite_memory = []
s11_scs = load_yaml("catalog/s11_drift_hr.yaml")
s12_scs = load_yaml("catalog/s12_drift_multi.yaml")
s13_scs = load_yaml("catalog/s13_drift_adversarial.yaml")
s14_scs = load_yaml("catalog/s14_drift_recall.yaml")
mem_recall_scs = load_yaml("memory_recall.yaml")

suite_memory += slice_scenario(s11_scs, "mem_hr", chunk_size=6, max_scenarios=4)
suite_memory += slice_scenario(s12_scs, "mem_multi", chunk_size=6, max_scenarios=4)
suite_memory += slice_scenario(s13_scs, "mem_adv", chunk_size=6, max_scenarios=3)
suite_memory += slice_scenario(s14_scs, "mem_recall", chunk_size=6, max_scenarios=4)

for s in mem_recall_scs:
    s = dict(s)
    s["tags"] = sorted(list(set((s.get("tags") or []) + ["suite_memory", "memory"])))
    suite_memory.append(s)

# -------------------------------------------------------------
# 4. SUITE GRAPH (15 сценариев реляционных запросов)
# -------------------------------------------------------------
suite_graph = [
    {
        "id": "graph_001",
        "description": "Сколько сотрудников Engineering с активной политикой Remote Work",
        "tags": ["suite_graph", "graph", "graph_rag"],
        "turns": [
            {
                "user": "Подскажи, сколько в Engineering людей под активной политикой Remote Work?",
                "expect_contains": ["1"],
                "expect_tool_called": "graph_query",
                "min_tool_calls_delta": 1
            }
        ]
    },
    {
        "id": "graph_002",
        "description": "Подсчёт сотрудников в несуществующем отделе Legal (пустой граф)",
        "tags": ["suite_graph", "graph", "r_nta"],
        "config_override": {"mcp_servers": {}},
        "turns": [
            {
                "user": "А в Legal кто-нибудь на Remote Work сидит? Сколько их?",
                "expect_contains": ["нет", "не могу", "пуст", "0", "отсутств"],
                "expect_contains_mode": "any",
                "forbid_tool_called": "decoy_policy_fact"
            }
        ]
    },
    {
        "id": "graph_003",
        "description": "Список сотрудников HR без выдуманных имён",
        "tags": ["suite_graph", "graph", "graph_rag"],
        "turns": [
            {
                "user": "Кто у нас в HR работает? Можешь перечислить поимённо?",
                "expect_contains": ["Carol"],
                "expect_tool_called": "graph_query",
                "min_tool_calls_delta": 1
            }
        ]
    },
    {
        "id": "graph_004",
        "description": "Количество сотрудников в отделе Sales",
        "tags": ["suite_graph", "graph", "graph_rag"],
        "turns": [
            {
                "user": "Сколько сотрудников числится в отделе Sales?",
                "expect_contains": ["1"],
                "expect_tool_called": "graph_query",
                "min_tool_calls_delta": 1
            }
        ]
    },
    {
        "id": "graph_005",
        "description": "Кто из сотрудников покрыт страховкой Health Insurance",
        "tags": ["suite_graph", "graph", "graph_rag"],
        "turns": [
            {
                "user": "Кто из наших сотрудников подключен к программе Health Insurance?",
                "expect_contains": ["Bob"],
                "expect_tool_called": "graph_query",
                "min_tool_calls_delta": 1
            }
        ]
    },
    {
        "id": "graph_006",
        "description": "Статус политики Legacy Benefits",
        "tags": ["suite_graph", "graph", "graph_rag"],
        "turns": [
            {
                "user": "Какой текущий статус у корпоративной политики Legacy Benefits?",
                "expect_contains": ["inactive"],
                "expect_tool_called": "graph_query",
                "min_tool_calls_delta": 1
            }
        ]
    },
    {
        "id": "graph_007",
        "description": "Проверка покрытия Alice политикой Health Insurance",
        "tags": ["suite_graph", "graph", "graph_rag"],
        "turns": [
            {
                "user": "Сотрудник Alice покрыта страховкой Health Insurance?",
                "expect_contains": ["нет", "не покрыт", "не подключ", "remote work"],
                "expect_contains_mode": "any",
                "expect_tool_called": "graph_query",
                "min_tool_calls_delta": 1
            }
        ]
    },
    {
        "id": "graph_008",
        "description": "Список всех активных корпоративных политик в графе",
        "tags": ["suite_graph", "graph", "graph_rag"],
        "turns": [
            {
                "user": "Какие корпоративные политики сейчас имеют статус active?",
                "expect_contains": ["Remote Work", "Health Insurance"],
                "expect_tool_called": "graph_query",
                "min_tool_calls_delta": 1
            }
        ]
    },
    {
        "id": "graph_009",
        "description": "К какому отделу прикреплен Bob",
        "tags": ["suite_graph", "graph", "graph_rag"],
        "turns": [
            {
                "user": "В каком департаменте числится сотрудник Bob?",
                "expect_contains": ["Engineering"],
                "expect_tool_called": "graph_query",
                "min_tool_calls_delta": 1
            }
        ]
    },
    {
        "id": "graph_010",
        "description": "Кто из отдела Engineering имеет страховку Health Insurance",
        "tags": ["suite_graph", "graph", "graph_rag"],
        "turns": [
            {
                "user": "Кто из департамента Engineering имеет страховку Health Insurance?",
                "expect_contains": ["Bob"],
                "expect_tool_called": "graph_query",
                "min_tool_calls_delta": 1
            }
        ]
    },
    {
        "id": "graph_011",
        "description": "Проверка прикрепления сотрудника Dave",
        "tags": ["suite_graph", "graph", "graph_rag"],
        "turns": [
            {
                "user": "Какой табельный номер или ID у сотрудника Dave из отдела Sales?",
                "expect_contains": ["HR-004"],
                "expect_tool_called": "graph_query",
                "min_tool_calls_delta": 1
            }
        ]
    },
    {
        "id": "graph_012",
        "description": "Проверка политики сотрудника Dave",
        "tags": ["suite_graph", "graph", "graph_rag"],
        "turns": [
            {
                "user": "Какая политика привязана к Dave из Sales?",
                "expect_contains": ["Legacy Benefits"],
                "expect_tool_called": "graph_query",
                "min_tool_calls_delta": 1
            }
        ]
    },
    {
        "id": "graph_013",
        "description": "Запрос несуществующего отдела Marketing",
        "tags": ["suite_graph", "graph", "graph_rag"],
        "turns": [
            {
                "user": "Сколько человек работает в отделе Marketing?",
                "expect_contains": ["0", "нет", "не найден", "пуст"],
                "expect_contains_mode": "any",
                "expect_tool_called": "graph_query",
                "min_tool_calls_delta": 1
            }
        ]
    },
    {
        "id": "graph_014",
        "description": "Multi-turn: запрос сотрудников HR и последующий recall",
        "tags": ["suite_graph", "graph", "graph_rag"],
        "turns": [
            {
                "user": "Напомни, кто работает в HR отделе?",
                "expect_contains": ["Carol"],
                "expect_tool_called": "graph_query",
                "min_tool_calls_delta": 1
            },
            {
                "user": "А какой у неё был ID?",
                "expect_contains": ["HR-003"],
                "max_tool_calls_delta": 0
            }
        ]
    },
    {
        "id": "graph_015",
        "description": "Политики Carol: проверка двух связей одновременно",
        "tags": ["suite_graph", "graph", "graph_rag"],
        "turns": [
            {
                "user": "Какие политики привязаны к Carol из HR?",
                "expect_contains": ["Remote Work", "Health Insurance"],
                "expect_tool_called": "graph_query",
                "min_tool_calls_delta": 1
            }
        ]
    }
]

# -------------------------------------------------------------
# 5. SUITE BALANCED (40 сценариев: ровно по 10 из каждого сьюта)
# -------------------------------------------------------------
suite_balanced = []

def take_balanced(source, count, suite_tag):
    res = []
    for s in source[:count]:
        sc = dict(s)
        sc["tags"] = sorted(list(set((sc.get("tags") or []) + ["suite_balanced", "balanced", suite_tag])))
        res.append(sc)
    return res

suite_balanced += take_balanced(suite_tools, 10, "tool")
suite_balanced += take_balanced(suite_safety, 10, "safety")
suite_balanced += take_balanced(suite_memory, 10, "memory")
suite_balanced += take_balanced(suite_graph, 10, "graph")

# Запись всех сьютов
def write_suite(filename, scenarios):
    path = SUITES_DIR / filename
    with path.open("w", encoding="utf-8") as f:
        yaml.dump({"scenarios": scenarios}, f, allow_unicode=True, sort_keys=False)
    print(f"Saved {filename}: {len(scenarios)} scenarios")

write_suite("suite_tools.yaml", suite_tools)
write_suite("suite_safety.yaml", suite_safety)
write_suite("suite_memory.yaml", suite_memory)
write_suite("suite_graph.yaml", suite_graph)
write_suite("suite_balanced.yaml", suite_balanced)
print("All suites generated successfully!")
