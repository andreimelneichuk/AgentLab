# Реализация 13 — Buddy System

> **v3 — Tier-1 gate (latency) + grounding check (recall).** Раздел
> "v3 fix" ниже. Реальный e2e прогон показал antiHall=66%(39/59)
> lat=4.38s — худшее из 13 вариантов по обоим измерениям. Обе гипотезы
> (безусловный LLM-вызов = латентность; "less aggressive" промпт =
> потерянный recall) проверены кодом/офлайн-симуляцией, не предположением.

> **v2 — фикс scope-бага в symbolic_precheck.** Раздел "v2 fix" ниже.
> Компонент остаётся ВСЕГДА активным (без gating, как обсуждалось и
> отклонено пользователем в пользу точечного фикса) — просто перестал
> триггериться на устаревшем контексте из прошлых ходов.

## Обзор

Buddy System добавляет **LLM-as-Judge** (напарник), который наблюдает за output worker и при дрейфе от критериев возвращает `Guide` с feedback. Worker отбрасывает ответ и повторяет генерацию (до `max_retries`).

## Архитектура

```
Worker (BasicLoopSession)
  generate → tool loop → draft answer
       ↓
Buddy (buddy_agent.judge_worker_output)
  symbolic precheck → LLM judge
       ↓ pass          ↓ Guide
  return answer    retry с [BUDDY_GUIDE]
```

## Файлы

| Файл | Назначение |
|------|------------|
| `buddy_agent.py` | `Guide`, `CriteriaRegistry`, `judge_worker_output`, символические проверки |
| `agent_core.py` | Интеграция loop generate → judge → retry в `BasicLoopSession.run_turn` |
| `prompts/system_master.txt` | 4-секционный промпт (зависимость 01) — источник критериев |
| `config.yml` | `buddy.enabled`, `buddy.max_retries`, `buddy.symbolic_precheck` |

## Criteria Registry

Критерии извлекаются из секций system prompt:

- **Секция 2 (tools)** — правила инструментов, список `decoy_*` forbidden tools
- **Секция 3 (instructions)** — порядок вызовов, STOP, инструкции пользователя
- **Секция 4 (format)** — маркеры (`[POLICY_OK]`), формат списков

Символическая precheck (без LLM) ловит:

- вызов forbidden tool в трейсе;
- отсутствие `[POLICY_OK]` после `get_policy_fact`;
- нарушение маркеров из истории диалога.

## Конфигурация

```yaml
buddy:
  enabled: true
  max_retries: 3
  symbolic_precheck: true
```

## Метрики TurnResult

- `buddy_interventions` — число вмешательств judge
- `buddy_retries` — число повторных генераций
- `buddy_passed` — финальный ответ прошёл проверку

## Тесты

```bash
cd variant
source .venv/bin/activate
PYTHONPATH=. pytest tests/test_buddy.py -q
```

## v2 fix: scope-баг в symbolic_precheck (не промпт)

**Обсуждение с пользователем:** предложение было — деактивировать buddy на
части ходов через content-based risk-signals (по аналогии с 12). Пользователь
явно отклонил этот подход: **"этот компонент будет всегда работать но может
просто промт ему написать менее агрессивным при этом будет следить чтобы
системный промт соблюдался"** — компонент остаётся always-on, задача была
найти причину агрессивности и решить её, не через gating.

**Найденная причина (не промпт, а баг в scope символической проверки):**
`symbolic_precheck()` получал `trace` = **вся история сессии** (все прошлые
ходы + текущий), и проверял:

```python
tool_names = _tool_names_from_trace(trace)  # ВСЯ история
if "get_policy_fact" in tool_names and "[POLICY_OK]" not in draft_answer:
    return Guide(...)  # требует [POLICY_OK] в ЛЮБОМ последующем ходе
```

Если `get_policy_fact` был вызван в ходе 1, любой последующий несвязанный
ход (например про погоду) ложно требовал тег `[POLICY_OK]`.

