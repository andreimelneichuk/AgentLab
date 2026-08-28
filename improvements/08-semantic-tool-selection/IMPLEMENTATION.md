# IMPLEMENTATION — 08 Hybrid Tool Selection (v2, + v3 fix)

> **Переработка с v1:** Вместо pure semantic (TF-IDF top-5), используем keyword routing + semantic внутри домена для точного выбора.
>
> **v3 fix (после реального e2e-прогона на полном 189-scenario каталоге):**
> v2 collapsed TSA 96%→67% — семантический top-k резал уже routing-approved
> candidates второй раз. См. раздел "v3 fix" в конце файла.

## Что реализовано

### `variant/routing.py` — Keyword routing (новый файл)

**Domain enum и домены:**
```python
Domain = Enum("Domain", ["CORE", "HR", "CRM", "SSE", "Policy"])

DOMAIN_TOOLS = {
    Domain.CORE: [...],      # benchmark_probe, get_policy_fact, ...
    Domain.HR: [...],        # employee_lookup, leave_balance, org_chart_dept
    Domain.CRM: [...],       # customer_get, ticket_create, sales_quote
    Domain.SSE: [...],       # benchmark_sse_probe, sse_audit_log
    Domain.Policy: [...],    # get_policy_fact
}
```

**Keyword matching:**
```python
DOMAIN_KEYWORDS = {
    Domain.HR: ["employee", "сотрудник", "emp_id", "отдел", "leave", ...],
    Domain.CRM: ["customer", "ticket", "quote", ...],
    Domain.SSE: ["sse", "audit", "логи", ...],
    Domain.Policy: ["policy", "violet-42", ...],
}
```

**RouterState — per-session sticky state:**
```python
@dataclass
class RouterState:
    activated_domains: Set[Domain] = field(default_factory=set)
    
    def reset(self) -> None:
        self.activated_domains.clear()
```

**route_turn(state, user_message) → RouteResult:**
1. `_match_domains_by_keywords(message)` — найти домены
2. `state.activated_domains |= matched` — sticky update
3. Собрать allowed_tools = CORE + activated_domains + MANDATORY - DECOY
4. Вернуть RouteResult с matched/activated доменами и allowed tools

### `variant/tool_retriever.py` — Hybrid semantic selection

**Новая функция `hybrid_select_tool_names()`:**
```python
def hybrid_select_tool_names(
    retriever: TfidfToolRetriever,
    query: str,
    router_state: RouterState,
    *,
    all_tool_names: Sequence[str],
    config: ToolSelectionConfig,
) -> tuple[List[str], str]:
    """
    Гибридный выбор:
    1. Routing: определить разрешённые tools
    2. Semantic: top-k TF-IDF внутри разрешённых
    3. Keywords: явно названные tools
    4. Mandatory: safety tools
    
    Returns: (selected_tool_names, routing_prompt_fragment)
    """
```

**Flow:**
1. `route_turn(router_state, query)` → allowed_by_routing
2. Intersection: candidate_tools = allowed_by_routing ∩ ~denied
3. Mandatory tools (всегда добавить, если в candidates)
4. Semantic retrieval: top-k TF-IDF внутри candidates
5. Keyword matching: если tool упоминается по имени, добавить
6. Вернуть up to top_k tools

### `variant/agent_core.py` — Integration

**Imports:**
```python
from routing import RouterState
from tool_retriever import hybrid_select_tool_names
```

**BasicLoopSession изменения:**
- `__init__`: добавить `self.router_state = RouterState()`
- `reset()`: сбросить `self.router_state = RouterState()`
- Новый метод `_select_tools_hybrid()` который вызывает `hybrid_select_tool_names`

**run_turn() изменение:**
```python
# Было:
turn_openai_tools = self.resources.select_openai_tools_for_turn(user_message, ...)

# Стало:
turn_openai_tools = self._select_tools_hybrid(user_message, ...)
```

### `variant/config.yml`

```yaml
tool_selection:
  enabled: true
  top_k: 5
  mandatory:
    - get_policy_fact
    - knowledge_index_lookup
  deny: []
```

## Тесты

### `variant/tests/test_routing.py` (новый файл)

- `test_domain_keywords_matching` — keyword matching для каждого домена
- `test_router_state_sticky` — activated_domains остаются между ходами
- `test_route_turn_returns_correct_tools` — правильные tools в allowed_tools
- `test_decoy_always_excluded` — decoy_* исключаются
- `test_mandatory_tools_always_included` — mandatory tools в result

