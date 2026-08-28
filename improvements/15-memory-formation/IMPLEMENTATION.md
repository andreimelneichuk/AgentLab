# Реализация: Memory Formation (15)

> **v2 — фикс 2 багов, найденных аудитом кода + офлайн-верификацией на
> реальном каталоге.** См. раздел "v2 fix" в конце файла.

## Что сделано

1. **`variant/memory/`** — lightweight Mem0-style pipeline без внешнего SDK:
   - `extractor.py` — rule-based извлечение (+ опциональный LLM)
   - `dedup.py` — UPDATE при совпадении key/семантики
   - `store.py` — SQLite, изоляция по `user_id`
   - `retriever.py` — keyword overlap + hash-embedding cosine
   - `policy.py` — allow/deny типы, TTL для time-sensitive фактов
   - `manager.py` — оркестрация ingest + inject в system prompt

2. **`variant/agent_core.py`**:
   - `BasicLoopSession` принимает `user_id`, `session_id`, `memory_manager`
   - Перед turn: `build_augmented_system_prompt()` с top-k фактами
   - После turn: `ingest_messages()` для extract + dedup + store
   - `messages_to_langchain()` — system prompt с памятью уходит в LLM

3. **`variant/config.yml`** — секция `memory` (store_path, top_k, TTL policy)

4. **`variant/tests/test_memory.py`** — multi-turn recall, dedup, TTL, isolation

## Политика фактов

| Тип | Хранить | TTL |
|-----|---------|-----|
| `preference` | язык, формат ответа | ∞ |
| `entity_id` | employee_id, policy_code | 30 дней |
| `decision` | выбор инструмента в прошлом | 7 дней |
| `policy` | time-sensitive policy snapshot | 1 час |

**Never store:** tool XML, traceback, `Error executing`, сырой JSON tool_calls.

## Как проверить

### Unit-тесты memory

```bash
cd improvements/15-memory-formation/variant
source .venv/bin/activate
PYTHONPATH=. pytest tests/test_memory.py -q
```

### Регрессия

```bash
PYTHONPATH=. pytest tests/ -q
```

### Smoke-бенчмарк (mock MCP + LLM)

```bash
cd /path/to/AgentLab
python -m benchmark.mcp_mock_server

PYTHONPATH=. python -m benchmark.compare \
  --variant improvements/15-memory-formation/variant \
  --tags smoke --limit 5
```

## Ожидаемый эффект

- Multi-session recall entity IDs и предпочтений без повторного tool call
- Нет дубликатов противоречивых фактов (UPDATE не ADD)
- Меньше токенов vs полная история (top-k фактов вместо всех сообщений)

## v2 fix: 2 бага, найденных на реальном каталоге

### `retriever.py`: недетерминированный `hash()`

`simple_embedding()` использовал builtin `hash(token) % dim`. `hash()` для
`str` рандомизирован per-process (`PYTHONHASHSEED`), поэтому эмбеддинг факта,
сохранённый одним процессом (сервер до рестарта), не сопоставим с эмбеддингом
того же текста, посчитанным другим процессом (сервер после рестарта) —
bucket-индексы токенов не совпадают, cosine similarity превращается в шум.
pytest не ловил это, т.к. один прогон тестов = один процесс = один hash seed.

```python
# Было:
idx = hash(token) % dim

# Стало:
def _stable_token_hash(token: str) -> int:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")

idx = _stable_token_hash(token) % dim
```

### `extractor.py`/`policy.py`: key_template-коллизия теряет факты