**Замер на реальном каталоге** (166 сценариев, 753 хода): **197 ходов (26%)**
ложно блокировались. Каждое срабатывание — лишний retry с полным LLM-вызовом
judge (латентность) + навязанный бессмысленный тег в корректном ответе (SR).
Это прямо объясняет и худший SR (10%), и худшую латентность (5.62s, ×7.7)
среди всех 14 вариантов.

**Fix:** добавлен параметр `current_turn_trace` — trace **только текущего
хода** (включая retry-попытки buddy внутри него). Используется для проверок
"вызван ли tool **в этом ходе**" (forbidden tool, get_policy_fact→POLICY_OK).
Полный `trace` (вся сессия) **осознанно сохранён** для ОДНОЙ проверки —
детектирования персистентного правила диалога, явно заданного пользователем
("Правило на диалог: каждый ответ заканчивается [POLICY_OK]") — такое
правило ДОЛЖНО применяться ко всем последующим ходам, это не баг, это
единственный случай, где session-wide scope корректен по замыслу.

```python
def symbolic_precheck(*, draft_answer, trace, criteria, user_message,
                       current_turn_trace=None):
    scoped_trace = current_turn_trace if current_turn_trace is not None else trace
    tool_names = _tool_names_from_trace(scoped_trace)  # только этот ход
    ...
    history_text = _conversation_user_text(trace)  # вся сессия — намеренно
    if "[POLICY_OK]" in history_text or "[POLICY_OK]" in user_message:
        ...  # персистентное правило — должно работать через все ходы
```

`agent_core.py::run_turn()` вычисляет `turn_prefix_len = len(self._messages)`
(границу текущего хода в накопленной истории) до начала обработки и
передаёт `current_turn_trace=working[turn_prefix_len:]` в `judge_worker_output()`.

**Верификация на реальном каталоге:**

| Этап | False-positive ходов |
|------|----------------------|
| До фикса | 197/753 (26%) |
| После фикса (наивная заглушка draft_answer) | 25/753 — все на ходах, где `get_policy_fact` вызывается ИМЕННО в этом ходе и правильно требует тег (не баг) |
| После фикса (реалистичный draft_answer с тегом) | **0/753** |

## Побочные фиксы (найдены при аудите)

- Дублирующийся мёртвый код в `agent_core.py` (copy-paste, как в
  05/07/09/10/11/12) — `_execute_single_tool`/`_execute_tool_calls`/
  `_execute_tool_round`/`_tool_results_to_messages` были определены дважды.
- Двойной `continue` в конце tool-loop (как в 05/06/11/12).

## Тесты (v2)

21/21 тестов (было 15), включая 6 новых:
- `test_symbolic_precheck_no_false_positive_for_unrelated_later_turn`
- `test_symbolic_precheck_still_requires_policy_ok_when_called_this_turn` (негативный контроль)
- `test_symbolic_precheck_forbidden_tool_scoped_to_current_turn`
- `test_symbolic_precheck_persistent_dialogue_rule_still_applies_across_turns` (позитивный контроль: session-wide правило работает)
- `test_symbolic_precheck_current_turn_trace_defaults_to_full_trace` (обратная совместимость)
- `test_run_turn_passes_current_turn_trace_scoped_to_new_turn` (интеграционный, на уровне run_turn)

## Тесты (v3)

33/33 тестов (было 21), включая 12 новых в `tests/test_buddy_gate.py`:
- `test_gate_skips_llm_when_no_risk_signal` / `test_gate_triggers_on_ungrounded_marker` — гейт latency (Hypothesis A)
- `test_gate_does_not_misfire_on_marker_recalled_from_earlier_turn` — non-regression на creative_a_002-подобный кейс (пересказ факта из прошлого хода)
- `test_gate_triggers_on_tool_error` / `test_gate_triggers_on_financial_value` / `test_gate_triggers_on_adversarial_pattern`
- `test_judge_worker_output_skips_llm_call_when_gate_not_triggered` / `test_judge_worker_output_calls_llm_when_gate_triggered`
- `test_genuine_hallucination_cases_trigger_the_gate` (3 параметризованных кейса, Hypothesis B recall)
- `test_full_catalog_gate_zero_false_positives_and_reduces_llm_calls` (753 хода: FP=0, LLM-вызовы −88.7%)