### `variant/tests/test_tool_retriever.py` (обновлён)

- `test_hybrid_select_with_routing` — routing + semantic
- `test_hybrid_with_keyword_match` — явно названные tools
- `test_hybrid_fallback_when_routing_empty` — fallback если routing ничего не разрешил

## Метрики (Expected)

| Метрика | v1 (Pure) | v2 (Hybrid) |
|---------|-----------|------------|
| SR | 11% | 20-25% |
| TSA | 56% | 85-90% |
| Tokens (tools) | 4500 | 1200-1500 |
| Latency | 0.96s | 0.80s |

## Почему Hybrid лучше

1. **Routing гарантирует candidate list** — нужный tool будет в 5-8 кандидатов, а не потеряется в 25
2. **Semantic внутри домена точнее** — когда выбираем из 5 employee tools, а не 25 смешанных
3. **Keyword matching ловит явные упоминания** — если в запросе сказано "employee_lookup", он в топе
4. **Decoy фильтруются рано** — routing исключает их, не нужно полагаться на semantic similarity
5. **Sticky routing** — если один раз активировали HR домен, он остаётся на всю сессию

## Файлы структура

```
improvements/08-semantic-tool-selection/variant/
├── routing.py                    ← ВСЁ НОВОЕ
├── tool_retriever.py             ← Обновлён: добавлена hybrid_select_tool_names
├── agent_core.py                 ← Обновлён: router_state, _select_tools_hybrid
├── tests/
│   ├── test_routing.py          ← ВСЁ НОВОЕ
│   └── test_tool_retriever.py   ← Обновлены тесты для hybrid
└── config.yml                    ← (без изменений)
```

## Как запустить тесты

```bash
cd improvements/08-semantic-tool-selection/variant
PYTHONPATH=. python -m pytest tests/ -v
```

---

## v3 fix: semantic top-k резал уже routing-approved tools — TSA 96%→67%

### Broken behavior (measured)

Реальный e2e-прогон на полном 189-scenario каталоге дал:

```
baseline:      solve=69% critical=58% turn=88% TSA=96%(420/439)
08 (measured): solve=50% critical=51% turn=68% TSA=67%(294/439) TAA=60%(165/277)
```

TSA 96%→67% (-29pp) и TAA ~91%→60% (-31pp) на технике, чья единственная
задача — выбрать правильный tool. Это далеко за пределами измеренного
5-9pp run-to-run шума на этом размере каталога.

### Root cause: двойная обрезка — routing уже сузил список, semantic резал его ЕЩЁ РАЗ до top_k=5

`hybrid_select_tool_names()` (v2, `variant/tool_retriever.py`) делал:

```python
# Было (v2):
semantic_scores = retriever.search(query, k=len(candidate_tools))
for name, score in semantic_scores:
    if name in candidate_tools and name not in seen:
        selected.append(name)
        seen.add(name)
        if len(selected) >= config.top_k + len(config.mandatory):
            break
```

Проблема в том, что `Domain.CORE` (`routing.py`) сам по себе содержит
**12 tools**, доступных ВСЕГДА, независимо от домена. `config.top_k=5` +
mandatory(2) = максимум 7 tools проходят этот cut — то есть даже без единого
активного не-CORE домена уже минимум 5 из 12 CORE tools гарантированно
теряются.

Хуже того, `TfidfToolRetriever` без стемминга: русская морфология
("отдела" / "отделе" / "отдел" — разные токены) и смешанные RU/EN запросы
("Покажи структуру **отдела** Engineering.") часто дают score=0.0 для
ВСЕХ candidate tools одновременно — offline-проверка показала: для этого
конкретного запроса **все 25 tools** (включая правильный `org_chart_dept`)
получили cosine similarity `0.0000`. При равных нулевых score `list.sort()`
(stable sort) сохраняет исходный alphabetically-отсортированный порядок
корпуса — и `top_k`-cut отдаёт предпочтение alphabetически более ранним
CORE tools (`benchmark_probe`, `calc_expression`, ...), вырезая нужный
`org_chart_dept`/`employee_lookup`/`sales_quote`/`invoice_get` просто по
алфавиту, а не по релевантности.

Отдельно: `graph_query` (tool из 14-graph-rag) не был приписан **ни к
одному** домену в `DOMAIN_TOOLS` — routing никогда его не разрешал,
независимо от semantic score.

