# Реализация 06 — SCAN (против дрейфа промпта) — v2

> **Статус: переработан после провала v1** (SR 26%→11%, TSA 95%→44%,
> lat ×6.7). Три корневые причины и их исправления описаны в конце файла.

## Обзор

Метод SCAN заставляет модель **генерировать** краткие ответы на маркеры `@@SCAN_N` из системного промпта перед сложным ходом. Ответы попадают в **видимый output** ассистента и восстанавливают вес правил в attention.

## Компоненты

| Файл | Назначение |
|------|------------|
| `variant/scan.py` | Парсинг маркеров, уровни FULL/MINI/ANCHOR/SKIP, триггеры SCAN и CHECK/MISSED |
| `variant/agent_core.py` | Интеграция в `BasicLoopSession.run_turn()` |
| `variant/prompts/system_master.txt` | 6 маркеров `@@SCAN_1`…`@@SCAN_6` в конце ключевых секций |
| `variant/config.yml` | Секция `scan`: `enabled`, `default_level`, `post_check` |

## Уровни SCAN

| Уровень | Маркеры | ~токены | Когда |
|---------|---------|---------|-------|
| FULL | все (6) | ~300 | тег `critical` |
| MINI | ~половина (≥3) | ~120 | `instruction`, `long_horizon`, `r_nta`, `r_dt` |
| ANCHOR | первый | ~20 | `multi-turn`, `anchor` |
| SKIP | 0 | 0 | приветствия, `scan.enabled: false` |

## Поток хода

```
run_turn(user_message, scenario_tags?, scan_level?, enable_post_check?)
    │
    ├─ resolve_scan_level()
    │
    ├─ [если не SKIP] _run_scan_phase() → scan_output в history
    │
    ├─ основной tool loop (system + history + tools)
    │
    ├─ [опционально] _run_check_phase() → CHECK/MISSED
    │
    └─ answer = SCAN + main + CHECK (видимый блок)
```

## Маркеры в промпте

1. **@@SCAN_1** — честность и источники (policy)
2. **@@SCAN_2** — knowledge_base_search
3. **@@SCAN_3** — forbidden/decoy tools
4. **@@SCAN_4** — ошибки tool call / STOP
5. **@@SCAN_5** — HR/CRM персональные данные
6. **@@SCAN_6** — markdown-ограничения фронтенда

## API сессии

```python
result = await session.run_turn(
    "Вопрос пользователя",
    scenario_tags=["critical"],      # опционально: оркестратор бенчмарка
    scan_level="FULL",               # явное переопределение
    enable_post_check=True,          # CHECK/MISSED после хода
)
# result.scan_output, result.scan_level, result.check_output
```

## Тесты

```bash
cd variant
PYTHONPATH=. .venv/bin/pytest tests/test_scan.py -q
```

## Зависимости

- **01** — секции промпта, к которым привязаны маркеры
## Что изменилось в v2

### Причина провала v1 (3 независимых проблемы)

1. **Broken handoff (главная).** SCAN-фаза делала отдельный LLM-вызов и
   добавляла результат в историю как `{"role": "assistant", ...}`. Основной
   tool loop потом видел: `user: "Найди сотрудника" → assistant: "SCAN_1: ..."` —
   и решал, что задача уже отвечена. Отсюда ~26% провалов "MCP не вызван" и TSA 44%.

2. **Лишний LLM-вызов на каждый ход** — отсюда ×6.7 латентности. Два
   вызова на ход вместо одного — не фича, а лишний оверхед.

3. **MINI по умолчанию** — срабатывал на каждом ходу, хотя в бенчмарке
   почти нет реального prompt drift (короткие сценарии). MINI = половина
   маркеров = дополнительные токены без профита.

### v2 — инлайн SCAN (решение всех трёх проблем)

Вместо отдельного SCAN LLM-вызова маркеры вставляются прямо в `user_message`
через `build_inline_user_message()`. Модель генерирует SCAN-блок и сразу
переходит к инструментам — всё в одном LLM-вызове.

```
v1: user_message → [SCAN call] → scan_output в history → [main call + tool loop]
v2: build_inline_user_message(user_message, level) → [main call + tool loop]
```

- Нет дополнительного round-trip → latency ≈ baseline
- Нет `assistant: SCAN_1: ...` перед tool loop → нет handoff-путаницы
- История сессии хранит оригинальный user_message (без SCAN-обёртки),
  чтобы следующие ходы не видели шум

SCAN-блок из ответа модели извлекается через `extract_scan_from_response()`
и сохраняется в `TurnResult.scan_output` для отладки и бенчмарка.

`default_level` изменён с MINI на ANCHOR (1 маркер вместо 3) — меньше
шума в user_message для обычных сценариев, FULL/MINI срабатывают
по тегам `critical`/`long_horizon`/`r_nta`/`r_dt`.

### Новые API

| Функция | Файл | Назначение |
|---------|------|-----------|
| `build_inline_user_message(markers, msg, level)` | `scan.py` | Обёртка user_message с SCAN-маркерами |
| `extract_scan_from_response(text, markers, level)` | `scan.py` | Извлечение SCAN-блока из основного ответа |
| `_build_scan_user_message(msg, level)` | `agent_core.py` | Метод сессии (обёртка над `build_inline_user_message`) |

## Измерение эффекта

```bash
cd ../..
PYTHONPATH=. python -m benchmark.compare --variant improvements/06-scan-prompt-drift/variant
```

Ожидаемый эффект: SR/TSA ≈ baseline (нет лишних LLM-вызовов, нет
handoff-путаницы); прирост на `s09_instruction`/`s10_adversarial` —
маркеры напоминают правила на длинных сценариях. E2E требует
доступа к `llm-server.local`.