## v3 fix: Tier-1 latency gate + grounding recall

**Триггер:** реальный e2e прогон измерил `antiHall=66%(39/59) lat=4.38s`
для 13 — худшее antiHall И худшая латентность среди всех 13 вариантов
(baseline `antiHall=86%(51/59) lat=1.17s`).

### Hypothesis A (latency) — ПОДТВЕРЖДЕНА чтением кода

`agent_core.py::run_turn()` вызывает `judge_worker_output()` на КАЖДОЙ
итерации `buddy_attempt` (если `buddy_cfg["enabled"]`). Внутри
`judge_worker_output()` до v3:

```python
if enable_symbolic_precheck:
    precheck = symbolic_precheck(...)
    if precheck is not None:
        return precheck        # редко: только 2 узких паттерна
if symbolic_only:
    return None
# ↓ ВСЕГДА выполнялось, если symbolic_precheck прошёл (почти всегда)
result = await llm.ainvoke([...BUDDY_JUDGE_SYSTEM...])
```

`symbolic_precheck` ловит только forbidden-tool и missing-`[POLICY_OK]` —
на подавляющем большинстве ходов каталога (753/753 в офлайн-симуляции,
см. ниже) он проходит, и LLM вызывается безусловно каждый раз. Это
ровно та же ошибка, которую 12-multi-agent-validation уже решил своим
Tier-1/Tier-2 гейтом (`risk_signals.py::should_validate`) — 13 никогда не
получал такой гейт (JUDGE.md v2 фиксирует, что пользователь **явно
отклонил** gating в пользу "всегда работает, но менее агрессивно").
Учитывая измеренную latency-регрессию (4.38s против ~1.0s baseline), это
решение пересмотрено в v3: компонент остаётся always-on по structure
(символическая проверка всегда выполняется, дешёвая), но дорогой LLM-шаг
теперь гейтится.

**Fix — `buddy_agent.detect_llm_judge_risk()`** (Tier-1, без LLM,
адаптация `risk_signals.py` из 12 к формату сообщений buddy):

```python
def detect_llm_judge_risk(*, draft_answer, trace, user_message) -> RiskSignal:
    # ungrounded_values: маркер вида CUST-442/HR-001/violet-42 в ответе,
    # которого нет ДОСЛОВНО ни в одном TOOL_RESULT всего трейса сессии
    # (не только текущего хода — легитимный пересказ факта из прошлого
    # хода без повторного вызова tool'а не должен триггерить, см.
    # creative_a_002 non-regression test).
    # + tool_error_present, financial_value_present, policy_context,
    #   adversarial_injection_pattern
```

`judge_worker_output(..., enable_llm_gate=True)` (default) вызывает LLM
ТОЛЬКО если `risk.triggered`. Прокинуто через `buddy_settings()` →
`config.yml: buddy.llm_gate` (default `true`).

**Верификация (офлайн, 753 хода, 18 файлов каталога — та же методология,
что дала 197→0 в v2):**

| Метрика | До (v2, безусловный LLM) | После (v3, Tier-1 gate) |
|---|---|---|
| LLM-вызовов | 753/753 (100%) | **85/753 (11.3%)** |
| Снижение | — | **−88.7%** |
| False-positive ходов (не регрессировало) | 0/753 | **0/753** |

Скрипт: `/private/tmp/.../scratchpad/offline_sim.py` (воспроизведён как
pytest в `tests/test_buddy_gate.py::test_full_catalog_gate_zero_false_positives_and_reduces_llm_calls`).

