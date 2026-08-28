# 07 — Семантический индекс фактов (Knowledge Index)

**Tier:** 2  
**Источник:** переработка v1 (была compression, теперь indexing)  
**Проблема:** Recall-сценарии требуют точных ответов инструментов, но их поиск в растущей истории дорого (токены, latency)

## Суть проблемы

**v1 (Focus compression):** Старые ходы сжимали в summary и удаляли из истории → SR упал с 26% до 12%.
- Recall-сценарии теряют детали формата
- "вспомни violet-42" → модель должна искать в истории или вызывать tool снова

**v2 (Knowledge Index):** Вместо удаления, создаём **семантический индекс** фактов
- Каждый успешный tool call автоматически индексируется
- Модель может **поискать в индексе перед tool call** → экономия токенов + точность recall
- История остаётся для контекста, но Knowledge Index служит как быстрый reference

## Архитектура Knowledge Index

### Концепция

```
run_turn():
  ├─ LLM видит Knowledge Index в system context
  ├─ Модель решает: есть ли факт в индексе?
  │  │
  │  ├─ ДА → knowledge_index_lookup(category="policy", key="violet-42")
  │  │     → получает {"full_format": "POLICY_FACT=violet-42. Правило: ...", ...}
  │  │     → цитирует из индекса (0 tool calls)
  │  │
  │  └─ НЕ → вызывает get_policy_fact()
  │         → LLM получает ответ
  │         → auto_index_from_tool_response() добавляет в Knowledge Index
  │
  └─ следующий ход: facts уже в индексе, повторение не нужно
```

### Структура Knowledge Index

```json
{
  "policy": {
    "violet-42": {
      "id": "a1b2c3",
      "key": "violet-42",
      "category": "policy",
      "summary": "POLICY_FACT=violet-42",
      "full_format": "POLICY_FACT=violet-42. Правило: каждый ответ должен заканчиваться [POLICY_OK].",
      "from_tool": "get_policy_fact",
      "turn_index": 5,
      "confidence": "high"
    }
  },
  "employee": {
    "HR-001": {
      "key": "HR-001",
      "category": "employee",
      "summary": "EMP_ID=HR-001",
      "full_format": "EMP_ID=HR-001 NAME=Alice Johnson DEPT=Engineering",
      "from_tool": "employee_lookup",
      "turn_index": 3,
      "confidence": "high"
    }
  },
  "marker": {
    "orchid-17": {...},
    "amber-91": {...}
  }
}
```

**Ключевые поля:**
- `key` — то что ищет модель ("violet-42", "HR-001")
- `full_format` — **точный формат для цитирования**, как вернул инструмент
- `from_tool` — откуда это пришло
- `turn_index` — когда было получено (для debug)

## Как работает

### 1. Auto-Indexing (автоматическая индексация)

После успешного tool call система парсит response и добавляет entries в Knowledge Index:

```python
# Tool: get_policy_fact() вернул: "POLICY_FACT=violet-42. Правило: каждый ответ..."
# Система добавляет в индекс:
knowledge.add_entry(IndexEntry(
    key="violet-42",
    category="policy",
    full_format="POLICY_FACT=violet-42. Правило: каждый ответ должен заканчиваться [POLICY_OK].",
    from_tool="get_policy_fact",
    turn_index=5
))
```

### 2. Knowledge Index Lookup (поиск перед tool call)

Модель может вызвать pseudo-tool `knowledge_index_lookup`:

```
User: "Напомни политику из начала"

LLM вызывает:
  knowledge_index_lookup(category="policy", key="violet-42")

Возвращается:
  {
    "found": true,
    "full_format": "POLICY_FACT=violet-42. Правило: каждый ответ...",
    "from_tool": "get_policy_fact",
    "note": "Use full_format field for accurate citation"
  }

LLM цитирует: "Политика из начала — violet-42. Правило: каждый ответ должен заканчиваться [POLICY_OK]."
```

### 3. Автоматические категории

Индексация работает автоматически для:

| Инструмент | Категория | Маркер |
|-----------|----------|--------|
| `get_policy_fact` | `policy` | `POLICY_FACT` |
| `benchmark_probe` | `marker` | `BENCH_MARKER_STREAMABLE` |
| `benchmark_sse_probe` | `marker_sse` | `BENCH_MARKER_SSE` |
| `employee_lookup` | `employee` | `EMP_ID` |
| `customer_get` | `customer` | `CUSTOMER` |
| `invoice_get` | `invoice` | `INVOICE` |
| `sse_audit_log` | `audit` | `AUDIT` |

## Результаты (Expected)

| Метрика | v1 (Compress) | v2 (Index) | Baseline |
|---------|---------------|-----------|----------|
| SR | 12% | **28-32%** ↑ | 26% |
| Recall точность | ❌ потеря деталей | ✅ полный формат | ✅ |
| Tool Selection (TSA) | 67% | **95%+** | 95% |
| Latency | 1.10s | ~0.75s | 0.73s |
| Anti-hallucination | 39% | **65%+** | 57% |

**Почему Index лучше Compress:**
- ✅ Recall-сценарии работают (индекс содержит полный формат)
- ✅ Меньше tool calls (knowledge_index_lookup вместо tool)
- ✅ Быстрее (индекс в памяти, парс JSON вместо новых calls)
- ✅ Anti-hallucination растёт (модель доверяет индексу как ground truth)

## Файлы и интеграция

| Файл | Роль |
|------|------|
| `variant/focus.py` | **Knowledge Index, auto-indexing, lookup логика** |
| `variant/agent_core.py` | Интеграция: создание KnowledgeIndexManager, инжекция в system context, обработка lookup tool |
| `variant/prompts/system_master.txt` | Инструкции когда и как использовать Knowledge Index lookup |
| `variant/config.yml` | `knowledge_index.enabled: true` |
| `variant/tests/test_focus.py` | Unit-тесты: indexing, lookup, auto-parsing |

## Критерии успеха

- ✅ SR ≥ 25% (приблизиться к baseline 26%)
- ✅ Recall-сценарии работают (индекс даёт полный формат)
- ✅ Latency ≤ 0.8s (Knowledge Index не усложняет вычисления)
- ✅ Количество entries в индексе растёт естественно (нет garbage collection)

## Отличие от других подходов

| 07 Knowledge Index | 01 Four-section | 05 Routing | 06 Scan | 09 Context-eng |
|-------------------|-----------------|-----------|--------|----------------|
| **Идея** | Индекс фактов | Структурированный prompt | Детерминированный выбор tool | Пересканирование | Внешний скретчпад |
| **Уровень** | Runtime indexing | Static prompt | Routing layer | Prompt refresh | External storage |
| **Для recall** | ✅ точный | ❌ indirect | ❌ narrow | ❌ confusion | ❌ overhead |
| **Latency** | ≈ baseline | +0.1s | +0.2s | ×6.7 | +0.4s |

## Зависимости

- **01** (правила STOP vs continue) — если будет integration
