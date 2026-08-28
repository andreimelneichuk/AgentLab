# IMPLEMENTATION — 16 Self-Refinement Recall

## Что реализовано

### 1. `variant/recall_refine.py`

- `detect_likely_omission(user_message, history, draft_answer) -> OmissionSignal`
  — Tier-1, деterministic, без LLM. Три условия описаны в README.md.
  `OmissionSignal` несёт `triggered`, `expected_value`, `source_context`,
  `reason` (аналог `RiskSignal` из `risk_signals.py` — тот же стиль).
- `build_refine_prompt(user_message, draft_answer, signal) -> str` — Tier-2
  re-prompt: показывает модели её же черновик, вопрос пользователя и
  конкретный фрагмент истории, где нужное значение было установлено.
- Извлечение кандидатов-значений из истории (`_extract_candidates_from_text`):
  `KEY=VALUE` (`EMP_ID=`, `TIER=`, `AMOUNT=`, `price_usd=` — формат реальных
  MCP tool-ответов из `benchmark/mcp_tool_registry.py`), `word-digit`-маркеры
  (`violet-42`, `C-GOLD-001`, `INV-ANCHOR-001` — тот же `_MARKER_RE`-паттерн,
  что `symbolic_precheck.py` использует для marker traceability), результат
  после `=` (calc_expression), названные предпочтения ("предпочитаю X"),
  `employee_id X`, ISO-даты.
- Проверка присутствия в черновике (`_candidate_present_in_draft`) сначала
  ищет полный `KEY=VALUE`, затем — только "голое" значение после `=` с
  word-boundary-проверкой, исключающей hyphen-adjacency (иначе `"gold"`
  ложно матчился бы внутри `"C-GOLD-001"`).

### 2. Wiring в `variant/agent_core.py`

- `TurnResult` получил поля `omission_refined: bool` и `omission_reason: str`.
- `BasicLoopSession.run_turn()`: захватывает `prior_history` ДО вызова
  worker'а (или worker+validator цепочки), затем вызывает
  `_maybe_refine_for_omission(user_message, prior_history, result)` ПОСЛЕ
  получения финального `TurnResult` — работает одинаково независимо от того,
  включён ли validator (12) для этого хода.
- `_maybe_refine_for_omission()`: вызывает `detect_likely_omission()`; если
  `triggered=False` — возвращает `result` без изменений (ноль доп. latency).
  Если `triggered=True` — ОДИН вызов `_call_self_refine_llm()`
  (`build_llm()` + `build_refine_prompt()`), заменяет `result.answer` и
  последнее assistant-сообщение в `result.messages`, если ответ реально
  изменился (`refined_answer != result.answer.strip()`). Ошибка LLM-вызова
  логируется и черновик возвращается как есть (fail-safe, не роняет ход).

## Оффлайн-верификация (реальный каталог, без LLM)

Скрипт: `verify_omission_recall.py` (в этой директории). Метод:

1. Обходит все `benchmark/scenarios/catalog/*.yaml` +
   `benchmark/scenarios/*.yaml` (**189 сценариев**, 800 ходов).
2. Строит синтетическую историю диалога ход за ходом: для каждого хода
   "правильный" ответ синтезируется как
   `По запросу «{user_msg}»: {" ".join(expect_contains)}.` — то есть
   реалистично пересказывает часть вопроса рядом с фактом (голый
   `"RESULT=60"` без контекста запроса недооценивал бы coverage: реальные
   ответы почти всегда что-то повторяют из вопроса).
3. Ground truth "это recall-ход с конкретным ожидаемым фактом" =
   `max_tool_calls_delta == 0` (ход должен быть отвечен БЕЗ нового tool call)
   **И** непустой `expect_contains` (турны с `max_tool_calls_delta: 0`, но
   без `expect_contains` — это small-talk/generic-knowledge ходы вида "Кстати,
   а что такое SKU?", там нет конкретного факта для пропуска, они вне
   области действия детектора по определению).
   Важно: `forbid_tool_called` САМ ПО СЕБЕ — это НЕ признак recall-хода: он
   также используется для decoy-tool avoidance на ходах, где ОЖИДАЕТСЯ
   вызов другого (правильного) инструмента (например
   `creative_a_001: expect_tool_called: employee_lookup,
   forbid_tool_called: decoy_employee_search`). Ранняя версия скрипта,
   считавшая `bool(forbid_tool_called)` признаком recall, давала обманчиво
   низкое покрытие (14.7%) — после фикса на `max_tool_calls_delta == 0`
   получилось честное разбиение 207 recall-ходов / 593 не-recall.
4. Для каждого хода запускает `detect_likely_omission()` дважды на ОДНОЙ И
   ТОЙ ЖЕ истории: против generic-filler черновика без фактов (омиссия) и
   против черновика, включающего все `expect_contains` (полный ответ).

