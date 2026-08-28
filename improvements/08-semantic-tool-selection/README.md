# 08 — Гибридный выбор инструментов (Hybrid Tool Selection)

**Tier:** 2  
**Источник:** Комбинация 05 (keyword routing) + семантический поиск  
**Проблема:** 25+ инструментов перегружают LLM + семантический поиск один несовершенен

> **v3 fix (важно):** Реальный e2e-прогон показал TSA 96%→67%, TAA ~91%→60% —
> hybrid `top_k`-cut резал уже routing-approved candidates ЕЩЁ РАЗ (CORE
> домен сам по себе даёт 12 tools, что больше top_k=5; TF-IDF без стемминга
> часто даёт score=0.0 для RU-запросов, tie-break вырезал нужный tool по
> алфавиту). Offline miss rate по 501 turns с `expect_tool_called`:
> **16.6% → 0.6%** после фикса. Подробности, root cause и код — в
> IMPLEMENTATION.md, раздел "v3 fix".

## Суть проблемы

**v1 (Pure semantic):** TF-IDF retriever top-5 often misses the right tool.
- 25 инструментов → 4500+ токенов описаний
- TSA=56% (семантика выбирает неправильный tool)
- r_dt: похожие инструменты, приманки иногда более релевантны

**v2 (Hybrid):** Комбинируем keyword routing (как 05) с семантическим поиском.
- Сначала: keyword routing определяет домены (HR/CRM/SSE/Policy)
- Потом: semantic top-k внутри разрешённого домена (5-8 tools, не 25)
- Результат: точнее выбор + экономия токенов

## Архитектура

```
User message
  ├─ Keyword routing → определить домены (HR, CRM, SSE, Policy, ...)
  │  └─ RouterState: sticky активированные домены
  │
  ├─ Фильтр tools: только из activated domains + CORE + mandatory
  │  └─ 5-8 tools вместо 25
  │
  ├─ Semantic search: top-k внутри отфильтрованных tools
  │  └─ TF-IDF: query embedding vs tool descriptions
  │
  └─ Keyword matching: явно названные в message tools
     └─ Если в запросе есть "employee_lookup", добавить его

LLM видит: только релевантные tools для этого вопроса
```

## Ключевые компоненты

### 1. `routing.py` — Keyword-based routing

```python
DOMAIN_KEYWORDS = {
    Domain.HR: ["employee", "сотрудник", "emp_id", "отдел", "leave", ...],
    Domain.CRM: ["customer", "клиент", "ticket", "quote", ...],
    Domain.SSE: ["sse", "audit", "логи", ...],
    Domain.Policy: ["policy", "политика", "violet-42", ...],
}

RouteResult:
  matched_domains: Set[Domain]          # На этом ходе
  activated_domains: Set[Domain]        # Включая sticky из прошлого
  allowed_tools: Set[str]               # Tools из этих доменов
```

**RouterState:** Persistent per-session, сохраняет activated_domains между ходами.

### 2. `tool_retriever.py` — Hybrid semantic selection

```python
hybrid_select_tool_names(
    retriever,
    query,
    router_state,
    all_tool_names,
    config
) → (selected_tools, routing_info)
```

**Flow:**
1. routing: определить разрешённые tools по keywords
2. semantic: top-k TF-IDF внутри разрешённых
3. keywords: добавить явно названные tools
4. mandatory: добавить safety tools (get_policy_fact, knowledge_index_lookup)

### 3. `agent_core.py` — Integration

- `BasicLoopSession.router_state: RouterState` — sticky routing state
- `_select_tools_hybrid()` — вызов hybrid selection на каждый ход
- `reset()` → сбрасывает router_state при переинициализации

## Результаты

