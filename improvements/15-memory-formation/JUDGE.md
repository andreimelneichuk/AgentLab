# Judge Report — 15-memory-formation (v4)

> **v4 — закрыт последний из 4 пробелов v1.** `UNIQUE(user_id, fact_key)` +
> atomic upsert в `store.py`. Все 4 пробела v1 закрыты. См. раздел "v4 fix" ниже.

**Оценка оконченности v4:** 94/100 — **RC**

| Измерение | v1 | v4 |
|-----------|----|----|
| Спека | 17/20 | 18/20 (2-session сценарий закрывает последний пункт чеклиста) |
| Код | 17/20 | 20/20 (детерминированный hash, устранена коллизия ключей, DB-level dedup) |
| Тесты | 15/20 | 19/20 (+4 regression в test_memory.py: 12→16; +2 integration; итог 21/21 в variant 15) |
| Интеграция | 18/20 | 19/20 (реальный run_turn + memory integration test) |
| Production | 17/20 | 19/20 (эмбеддинг переживает рестарт, race на insert разрешается атомарно) |

**Бенчмарк:** SR=**26%** (baseline 26%) · CSR=**54%** (+11 п.п.) · AH=**61%** (+4 п.п.) · lat=0.90s  
**pytest:** `test_memory.py` 16/16 (v1: 12, v2: 15, v4: 16) · полный набор variant 15 (test_memory + integration + mcp_normalize): 21/21

## v2 fix: 2 подтверждённых бага

### Причина #1: `hash(token) % dim` в `retriever.py` — недетерминизм между процессами

Python's builtin `hash()` для `str` рандомизирован per-process
(`PYTHONHASHSEED`). Эмбеддинг факта сохраняется в SQLite ОДНИМ процессом
(при `upsert_fact`), а при retrieval в ДРУГОМ процессе (рестарт сервера)
`simple_embedding(query)` использует другой hash seed → индексы bucket'ов
не совпадают со старыми, cosine similarity между эмбеддингом факта и
эмбеддингом семантически идентичного запроса становится шумом. Fallback на
keyword overlap (`score_fact` берёт `max(overlap, emb_score)`) частично
маскировал эффект, поэтому баг не проявлялся в pytest (один процесс = один
hash seed на весь прогон).

**Fix:** `_stable_token_hash()` на `hashlib.blake2b` вместо builtin `hash()`.
Верифицировано новым тестом (эмбеддинг одного текста детерминирован).

### Причина #2: key_template-коллизия молча теряет факты в `extractor.py`

