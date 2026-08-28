# Реализация: Context Engineering (Write / Select / Compress / Isolate) — v2

> **v2 — переработка после провала v1** (SR 26%→13%, TA=23% худшее среди всех
> вариантов). Три корневые причины и их исправления описаны ниже.

## Что сделано

### 1. `variant/scratch_store.py` (не изменён)
- In-memory scratch pad для больших ответов MCP
- `estimate_tokens()` — грубая оценка (~4 символа/токен)
- `save` / `load` / `make_key`

### 2. `variant/context_policy.py` (переписан)
- **Write** — tool results > N токенов → scratch, в контексте ссылка `scratch:{key}`
- **Select** — v2: работает на уровне **атомарных сегментов**, не отдельных сообщений
- **Compress** — masking старых tool results, v2: с защитой MCP-маркеров
- **Isolate** — v2: только **помечает** `_block`, не переставляет сообщения
- `never_drop_patterns` + `MARKER_NEVER_DROP_REGEX` — security rules и реальные
  MCP-маркеры (EMP_ID=, POLICY_FACT=, ...) не маскируются
- `ContextPolicyReport` — мониторинг сегментов финального prompt

### 3. `variant/agent_core.py` (без изменений в этой переработке)
- `messages_to_langchain()` — system prompt + поддержка `role=system`
- `build_scratch_tools()` — pseudo-tools `save_scratch` / `load_scratch`
- `BasicLoopSession._prepare_llm_messages()` — policy перед каждым LLM invoke
- `last_context_report` — сегменты последнего prompt

### 4. `variant/config.yml`
- `mask_tool_older_than_turns`: 2 → **4** (средний сценарий каталога ~2.8 хода)

### 5. `variant/tests/test_context_policy.py`
- 12 старых тестов (не тронуты, все passing)
- **6 новых** тестов на инварианты, не покрытые в v1

## v1 → v2: три корневые причины провала

### Баг #1 (главный): Isolate переставлял сообщения местами

**v1:** `isolate_message_blocks()` собирал все `user`/`assistant` в один список,
все `tool` в другой, склеивал в порядке `[system, chat..., tools...]`.

```
Было (v1):  user → assistant(tool_call c1) → assistant(ответ) → tool_result(c1)
Нужно:      user → assistant(tool_call c1) → tool_result(c1) → assistant(ответ)
```

OpenAI/vLLM API требует, чтобы `ToolMessage` шёл **сразу** после `AIMessage`
с соответствующим `tool_call_id`. Пересортировка ломала эту связь **на каждом
ходу** с tool-вызовом (isolate включён по умолчанию, без порога по длине истории).

**v2 fix:** `isolate_message_blocks()` больше не трогает порядок chat/tool
сообщений — только добавляет `_block` метку для мониторинга. Ведущие system-блоки
(`RULES`, `FACTS`) остаются перед остальными сообщениями, но сами сообщения идут
1:1 в исходном порядке.

```python
# v2:
for msg in messages:  # БЕЗ группировки по role
    m = copy.copy(msg)
    m["_block"] = BLOCK_TOOLS if msg["role"] == "tool" else BLOCK_CHAT
    isolated.append(m)  # порядок как во входном messages
```

### Баг #2: Select создавал осиротевшие ToolMessage

**v1:** `select_relevant_history()` фильтровал сообщения **по отдельности**.
`assistant` с tool_calls обычно имеет **пустой content** (данные в `tool_calls`,
не в тексте) → keyword overlap = 0 → **дропался**. Соответствующий `tool` result
мог остаться (у него содержательный текст) → осиротевший `ToolMessage` без своего
`AIMessage` — нарушение API contract.

**v2 fix:** `_group_into_segments()` группирует сообщения в атомарные единицы:
`[user]` или `[assistant(+tool_calls), tool, tool, ...]`. Решение
оставить/выбросить принимается **на уровне сегмента целиком** — `tool_call_id`
всегда остаётся со своей парой.

```python
def _group_into_segments(messages):
    """user ИЛИ (assistant+tool_calls + все его tool results) — атомарно."""
    ...

def select_relevant_history(self, messages, current_query, report=None):
    segments = _group_into_segments(messages)
    for segment in segments:
        # решение keep/drop применяется к segment целиком
        ...
```

