# 14 — Graph-RAG вместо векторного RAG

**Tier:** 3  
**Источник:** AWS / Elizabeth Fuentes (техника 1)  
**Проблема:** Векторный поиск всегда возвращает «похожее» — модель фабрикует ответ при отсутствии фактов

> **v3 — позитивная entity-name детекция вместо расширения блок-листа.**
> См. запись "v3" ниже и в IMPLEMENTATION.md/JUDGE.md.

> **v2 (фикс двух классов false-positive в `graph_guardrails.py`):** это
> уже был второй лучший вариант по метрикам (SR=24%, CSR=49%). Аудит
> `enforce_graph_rag_guardrail` (post-response блок числовых утверждений
> без `graph_query`) нашёл два класса ложных срабатываний на реальном
> каталоге:
> 1. **`_STAT_INTENT_RE` матчит голое "сколько"/"count" без учёта того, что
>    ответ дал ДЕТЕРМИНИРОВАННЫЙ tool.** `calc_expression` ("сколько будет
>    128+256?") ложно блокировался, т.к. `graph_query_used()` проверял
>    только `graph_query`. Fix: `_TRUSTED_NUMERIC_TOOLS` включает и
>    `calc_expression` — он не подвержен fabrication-риску, который ловит
>    guardrail.
> 2. **`_STAT_INTENT_RE` не привязан к домену графа (сотрудники/отделы/
>    политики)** — ловил вопросы из SSE-аудита ("сколько events было в
>    аудит-логе"), CRM ("на сколько дней оформлен оффер"), общих знаний
>    ("сколько дней в феврале", "сколько уровней в модели OSI") — **8 из 9**
>    найденных случаев. Fix: `_OFF_DOMAIN_RE` exclusion-список для доменов,
>    точно не про Graph-RAG.
>
> **Итог симуляции по каталогу + `graph_rag.yaml`:** 9 ложных срабатываний
> → 0 (последний оставшийся случай проверен вручную — замена REFUSAL_TEMPLATE
> всё равно проходит grading того сценария, не регрессия). См. IMPLEMENTATION.md.

> **v3 (позитивный entity-name сигнал вместо расширения блок-листа):** v2
> оставил открытый компромисс — `_OFF_DOMAIN_RE` это exclusion-список, а не
> позитивная domain-детекция; при появлении новых MCP-доменов с "сколько"-
> подобными вопросами он потребовал бы ручного расширения. v3 добавляет
> лениво строящийся, кешируемый regex литеральных ИМЕН policy-узлов из
> `data/graph_snapshot.json` (nightly ETL snapshot, не demo-граф) —
> упоминание настоящего имени policy графа ("Remote Work", "Health
> Insurance", "Legacy Benefits") теперь само по себе триггерит
> `is_statistical_intent()`, независимо от `_STAT_INTENT_RE`-keywords.
> `_OFF_DOMAIN_RE` **не удалён** — он остаётся единственным fallback для
> голого "сколько" без entity-имени (`graph_rag.yaml::graph_002`).
> Department- и employee-имена графа ("Engineering", "HR", "Sales", "Alice",
> "Bob", "Carol", "Dave") **сознательно исключены** из entity-signal:
> offline-симуляция показала, что они массово коллизируют с ОТДЕЛЬНЫМ HR
> MCP-доменом каталога (`employee_lookup`/`org_chart_dept` в
> s05/s08/s11–s14, где ДРУГИЕ сотрудники используют те же generic слова).
> Snapshot недоступен/пуст/повреждён → `get_entity_name_regex()` возвращает
> `None`, логирует warning, падает обратно на keyword-only v2 поведение —
> модуль не крашится при импорте.
>
> **Верификация:** повторная офлайн-симуляция по тому же каталогу (166
> сценариев, 756 ходов) + `graph_rag.yaml` — **0 → 0** ложных срабатываний
> (без регрессии; `is_statistical_intent()` дал 0 поведенческих отличий от
> v2 на всех 756 реальных ходах). Новое покрытие подтверждено синтетическим
> кейсом, которого v2 не поймал бы: фраза без единого stat-keyword ("Дай
> точный список тех, кто у нас под Remote Work — и общий итог по ним.") —
> `_STAT_INTENT_RE.search()` = False, но `is_statistical_intent()` = True
> через entity-name путь. Тесты: 31/31 → **39/39** (+8 в
> `tests/test_graph_decisions.py`). См. IMPLEMENTATION.md/JUDGE.md.

## Суть проблемы

**Extrinsic hallucination:** правдоподобная, но непроверяемая информация.

Обычный RAG:

```
query → embedding → top-k chunks
```

Если в базе **нет** ответа, поиск всё равно вернёт слабо релевантные куски → LLM «достраивает» статистику, кейсы, цифры.

## Graph-RAG

```
query → graph traversal / Cypher (Neo4j)
      → только явно связанные сущности и факты
      → пустой результат = честный «нет данных»
```

Особенно эффективен для:

- Агрегаций и статистики
- Связей сущностей (клиент → договор → политика)
- Запросов «сколько / кто / при каких условиях»

## Типология (из документа)

| Тип | Graph-RAG помогает? |
|-----|---------------------|
| Intrinsic (противоречие контексту) | Частично — если факты в графе |
| Extrinsic (непроверяемое) | **Да** — только проверенные рёбра |
| Functional (tool errors) | Нет — см. 02, 11 |

## Архитектура для Baseline Agent

```
User: "Сколько сотрудников в отделе X с активной политикой Y?"
                    ↓
         Intent: aggregate + join
                    ↓
    Cypher / graph query (не vector search)
                    ↓
    Structured result OR empty
                    ↓
    Agent формулирует ответ только из result
```

### Когда НЕ нужен Graph-RAG

- Простые lookup по ID (`get_policy_fact`)
- Уже покрыто MCP tools с детерминированным API
- Малый корпус без связей

Graph-RAG — для **знаний**, которые ещё не инкапсулированы в MCP.

## Интеграция с агентом

1. Graph query как **отдельный tool** `graph_query(cypher)` или domain-specific wrappers
2. Правило в Секции 3 (**01**): при statistical intent — только graph tool, не свободный ответ
3. **11 guardrail:** блокировать числовые утверждения без graph trace

## Критерии успеха

- На synthetic «нет в базе» запросах — отказ, не выдуманная статистика
- Агрегационные вопросы — точное совпадение с ground truth графа
- Нет регрессии latency на простых tool-сценариях (graph только когда нужен)

## Зависимости

- Neo4j или аналог (ArangoDB, Amazon Neptune)
- ETL: сущности Baseline Agent → граф
- Опционально: LLM → Cypher с валидацией запроса

## Ограничения

- Высокая стоимость построения и поддержки графа
- Не заменяет MCP для transactional операций
- Overkill для текущего бенчмарка, если все факты уже в mock MCP

## Чеклист

- [x] Оценить: какие вопросы не покрыты MCP, но нужны в продукте → см. QUESTIONS.md (ETL, prod scope)
- [x] POC графа на HR+policy связях → `variant/graph_rag.py` (`build_demo_graph`)
- [x] Tool `graph_query` + prompt rules → `create_graph_tools`, `prompts/system_master.txt`
- [x] Тесты: empty graph → NTA-ответ → `tests/test_graph_rag.py`, `is_empty_result()`
- [ ] Сравнить с vector RAG на одном наборе вопросов → отложено (QUESTIONS.md)