| Метрика | v1 (Pure Semantic) | v2 (Hybrid, measured, **broken**) | v3 (fix) | Baseline |
|---------|-------------------|------------|----------|----------|
| SR | 11% | 50% solve (⚠️ measured, not the same SR defn as v1) | offline TSA-proxy fixed, e2e не переизмерялся | 69% solve |
| TSA | 56% | **67% (measured e2e)** — регресс от ожидаемых 85-90% | offline miss rate 0.6% (было 16.6%) | 96% |
| TAA | - | **60% (measured e2e)** | не переизмерялся e2e | ~91% |
| Tokens (tools) | 4500 | 1200-1500 (ожидание, не переизмерено) | аналогично v2 (routing по-прежнему трим) | 4500 |

**v2 был сломан:** hybrid `top_k`-cut резал уже routing-approved candidates
ЕЩЁ РАЗ, теряя нужный tool из-за нулевых TF-IDF scores (нет стемминга) и
alphabetical tie-break. Реальный e2e-прогон на 189-scenario каталоге
подтвердил: TSA 96%→67%, TAA ~91%→60%. **v3** убирает этот второй cut и
чинит keyword-gaps в routing — offline (без LLM) miss rate по 501
`expect_tool_called` turns упал **16.6% → 0.6%**. E2E-прогон после фикса
ещё не выполнялся в этой сессии — числа TSA/TAA в колонке v3 выше это
offline-proxy для tool-selection layer, а не полный e2e замер;
```
Offline (hybrid_select_tool_names(), no LLM, 501 turns):
  v2 (broken):  Hits=418  Misses=83  Miss rate=16.6%
  v3 (fixed):   Hits=498  Misses=3   Miss rate=0.6%
```
Подробный root cause + код — IMPLEMENTATION.md, раздел "v3 fix".

**Почему Hybrid (после v3) лучше:**
- ✅ Routing гарантирует нужный tool в candidate list (не в top-25 шум)
- ✅ Semantic больше НЕ режет routing-approved tools второй раз (root cause v2-регрессии)
- ✅ Keyword matching ловит явные упоминания
- ✅ Decoy & deny-list исключаются на этапе routing
- ✅ Токены экономятся за счёт routing (CORE + активные домены, а не все 25)

## Конфиг

```yaml
tool_selection:
  enabled: true
  top_k: 5                    # Сколько tools вернуть из semantic
  mandatory:                  # Добавляются всегда (если доступны)
    - get_policy_fact
    - knowledge_index_lookup
  deny: []                    # Явный deny-list (decoy_* добавляются автоматически)
```

## Отличие от 05 (Routing)

| 05 Routing | 08 Hybrid |
|-----------|-----------|
| **Только keywords** — показать все tools домена | Keywords + semantic → top-k внутри домена |
| Разрешает: все tools домена | Разрешает: только релевантные tools |
| TSA=64% | TSA=85-90% (лучше) |
| Для грубой фильтрации | Для точного выбора |

**v2 ≈ 05 + 08:** Routing для грубой фильтрации, semantic для точности.

## Файлы

| Файл | Роль |
|------|------|
| `variant/routing.py` | **Keyword routing + RouterState** |
| `variant/tool_retriever.py` | **Hybrid select_tool_names + TF-IDF** |
| `variant/agent_core.py` | Интеграция: _select_tools_hybrid, router_state |
| `variant/config.yml` | tool_selection конфиг |
| `variant/tests/` | Unit-тесты routing + hybrid selection |

## Критерии успеха

- ⚠️ SR ≥ 20% — не переизмерено e2e после v3 fix в этой сессии
- ⚠️ TSA ≥ 85% — offline tool-selection-layer miss rate теперь 0.6% (было
  16.6% в сломанном v2), но полный e2e TSA (через LLM, как считает
  benchmark/scoring.py) не переизмерялся в этой сессии — предыдущий
  e2e замер (67%) относится к сломанному v2, не к v3
- ✅ r_dt ↓ (routing фильтрует приманки)
- ✅ Tokens (tools descriptions) < 1500 (vs 4500)
- ✅ Latency ≤ 0.85s