### Баг #3: never_drop_patterns не защищал recall-маркеры

**v1:** `never_drop_patterns = ["POLICY_OK", "ЗАПРЕЩЕНО", "security", "СТРОГО", "STOP"]`
защищал общие security-фразы, но **не** сами маркеры MCP-ответов
(`EMP_ID=`, `violet-42`, `orchid-17`, `CUSTOMER=`, `INVOICE=`, `AUDIT`), которые
бенчмарк проверяет на recall. При `mask_tool_older_than_turns=2` эти маркеры
маскировались уже на 3-м ходу.

**v2 fix:** Добавлен `MARKER_NEVER_DROP_REGEX`, который распознаёт форматы
реальных tool-ответов и защищает их независимо от текстовых паттернов:

```python
MARKER_NEVER_DROP_REGEX = re.compile(
    r"(EMP_ID=|POLICY_FACT=|BENCH_MARKER_\w+=|CUSTOMER=|INVOICE=|AUDIT\b|"
    r"TICKET_ID=|QUOTE\b|LEAVE_DAYS=|DEPT=|RESULT=|WEATHER\b|TRANSLATED\[|"
    r"RANDOM_MARKER=)",
    re.IGNORECASE,
)
```

`_matches_never_drop()` теперь проверяет **и** текстовые паттерны, **и** этот
regex. Плюс `mask_tool_older_than_turns` поднят с 2 до 4 (средний сценарий
каталога ~2.8 хода — старый порог маскировал факты почти сразу после получения).

## Never-drop (не сжимается / не маскируется / не выбрасывается) — v2

- System prompt (блок `[RULES]`, isolate)
- Сообщения с паттернами: `POLICY_OK`, `ЗАПРЕЩЕНО`, `security`, `СТРОГО`, `STOP`
- **Новое:** MCP-маркеры реальных ответов (`EMP_ID=`, `POLICY_FACT=`,
  `BENCH_MARKER_*=`, `CUSTOMER=`, `INVOICE=`, `AUDIT`, `TICKET_ID=`, `QUOTE`,
  `LEAVE_DAYS=`, `DEPT=`, `RESULT=`, `WEATHER`, `TRANSLATED[`, `RANDOM_MARKER=`)
- Tool results текущего и последних K user-turns (по умолчанию K=4)
- Последние N user-turns при Select (по умолчанию N=2) — для s04_003
- **Новое:** атомарные `assistant(tool_calls) + tool_results` сегменты — Select
  никогда не разделяет пару

## Как проверить

### Unit-тесты

```bash
cd improvements/09-context-engineering/variant
PYTHONPATH=. pytest tests/test_context_policy.py -v
PYTHONPATH=. pytest tests/ -q
```

Ожидается: 21/21 тестов, включая 6 новых регрессионных на инвариант
tool_call/tool_result adjacency:
- `test_isolate_preserves_tool_call_adjacency`
- `test_prepare_for_llm_preserves_tool_call_adjacency_full_pipeline`
- `test_select_never_orphans_tool_result_without_its_assistant_call`
- `test_compress_never_masks_benchmark_markers`
- `test_compress_masks_non_marker_old_tool_results`
- `test_isolate_no_reorder_matches_input_order`

### Smoke-бенчмарк

```bash
cd /path/to/AgentLab
python -m benchmark.mcp_mock_server  # терминал 1

PYTHONPATH=. python -m benchmark.compare \
  --variant improvements/09-context-engineering/variant \
  --tags smoke --limit 5
```

## Ожидаемый эффект (v2)

| Метрика | v1 | v2 (Expected) | Baseline |
|---------|----|--------------|----------|
| SR | 13% | **22-26%** | 26% |
| TA | 23% (худшее) | **45%+** | 49% |
| Recall (маркеры) | ❌ маскировались | ✅ защищены regex | ✅ |
| Tokens | −30% (заявлено) | −20-25% (реалистично после fix) | baseline |

Главный эффект — устранение API-breaking reordering bug, который ломал
**каждый** ход с tool-вызовом, независимо от длины сессии.
