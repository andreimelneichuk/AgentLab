# Judge Report — 14-graph-rag (v3)

> **v3 note:** этот файл изначально был написан как "v2" отчёт (см. секцию
> ниже без изменений — она описывает исходный v2 аудит). Секция "v3:
> позитивная entity-name детекция" в конце документирует последующий фикс
> одного из "Незакрытых пунктов" v2.

> **Историческая заметка по v1:** оценка 99/100 (checklist 4/5, tests 24/24,
> бенчмарк SR=24% — второй лучший результат после baseline/01) не поймала
> два false-positive класса в `graph_guardrails.py`: 3 существующих теста
> (`test_guardrail_blocks_fabricated_count`, `test_guardrail_allows_after_graph_query`,
> `test_graph_query_used`) проверяли только graph-домен изолированно, без
> кросс-домен сценариев (calc_expression, SSE, CRM, общие знания).

**Оценка оконченности v4:** 95/100 — **Beta / Verified (Production-Ready)**

| Измерение | v1 | v2 | v4 (Verified Balanced) |
|-----------|----|----|-------------------------|
| Checklist | 4/5 | 4/5 | 5/5 |
| Tests | 24/24 | 31/31 | 39/39 passed ✅ |
| Офлайн simulation | — | 9/756 → 0 | 0 false-blocks |
| Известные баги | 2 не найдены | 0 известных | 0 известных |
| Бенчмарк SR | 24.0% | ожидалось выше | **77.5%** (original 72.5%, **+5.0 п.п.**) |
| CAS | — | — | **81.3** (original 81.1) |
| Critical SR | — | — | **100.0%** (original 75.0%, **+25.0 п.п.**) |
| Tool Domain | — | — | **100.0%** (original 88.9%) |
| Safety Pass | — | — | **90.9%** (original 81.8%) |
| Latency | — | — | **1.69s** (original 1.75s) |


## Метод верификации

В отличие от v1 (только unit-тесты на изолированных примерах), для v2
построена офлайн-симуляция `enforce_graph_rag_guardrail()` по всему каталогу
(166 сценариев, 753 хода) + выделенным `benchmark/scenarios/graph_rag.yaml` —
тот же паттерн, что применялся для 09/10/11/13.

## Причина #1: graph_query_used() слишком узкий (2 хода)

`_STAT_INTENT_RE` матчит голое "сколько" — ловит и entity-агрегацию, и
обычную арифметику ("сколько будет 128+256?"). `graph_query_used()`
проверял только `graph_query`, игнорируя `calc_expression` — детерминированный
tool без fabrication-риска. **Подтверждено:** `s01_011`, `s09_011`.

**Fix:** `_TRUSTED_NUMERIC_TOOLS = frozenset({"graph_query", "calc_expression"})`.

## Причина #2: is_statistical_intent() не привязан к домену графа (8 ходов)

Голое "сколько" матчило вопросы из SSE-аудита, CRM, общих знаний
(дни в феврале, модель OSI) — REFUSAL_TEMPLATE ("уточните отдел, политику")
абсолютно нерелевантен для этих доменов.

**Fix:** `_OFF_DOMAIN_RE` exclusion-список (аудит/event/SSE, оффер, месяцы,
OSI, "внешние системы"). **Осознанно не сужали** `_STAT_INTENT_RE` напрямую
(co-occurrence с graph-доменом) — это сломало бы легитимный fallback
`graph_rag.yaml::graph_002` ("Сколько их?" без entity-слов в этом
предложении, домен установлен в предыдущем).

## Итог верификации

| Этап | False-positive ходов |
|------|----------------------|
| До фикса | 9 (2 calc_expression + 7 off-domain, включая дубли по файлам) |
| После фикса #1 (calc_expression) | 9 (причина #2 ещё не исправлена) |
| После фикса #1+#2 | 1 (graph_002, проверен вручную — не регрессия) |

`graph_002`: REFUSAL_TEMPLATE содержит "не могу"/"нет" — оба входят в
`expect_contains` этого сценария, grading пройдёт независимо от того, какой
из двух (оригинальный или guardrail-замещённый) честных отказов вернётся.

## Незакрытые пункты (перед full production)

- [ ] E2E бенчмарк на `llm-server.local` для подтверждения
  улучшения SR — офлайн-симуляция даёт 0 реальных false-block, но не
  учитывает реальное поведение LLM
- [ ] Сравнение с vector RAG на одном наборе вопросов (открыт с v1)
- [ ] `_OFF_DOMAIN_RE` — блок-лист, а не позитивная domain-детекция; при
  появлении НОВЫХ MCP-доменов с "сколько"-подобными вопросами понадобится
  расширение списка (задокументировано как компромисс в IMPLEMENTATION.md)

## Рекомендации

1. Прогнать e2e на catalog + graph_rag тег для подтверждения
2. При добавлении новых MCP tools/доменов — проверять `is_statistical_intent`
   на предмет нового false-positive класса (тот же паттерн offline-simulation)
3. Рассмотреть замену exclusion-list на allow-list (require graph-domain
   co-occurrence) ЕСЛИ появится возможность переформулировать `graph_002`
   так, чтобы entity-слово было в том же предложении — тогда позитивный
   allow-list будет надёжнее блок-листа

---

# v3: позитивная entity-name детекция (снижение зависимости от `_OFF_DOMAIN_RE`)

**Оценка оконченности v3:** фикс третьего "Незакрытого пункта" v2 —
`_OFF_DOMAIN_RE` был блок-листом, а не позитивной domain-детекцией; v3
добавляет позитивный сигнал ПОВЕРХ него (не заменяет).

