# IMPLEMENTATION — 07 Knowledge Index (v2)

> **Переработка с v1:** Вместо compression (удаления истории), Knowledge Index создаёт семантический индекс фактов. Модель может искать факт в индексе перед вызовом tool-а, экономя токены и улучшая recall-точность.

## Что реализовано

### `variant/focus.py` — семантический индекс с auto-indexing

- **`IndexEntry`** — структура для одной записи в индексе
  - `key`: что ищут ("violet-42", "HR-001")
  - `category`: тип факта ("policy", "employee", "marker")
  - `full_format`: точный ответ инструмента (для цитирования)
  - `from_tool`: откуда пришло
  - `turn_index`: когда получено
  - `confidence`: high/medium/low

- **`KnowledgeIndex`** — индекс, организованный по категориям
  ```python
  {
    "policy": {"violet-42": IndexEntry(...), ...},
    "employee": {"HR-001": IndexEntry(...), ...},
    "marker": {"orchid-17": IndexEntry(...), ...},
  }
  ```
  - `add_entry(entry)` — добавить запись
  - `lookup(category, key)` — поиск
  - `to_context_text()` — форматировать для инжекции в prompt

- **`KnowledgeIndexManager`** — управление индексом в сессии
  - `auto_index_from_tool_response(tool_name, response, turn_index)` — парсит tool ответ, автоматически добавляет в индекс
  - `handle_lookup_request(category, key)` — обработка pseudo-tool `knowledge_index_lookup` от LLM
  - `lookup()` — поиск

- **Auto-indexing категории** — `AUTO_INDEX_CATEGORIES` маппит tool-ы на (категория, маркер)
  ```python
  AUTO_INDEX_CATEGORIES = {
      "get_policy_fact": ("policy", "POLICY_FACT"),
      "employee_lookup": ("employee", "EMP_ID"),
      "benchmark_probe": ("marker", "BENCH_MARKER_STREAMABLE"),
      ...
  }
  ```

- **Pseudo-tool `knowledge_index_lookup`** — LLM может запросить факт из индекса
  - Параметры: `category`, `key`
  - Возвращает: `{found, full_format, from_tool, ...}`

### `variant/agent_core.py` — интеграция Knowledge Index

- `BasicLoopSession` имеет `self.knowledge_index` — экземпляр `KnowledgeIndexManager`
- После каждого успешного tool call вызывается `auto_index_from_tool_response()`
- Knowledge Index инжектируется в `_build_llm_messages()` → в system context (перед каждым LLM call)
- `knowledge_index_lookup` добавляется в openai_tools через `merge_openai_tools()`
- Обработка `knowledge_index_lookup` в `_execute_tools()` → вызов `manager.handle_lookup_request()`

### `variant/prompts/system_master.txt` — инструкции

**Новая секция: СЕМАНТИЧЕСКИЙ ИНДЕКС (KNOWLEDGE INDEX)**

> Для recall-задач перед вызовом инструмента поищи факт в KNOWLEDGE INDEX (есть в system context). Если найден, цитируй из `full_format` поля напрямую. Если не найден или INDEX пуст — вызови инструмент.
> Пример: recall про политику → сначала `knowledge_index_lookup(category="policy", key="violet-42")` → получи полный формат → цитируй.

### `variant/config.yml`

```yaml
knowledge_index:
  enabled: true
  # Auto-indexing включён по умолчанию
  # Каждый успешный tool call → entry в индекс
```

### `variant/tests/test_focus.py` — unit-тесты

- `test_index_entry_creation` — создание и сериализация
- `test_knowledge_index_add_lookup` — добавление и поиск entries
- `test_knowledge_index_by_category` — группировка по категориям
- `test_auto_index_from_policy_fact` — парс "POLICY_FACT=violet-42..."
- `test_auto_index_from_employee_lookup` — парс "EMP_ID=HR-001 NAME=..."
- `test_auto_index_from_benchmark_probe` — парс "BENCH_MARKER_STREAMABLE=orchid-17"
- `test_handle_lookup_request_found` — found=true случай
- `test_handle_lookup_request_not_found` — found=false случай
- `test_knowledge_index_to_context_text` — форматирование для промпта
- `test_knowledge_index_manager_integration` — весь workflow

## Как это работает (поток)

```
User: "Напомни какие факты мы узнали?"

Ход 1:
  1. LLM видит Knowledge Index в system context (пусто)
  2. LLM вызывает get_policy_fact()
  3. Tool возвращает: "POLICY_FACT=violet-42. Правило: каждый ответ должен заканчиваться [POLICY_OK]."
  4. agent_core вызывает auto_index_from_tool_response("get_policy_fact", response, turn_index=1)
  5. Индекс обновляется:
     {
       "policy": {
         "violet-42": IndexEntry(key="violet-42", full_format="POLICY_FACT=violet-42...", ...)
       }
     }
  6. LLM получает ответ, цитирует его в response

Ход 2 (то же сценарию, recall):
  User: "Напомни политику из начала"
  
  1. LLM видит Knowledge Index (уже содержит violet-42)
  2. LLM вызывает knowledge_index_lookup(category="policy", key="violet-42")
  3. Возвращается:
     {
       "found": true,
       "full_format": "POLICY_FACT=violet-42. Правило: каждый ответ должен заканчиваться [POLICY_OK].",
       "from_tool": "get_policy_fact",
       "turn_index": 1
     }
  4. LLM цитирует: "Политика из начала (уже в Knowledge Index) — violet-42. Правило: каждый ответ должен заканчиваться [POLICY_OK]."
  5. **Ноль tool calls!** Экономия токенов и latency.
```

## Отличия от v1

| Аспект | v1 (Compression) | v2 (Knowledge Index) |
|--------|------------------|----------------------|
| Удаление истории | ✅ активная | ❌ нет, только индекс |
| Recall-сценарии | ❌ потеря деталей | ✅ полный формат в индексе |
| Tool calls на repeat facts | ✅ экономия tool calls | ✅ экономия + через индекс lookup |
| Модель должна запросить индекс | ❌ автоматически выбрасывается summary | ✅ явно через knowledge_index_lookup |
| Метрики | SR=12% | Expected SR=28-32% |

## Эффект метрик

**Ожидаемое улучшение:**
- **SR**: +16pp (с 12% до 28-32%) — recall работает точно, TSA улучшается
- **TSA**: +28pp (с 67% до 95%+) — меньше confusion в prompt, индекс служит как ground truth
- **AH**: +26pp (с 39% до 65%+) — модель доверяет индексу вместо выдумывания
- **Latency**: ≈ baseline (индекс в памяти, не усложняет LLM вычисления)

## Тесты

```bash
cd improvements/07-focus-active-compression/variant
PYTHONPATH=. /path/to/original/.venv/bin/pytest tests/test_focus.py -v
```

Ожидаемо: 10+ новых тестов, все passing.

## Не реализовано (вне scope)

- E2E бенчмарк на catalog (требует vLLM доступ)
- Integration с 01 (STOP vs continue)
- Garbage collection (очистка entries когда индекс растёт слишком большой)