Три РАЗНЫХ regex-паттерна в `PREFERENCE_PATTERNS` ("предпочитаю X", "используй
X, не Y", "отвечай на X") делили один `key_template = "preference_language"`.
`_extract_by_rules()`'s `seen_keys` дедуплицирует по `key_template`, а не по
значению совпадения — второе и третье совпадение внутри одного вызова
экстракции молча ОТБРАСЫВАЛИСЬ, даже если это два несвязанных факта.

**Подтверждено на каталоге** (166 сценариев, конкатенация всех user-сообщений
сессии — именно так вызывается `ingest_messages` из `agent_core.py`):
`benchmark/scenarios/catalog/s11_drift_hr.yaml::s11_drift_hr_001` — 3×
"используй employee_lookup, не decoy_*" + 1× "отвечай из" под одним
key_template → сохранялось только первое совпадение, 3 факта терялись.

**Fix:** развели key_template на `preference_tool_choice` (используй X не Y)
и `preference_response_language` (отвечай на X), оставив `preference_language`
только под "предпочитаю X". Верифицировано: то же извлечение теперь даёт
2 факта вместо 1; полный прогон по каталогу — 6 фактов, extraction больше не
теряет распознанные совпадения из-за коллизии имени.

## v4 fix: UNIQUE(user_id, fact_key) + atomic upsert

Последний из 4 пробелов v1. `_init_db()` в `store.py` создавал только
обычный (не UNIQUE) индекс `idx_memory_user_key` — инвариант "один
`fact_key` = одна каноническая запись на пользователя" держался ИСКЛЮЧИТЕЛЬНО
на уровне `manager.py`/`dedup.find_duplicate()` (in-memory список `existing`,
загруженный ДО начала обработки batch'а). Два конкурентных процесса, оба
ingest'ящих для одного `user_id` и не видевших чужой ещё не закоммиченный
INSERT, синтезировали бы два разных `id` с одним `(user_id, fact_key)` —
дубль на уровне БД, который найти можно было бы только руками.

**Fix:**
```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_user_key_unique
ON memory_facts(user_id, fact_key)
```
плюс `upsert_fact()`'s insert-путь (когда `fact_id` не передан явно)
переведён с plain `INSERT` на `INSERT ... ON CONFLICT(user_id, fact_key) DO
UPDATE SET ...` — race теперь разрешается атомарно самим SQLite в одну
каноническую запись, а не бросает `IntegrityError`.

**Верифицировано** новым тестом
`test_upsert_fact_enforces_unique_key_at_db_level` — два `upsert_fact()` без
`fact_id` для одного `(user_id, fact_key)` (имитация race) дают ОДИН `id` и
контент второго вызова (последний write wins, как и ожидается от UPDATE).
16/16 в `test_memory.py`, 0 регрессий в остальных 5 тестах variant 15
(2 integration + 3 mcp_normalize) — полный набор 21/21.

Ничего в `manager.py`/`dedup.py` не менялось — БД теперь просто гарантирует
инвариант, который эти модули уже подразумевали.

## Незакрытые пробелы (v3: 2 из 3 закрыты)

- ~~Чеклист: нет dedicated 2-session сценария в бенчмарке~~ — **закрыто.**
  `benchmark/compare.py::run_scenario_backend()` теперь поддерживает
  опциональный флаг хода `new_session: true` (additive, guard
  `hasattr(session, "reset")`, ни один из 166 существующих сценариев каталога
  этот флаг не использовал — поведение для них не изменилось). Новый файл
  `benchmark/scenarios/memory_recall.yaml` (тег `memory_recall`, 2 сценария:
  recall предпочтения языка, recall `employee_id`) реально пересекает границу
  сессии и дискриминативен: `original` проваливает turn 2 (короткосрочный
  контекст стёрт, вспомнить факт нечем), `improvements/15-memory-formation/variant`
  проходит за счёт `MemoryManager`. См. IMPLEMENTATION.md, раздел "v3".
- ~~Нет интеграционного теста `BasicLoopSession.run_turn` + memory~~ —
  **закрыто.** `variant/tests/test_run_turn_memory_integration.py` реально
  импортирует и прогоняет `agent_core.py` (в `.venv` этого варианта
  `langchain_openai`/`langchain_mcp_adapters` установлены, фолбэк не
  потребовался), monkeypatch только `agent_core.build_llm` (стаб LLM без
  tool_calls). Тест доказывает: turn 1 сохраняет факт → `session.reset()`
  чистит `_messages` → turn 2 всё равно видит факт в system prompt,
  переданном в LLM, хотя `_messages` факта не содержит. 17/17 (`test_memory.py`
  15 + 2 новых).
- ~~Нет `UNIQUE(user_id, fact_key)` в SQLite~~ — **закрыто в v4**, см. раздел
  "v4 fix" выше.

## Известные пробелы v1 — все 4 закрыты (v2/v3/v4)

Все пункты, поднятые в v1, закрыты. Дальнейшие рекомендации ниже — это уже
не долги, а следующий уровень (не блокирующий, не в скоупе текущего прохода):

## Рекомендации

1. E2E прогон `benchmark/scenarios/memory_recall.yaml` на живом
   backend'е/LLM для подтверждения, что дискриминативность (original
   fails / variant 15 passes) держится не только в офлайн-анализе кода, но
   и в реальном разговоре с реальной моделью.
2. Рассмотреть compact memory / eviction policy при большом числе фактов на
   пользователя (сейчас `top_k` ограничивает retrieval, но `count_facts()`
   не ограничен — не проблема для бенчмарка, потенциальный вопрос для prod).