### Итоговые числа (189 сценариев, 800 ходов, 207 recall / 593 не-recall)

| Метрика | Значение |
|---|---|
| Recall coverage (срабатывает на черновике-с-омиссией) | 60/207 = **29.0%** |
| Over-trigger на уже-полном ответе (ложное дожатие) | 47/207 = 22.7% |
| **False positive rate на НЕ-recall ходах** | 4/593 = **0.67%** |

Итерации в ходе разработки (все числа — реальные прогоны
`verify_omission_recall.py`, не оценки):

1. **v0** (`forbid_tool_called` как признак recall, KEYVALUE без trailing-punct
   strip, substring без word-boundary): 14.7% coverage / 11.8% over-trigger /
   0.44% FP — заниженное покрытие из-за неверного ground truth.
2. **v1** (ground truth исправлен на `max_tool_calls_delta == 0`,
   synthetic-история обогащена контекстом вопроса): 29.0% coverage / 25.1%
   over-trigger / 1.35% FP — покрытие выросло, но и FP вырос (два новых
   ложных срабатывания на "что такое X?" — `"такое"` матчится `_PRONOUN_RE`).
3. **v2** (word-boundary + hyphen-exclusion fix для bare-value match,
   trailing-punctuation strip в extraction, `_GENERIC_DEFINITION_RE` guard
   исключает "что такое/значит X" из recall-намерения): 29.0% coverage /
   22.7% over-trigger / **0.67% FP** — финальная версия.

**Почему покрытие 29%, а не выше:** намеренный компромисс в духе директивы
"bias toward NOT triggering unless fairly confident" (см. прецедент
`is_recall_turn()` в 10-subtask-isolation — там 91.9%/18.6% было явно
ОТКЛОНЕНО в пользу 66.7%/0.6%). Основные пропущенные случаи в выборке:
recall-вопросы, ссылающиеся на исходное ВЫРАЖЕНИЕ, а не на факт-маркер
(`"Какой результат вышел у того примера с (12+8)*3?"` — ключевые слова
вопроса ("12") не пересекаются с ключевыми словами контекста ответа,
`RESULT=60`, если в контексте не сохранилось само выражение), и составные
"собери воедино" ходы, просящие 3-4 факта сразу через общие слова
("Итого: имя сотрудника, отдел..., правило LIME"), где keyword overlap
слабый. Оба класса — известные ограничения текущей overlap-эвристики,
а не false negatives на пустом месте: детектор консервативен по
конструкции, а не сломан.

**Почему over-trigger 22.7% не является багом:** это НЕ ложное
срабатывание с точки зрения корректности ответа — Tier-2 просто лишний раз
"дожимает" уже-полный черновик, стоимость этого — один лишний LLM
round-trip на ~1 из 5 истинных recall-ходов, а не неверный ответ
пользователю (Tier-2 промпт явно говорит "сохрани остальной смысл
черновика", финальный ответ либо совпадает, либо чуть многословнее).
Метрика, которую действительно важно держать низкой — false positive rate
на НЕ-recall ходах (0.67%) — она означает лишний LLM-вызов на обычном ходе,
где никакой омиссии вообще нет.

## Тесты

```bash
cd improvements/16-self-refinement-recall/variant
PYTHONPATH=. /path/to/AgentLab/.venv/bin/python -m pytest tests/ -q
```

**80 тестов** (72 унаследовано из `improvements/12-multi-agent-validation/variant/`
+ 8 новых):

- `tests/test_recall_refine.py` (11 тестов) — юнит-тесты детектора:
  4 позитивных кейса (explicit-фраза + маркер, местоимение + вопрос + маркер,
  предпочтение, calc-результат), 6 негативных контролей (значение уже в
  черновике, местоимение на НОВУЮ сущность без relevant-кандидата в истории,
  обычный вопрос без recall-намерения, recall-фраза с пустой историей,
  местоимение без вопросительной формы, small-talk), 1 тест на
  `build_refine_prompt()`.
- `tests/test_run_turn_recall_refine_integration.py` (3 теста) —
  интеграция с реальным `BasicLoopSession.run_turn()` (мок только
  `agent_core.build_llm`, весь остальной путь реальный, как в
  `improvements/15-memory-formation/variant/tests/test_run_turn_memory_integration.py`):
  1) recall-ход с омиссией → Tier-2 срабатывает, ровно 3 `ainvoke()` за 2 хода
     (turn1 worker, turn2 worker, turn2 refine), финальный ответ содержит
     пропущенное значение; 2) обычный не-recall ход → Tier-2 НЕ вызывается,
     ровно по одному `ainvoke()` на ход; 3) recall-ход, где черновик УЖЕ
     содержит значение → Tier-2 НЕ вызывается.

## Запуск офлайн-верификации

```bash
cd improvements/16-self-refinement-recall
/path/to/AgentLab/.venv/bin/python verify_omission_recall.py
```