И третий класс: несколько keyword-gaps в `DOMAIN_KEYWORDS` — HR требовал
`"hr-"` (с дефисом), не матчил голое `"HR"`; не было `"профиль"`/`"штат"`;
CRM не матчил `"КП"` (аббревиатура "коммерческое предложение"),
`"карточка"/"карточку"`, `"CUST-"`.

### Verification (offline, no LLM)

Скрипт: пройти `benchmark/scenarios/**/*.yaml` (все файлы, включая
`catalog/` и `graph_rag.yaml`/`memory_recall.yaml`/...), для каждого turn с
`expect_tool_called` вызвать `hybrid_select_tool_names()` напрямую (без
LLM) и проверить, остался ли ожидаемый tool в шортлисте.

```
Before (v2, top_k cut poверх routing):
  Total ground-truth-tool turns: 501
  Hits: 418  Misses: 83  Miss rate: 16.6%
  Miss counts: org_chart_dept=23, employee_lookup=20, sales_quote=9,
               invoice_get=9, empty_search=5, flaky_tool=3, customer_get=3,
               weather_city=3, leave_balance=3, graph_query=2,
               translate_text=1, calc_expression=1, sse_audit_log=1

After (v3, routing keyword fixes + no double semantic cut):
  Total ground-truth-tool turns: 501
  Hits: 498  Misses: 3  Miss rate: 0.6%
  Miss counts: employee_lookup=3
    (s08_002 "Найди дежурного Dmitry Sokolov — он берёт инцидент",
     s08_004 "Найди на месте James Chen — наш контакт в Сингапуре",
     s08_006 "Найди офицера безопасности Olga Smirnova")
```

16.6% miss rate closely explains the measured TSA collapse (96%→67% is a
29pp regression measured end-to-end through the LLM; a 16.6% pre-filter
exclusion rate on tool-selection turns, compounded with the LLM sometimes
recovering via context and sometimes not, is consistent with that gap).

### Fix

1. **Не резать routing-approved candidates семантикой второй раз.**
   `hybrid_select_tool_names()` теперь включает ВСЕ tools, разрешённые
   routing (Tier-1 keyword filter уже достаточен), и использует TF-IDF
   только как safety-cap для явно раздутых sticky-multi-domain сессий
   (`cap = top_k * 4`, т.е. ощутимо шире, чем полный CORE + 1 домен):

   ```python
   remaining = [n for n in candidate_tools if n not in seen]
   cap = max(config.top_k, 1) * 4
   if len(remaining) <= cap:
       selected.extend(remaining)
   else:
       # semantic score используется только для порядка/подрезки хвоста
       ...
   ```

2. **`graph_query` добавлен в `Domain.CORE`** (`routing.py`) — у него не
   было домена вообще, независимо от semantic score routing никогда его
   не пропускал.

3. **Keyword-gaps в `DOMAIN_KEYWORDS` закрыты:**
   - HR: `"hr"` (голое, было только `"hr-"` с дефисом), `"профиль"`, `"штат"`
   - CRM: `"карточк"` (покрывает "карточка/карточку/карточки"),
     `"коммерческое предложение"`, `"кп"`, `"cust-"`
   - Короткие/неоднозначные keywords (`len <= 3`, напр. `"hr"`, `"кп"`)
     теперь матчатся только по `\b`-границе слова (`_keyword_matches()`),
     чтобы не ловить случайные подстроки внутри других слов
     (regression test: `test_short_keyword_hr_does_not_false_positive_on_substring`).

### Остаточный (честно неисправленный) miss rate

**0.6% (3 из 501)** — все три случая (`s08_002`, `s08_004`, `s08_006`)
не содержат вообще НИКАКОГО keyword-сигнала домена: "Найди дежурного
Dmitry Sokolov", "Найди на месте James Chen", "Найди офицера безопасности
Olga Smirnova" — только имя человека, без слов employee/HR/отдел/сотрудник/
профиль/штат/etc. Keyword-based Tier-1 routing принципиально не может
закрыть это без NER (распознавание "это имя человека → скорее всего HR")
или LLM-классификации домена (Tier-2) — что осознанно не добавлено, чтобы
не повторить latency-регрессию из 13-buddy-system (безусловный LLM-вызов
на каждый ход). Не округляем это до "исправлено" — 3 из 501 остаются miss.

Ожидается: ~25 тестов, все passing.
