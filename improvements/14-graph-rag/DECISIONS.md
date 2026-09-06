# Принятые решения — 14 Graph-RAG

Дата: 2026-07-01. Вопросы из `QUESTIONS.md` закрыты без участия product-owner.

## 1. Graph DB для production → **Neo4j**

**Решение:** Neo4j как целевой persistent store (Cypher, зрелый ETL через APOC).

| Среда | Backend | Конфиг |
|-------|---------|--------|
| Бенчмарк / dev | `memory` (default) | `graph_rag.backend: memory` |
| Staging / prod | `neo4j` | `graph_rag.backend: neo4j` + `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD` |

Если драйвер Neo4j не установлен или URI не задан — **автоматический fallback на memory** + warning в лог.

Neptune/ArangoDB не рассматриваем: стек Baseline Agent не AWS-only, Neo4j проще для команды с MCP/self-hosted.

## 2. ETL → nightly snapshot из HR MCP + policy

**Решение (фаза 1):**

- **Оперативные сущности** (employee, department): nightly batch job вызывает HR MCP (`employee_lookup`, `org_chart_dept`), пишет `data/graph_snapshot.json`.
- **Политики**: факт `get_policy_fact` + ручной seed в snapshot (policy nodes).
- **Частота:** nightly sync; intraday — только через MCP tools агента, не через граф.
- **DWH:** отложено до фазы 2, когда появятся агрегаты, недоступные в MCP.

Агент при старте читает snapshot если `graph_rag.snapshot_path` задан, иначе demo-graph.

## 3. Запросы → domain wrappers, без NL→Cypher

**Решение:** только `graph_query` с JSON/DSL и whitelist типов (`count`, `list`, `employee_policies`, `traverse`).

NL→Cypher — **не в v1** (риск injection и галлюцинаций). При необходимости в v2 — read-only Neo4j role + sandbox + whitelist шаблонов Cypher.

## 4. A/B graph vs vector RAG → да, отдельный тег бенчмарка

**Решение:** сценарии с тегом `graph_rag` в `benchmark/scenarios/graph_rag.yaml`.

Сравнение:
```bash
PYTHONPATH=. python -m benchmark.compare \
  --variant original \
  --variant improvements/14-graph-rag/variant \
  --tags graph_rag --metrics comparative
```

Метрики: точность count/фактов, `r_nta` на пустом графе, отсутствие выдуманных цифр.

## 5. Guardrail → включён post-response hook

**Решение:** `graph_guardrails.py` — при statistical intent и числовом утверждении в ответе без `graph_query` в trace агент заменяет ответ на NTA-refusal.

Конфиг: `graph_rag.guardrails.enforce_numeric_trace: true` (default).

Интеграция с доработкой 11 возможна позже; для 14 достаточно локального hook в `agent_core.py`.