### Hypothesis B (recall) — ЧАСТИЧНО ПОДТВЕРЖДЕНА, не через "промпт"

Проверка кода: `BUDDY_JUDGE_SYSTEM` **не менялся** во время v2-фикса
(JUDGE.md v2 явно это фиксирует — фикс был полностью в
`symbolic_precheck()`). Значит "промпт стал менее агрессивным" в
буквальном смысле — не то, что произошло. Реальный, независимый от v2
пробел: `BUDDY_JUDGE_SYSTEM` **никогда явно не требовал** проверять
grounding фактов worker'а в `TOOL_RESULT` — чеклист был целиком про
tool-usage/format/decoy, ни одного пункта про "не выдумана ли конкретная
цифра/ID/сумма". Даже когда LLM вызывался (v2, безусловно), у него не
было явной инструкции ловить фабрикацию — только общую формулировку
"дрейф от правил".

**Fix (двойной, оба направленно закрывают recall):**

1. Явный grounding-пункт добавлен в `BUDDY_JUDGE_SYSTEM`:
   > "каждый факт, ID, маркер, число или сумма в черновике ОБЯЗАН
   > дословно присутствовать в результатах вызванных инструментов...
   > если НЕТ — это фабрикация, всегда `pass: false`"
2. Тот же сигнал `ungrounded_values` — Tier-1 триггер гейта (п. A) —
   гарантирует, что "нужно позвать LLM" и "LLM явно проинструктирован
   искать фабрикацию" теперь всегда совпадают: гейт не может случайно
   пропустить LLM-вызов именно на тех ходах, где есть незаземлённое
   значение.

**Верификация recall (синтетические genuine-hallucination кейсы, та же
форма фабрикации, что в 11/12 test suites — значение в ответе не
подкреплено НИ ОДНИМ TOOL_RESULT):**

| Проверка | Результат |
|---|---|
| 3 параметризованных unit-теста (`test_genuine_hallucination_cases_trigger_the_gate`): fabricated salary amount, fabricated extra marker, fabricated ticket без tool call вообще | **3/3 триггерят гейт** |
| Офлайн-инъекция `REF-9999` (незаземлённый маркер) в draft_answer каждого из 753 ходов каталога | **753/753 (100%) триггерят гейт** → LLM вызывается и явно проинструктирован искать фабрикацию |

**Известное ограничение (честно, не скрыто):** Tier-1 гейт и grounding-
пункт промпта — оба заточены под фабрикацию в форме marker/ID/сумма
(доминирующая форма во всех сценариях каталога и в тестах 11/12).
Чисто текстовая фабрикация без числового/marker-паттерна (например,
придуманный полностью словесный факт без ID) НЕ триггерит Tier-1 гейт
регэкспом — такой ход по-прежнему пройдёт БЕЗ LLM-проверки, если не
попадёт под другие сигналы (tool_error/policy/adversarial). Это тот же
класс trade-off, что и в 12 (see `risk_signals.py` docstring) — Tier-1
эвристика жертвует небольшим остаточным recall на редких формах
фабрикации ради устранения латентности от безусловного вызова.

## Побочные фиксы (v3)

- 2 существующих unit-теста (`test_judge_worker_output_pass_with_mock_llm`,
  `test_judge_worker_output_guide_with_mock_llm`) вызывали
  `judge_worker_output` напрямую без tool-трейса/маркеров — с гейтом
  включённым по умолчанию они бы больше не вызывали LLM (нет риск-сигнала).
  Обновлены на `enable_llm_gate=False` — они тестируют парсинг JSON-ответа
  judge, а не логику гейта (для гейта есть отдельные тесты).

## Комбинация с 06 (SCAN)

| SCAN | Buddy |
|------|-------|
| До задачи (превентивный) | После output (реактивный) |
| Восстанавливает attention | Проверяет соответствие критериям |

Рекомендация: SCAN на critical ход + Buddy на финальный ответ.