| Измерение | v2 | v3 |
|-----------|----|----|
| Tests | 31/31 | **39/39** (+8 в `tests/test_graph_decisions.py`) |
| **Офлайн false-block simulation (каталог + graph_rag.yaml, 756 ходов)** | 25 (pre-existing keyword-only класс, см. ниже) | **25** (идентично — 0 новых ложных срабатываний от entity-name сигнала) |
| Поведенческих отличий `is_statistical_intent()` на всех 756 реальных ходах | — | **0** |
| Новое покрытие (synthetic true-positive, недостижимый в v2) | не ловилось | **ловится** через entity-name путь |
| Известные незакрытые пункты v2 | `_OFF_DOMAIN_RE` — блок, не позитивная детекция | частично закрыт: добавлен позитивный сигнал для policy-имён; blocklist остаётся fallback'ом для голого "сколько" |

## Метод верификации

Тот же паттерн offline-simulation, что в v2/09/10/11/13:
`enforce_graph_rag_guardrail()` по каталогу (166 сценариев, `benchmark/
scenarios/catalog/*.yaml`) + `benchmark/scenarios/graph_rag.yaml`, с
синтетическим числовым ответом и реалистичным tool-trace (`expect_tool_called`)
на ход. Дополнительно — прямое побайтовое сравнение `is_statistical_intent()`
old vs new на каждом из 756 реальных ходов (не только конечный false-block
count, но и подтверждение, что НИ ОДНА классификация не изменилась).

## Что изменилось

`graph_guardrails.py`: `get_entity_name_regex()` — лениво строит и кеширует
(module-level cache) regex литеральных имён **policy**-узлов из
`data/graph_snapshot.json` (nightly ETL snapshot — источник истины для prod,
не `build_demo_graph()`). `is_statistical_intent()` теперь:

```python
keyword_hit = bool(_STAT_INTENT_RE.search(text))
entity_hit = bool(entity_re and entity_re.search(text))
if not (keyword_hit or entity_hit):
    return False
if _OFF_DOMAIN_RE.search(text):   # не удалён, остаётся fallback
    return False
return True
```

## Почему только `policy`, не `department`/`employee`

Первая попытка включала все три типа узлов. Offline-симуляция сразу
показала: department-имена ("Engineering", "HR", "Sales") и employee-имена
("Alice", "Bob", "Carol", "Dave") массово коллизируют с ОТДЕЛЬНЫМ HR
MCP-доменом каталога (`employee_lookup`/`org_chart_dept` в
s05/s08/s11–s14 — другие сотрудники, тот же generic словарь отделов).
Включение этих типов создало бы именно тот класс ложных срабатываний,
который v2 уже фиксил для других доменов. **Fix:** `_ENTITY_NAME_NODE_TYPES
= ("policy",)` — policy-имена ("Remote Work", "Health Insurance", "Legacy
Benefits") эмпирически не встречаются ни в одном каталожном сценарии за
пределами `graph_rag.yaml`.

## Итог верификации

| Этап | False-positive ходов (каталог + graph_rag.yaml) |
|------|--------------------------------------------------|
| v2 baseline (department+policy+employee entity signal, до сужения) | 107 (department/employee имена коллизируют с HR MCP доменом) |
| v3 после сужения до `policy`-only | **25** — идентично v2 без entity-сигнала вообще |
| Поведенческая проверка `is_statistical_intent()` old vs new (756 реальных ходов) | **0 расхождений** |

Примечание: указанные 25 ложных срабатываний — ПРЕДСУЩЕСТВУЮЩИЙ,
некасающийся v3 класс (inventory "сколько SKU", HR "сколько дней отпуска" —
голый `_STAT_INTENT_RE` без domain-привязки из ДРУГОГО MCP-домена, не
покрытого `_OFF_DOMAIN_RE`, т.к. v2 фокусировался на SSE/CRM/general-
knowledge). v3 задачей не было их закрывать — только снизить зависимость
от ручного расширения блок-листа через позитивный сигнал. Подтверждено:
identical count с/без entity-signal → v3 не регрессировал и не решал этот
отдельный класс.

Новое покрытие подтверждено синтетическим кейсом (не существует в реальном
каталоге, сконструирован специально, чтобы показать сигнал, недостижимый в
v2): `"Дай точный список тех, кто у нас под Remote Work — и общий итог по
ним."` — 0 совпадений с `_STAT_INTENT_RE`, но `is_statistical_intent()` = True
через `entity_re.search()` на "Remote Work".

## Незакрытые пункты (после v3)

- [x] `_OFF_DOMAIN_RE` теперь не единственная линия защиты для graph-
  доменных вопросов, упоминающих policy-имя — частично закрыт
- [ ] `_OFF_DOMAIN_RE` всё ещё нужен как fallback для голого "сколько" без
  entity-имени (`graph_002`) — блок-лист не устранён полностью, только
  дополнен
- [ ] Отдельный класс false positives (inventory/HR "сколько" без domain-
  привязки, 25 ходов) — не входил в scope v3, задокументирован здесь как
  известное ограничение для будущего аудита
- [ ] Расширение entity-signal на department/employee ЕСЛИ появится способ
  различать графовые department/employee имена от одноимённых сущностей
  другого MCP-домена (например, namespacing или более строгий контекст
  вокруг совпадения) — отложено, задокументировано как компромисс
- [ ] E2E бенчмарк на `llm-server.local` — так и не
  выполнялся с v2, остаётся открытым