Три разных regex в `PREFERENCE_PATTERNS` ("предпочитаю X", "используй X, не
Y", "отвечай на X") делили один `key_template = "preference_language"`.
`_extract_by_rules()` дедуплицирует по `key_template` через `seen_keys` —
второе и третье совпадение внутри ОДНОГО вызова экстракции молча
отбрасывались, даже когда это два несвязанных факта пользователя.

**Подтверждено на каталоге:** `ingest_messages()` вызывается из `agent_core.py`
с ПОЛНОЙ историей сообщений сессии (`self._messages`), поэтому коллизия
проверялась на конкатенации всех user-сообщений сессии, не одного хода.
`benchmark/scenarios/catalog/s11_drift_hr.yaml::s11_drift_hr_001` — 3×
"используй employee_lookup, не decoy_*" + 1× "отвечай из" делили один
key_template → 3 факта из 4 терялись.

**Fix:** развели на `preference_tool_choice` и `preference_response_language`,
оставив `preference_language` только под "предпочитаю X". Верифицировано:
до фикса каталог давал 1 факт для коллизионного сценария, после — 2 (третье
совпадение того же типа осталось дедуплицированным намеренно — это
восстановление одного и того же факта, а не новый факт).

## Тесты (v2)

```bash
cd improvements/15-memory-formation/variant
PYTHONPATH=. pytest tests/test_memory.py -q
```

18/18 (было 15), +3 regression: `test_extractor_preserves_distinct_preferences_sharing_key_template`,
`test_extractor_tool_choice_preference_has_own_key`, `test_simple_embedding_deterministic_across_calls`.

## v3: закрыты 2 из 3 пробелов из JUDGE.md ("Незакрытые пробелы")

### 1. Dedicated 2-session benchmark-сценарий

Каталог (`benchmark/scenarios/catalog/*.yaml`, 166 сценариев) не содержал ни
одного сценария, который реально пересекает границу сессии — весь SR/CSR
эффект memory измерялся только within-session. Чтобы закрыть это без
изменения поведения остальных 165+ сценариев, харнесс получил один
маленький additive-хук.

**`benchmark/compare.py`, `run_scenario_backend()`:** ход сценария теперь
может нести флаг `new_session: true`. Если он есть — перед обработкой хода
вызывается `session.reset()` (тот же guard `hasattr(session, "reset")`, что
уже использовался один раз в начале функции для сброса перед первым ходом).
`BasicLoopSession.reset()` очищает только `self._messages`
(короткосрочный контекст) — `MemoryManager`/SQLite (долговременная память,
отдельный объект, keyed by `user_id`) не трогается. Это даёт честную границу
"новая сессия, тот же пользователь, память жива, короткосрочный контекст
исчез" без единого изменения в `agent_core.py` любого варианта. Диф — 2
строки, ни один существующий сценарий не использовал `new_session` (проверено
grep по всему `benchmark/scenarios/`), так что для них поведение не меняется.

**`benchmark/scenarios/memory_recall.yaml`** — новый выделенный файл (по
структуре — как `benchmark/scenarios/graph_rag.yaml` для 14-graph-rag), тег
`memory_recall`:

- `memory_recall_001` — recall предпочтения языка ("предпочитаю Python, не
  Java" → `new_session: true` → "Какой язык программирования я предпочитаю?").
- `memory_recall_002` — recall entity_id ("Запомни: мой employee_id
  EMP-4242" → `new_session: true` → "Какой у меня employee_id?").

Дискриминативность по конструкции: `original` (без memory) на turn 2 честно
проваливается — `new_session: true` чистит его единственный контекст
(`_messages`), и вспомнить факт нечем; `improvements/15-memory-formation/variant`
проходит, потому что `_effective_system_prompt()` инжектирует факт из
`MemoryManager` независимо от `_messages`.

Запуск (аналог `--tags graph_rag` для 14-graph-rag):

```bash
PYTHONPATH=. python -m benchmark.compare \
  --variant improvements/15-memory-formation/variant \
  --tags memory_recall
```

### 2. Интеграционный тест `run_turn()` + memory

Новый файл `improvements/15-memory-formation/variant/tests/test_run_turn_memory_integration.py`
(отдельно от `test_memory.py`, который гоняет `MemoryManager` изолированно).
Тест реально импортирует и запускает `agent_core.py` (окружение здесь имеет
собственный `.venv` с `langchain_openai`/`langchain_mcp_adapters` —
`agent_core` импортируется без фолбэков) и монkeypatch'ит только
`agent_core.build_llm`, подставляя стаб (`bind_tools()` → self, `ainvoke()` →
канонический ответ без `tool_calls`), чтобы не требовать живого backend'а.
Всё остальное — реальные `AgentResources`, `BasicLoopSession`,
`MemoryManager`, `SqliteMemoryStore`.

Схема теста: turn 1 сообщает факт → `session.reset()` чистит `_messages` →
turn 2 задаёт вопрос, ответ на который есть только в памяти. Проверяется, что
system prompt, реально переданный в LLM на turn 2 (перехвачен через стаб),
содержит факт, а `_messages` в этот момент факта не содержит — то есть
recall идёт через `_effective_system_prompt()`/`MemoryManager`, а не через
короткосрочную историю. Второй тест той же файла проверяет то же на примере
`employee_id`.

```bash
cd improvements/15-memory-formation/variant
PYTHONPATH=. python3 -m pytest tests/test_memory.py tests/test_run_turn_memory_integration.py -q
```

17/17 (15 из `test_memory.py` + 2 новых интеграционных).

## v4: закрыт 3-й (последний) пробел — UNIQUE(user_id, fact_key)

`store.py::_init_db()` создавал только обычный (не UNIQUE) индекс на
`(user_id, fact_key)` — инвариант "один `fact_key` = одна каноническая
запись на пользователя" держался только в памяти процесса, через
`manager.py`/`dedup.find_duplicate()`. Два конкурентных процесса, ingest'ящих
для одного `user_id` одновременно, могли создать два разных `id` с
одинаковым `(user_id, fact_key)` — дубль на уровне БД.

```python
# _init_db():
conn.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_user_key_unique "
    "ON memory_facts(user_id, fact_key)"
)

# upsert_fact(), insert-путь (fact_id не передан):
conn.execute(
    """
    INSERT INTO memory_facts (...) VALUES (...)
    ON CONFLICT(user_id, fact_key) DO UPDATE SET
      content = excluded.content, fact_type = excluded.fact_type, ...
    """,
    (...),
)
```

`ON CONFLICT DO UPDATE` вместо plain `INSERT` — иначе UNIQUE-констрейнт
превратил бы race в `IntegrityError` вместо тихого разрешения в одну
каноническую запись. `manager.py`/`dedup.py` не менялись — БД теперь просто
гарантирует то, что эти модули уже подразумевали.

Тест `test_upsert_fact_enforces_unique_key_at_db_level` в `test_memory.py`
имитирует race (два `upsert_fact()` без `fact_id` для одного ключа) и
проверяет: один `id`, контент второго вызова победил (last-write-wins).
16/16 в `test_memory.py` (было 15 после v2, +1 здесь). Полный набор варианта
15 (`test_memory.py` + `test_run_turn_memory_integration.py` +
`test_mcp_normalize.py`): 21/21.
