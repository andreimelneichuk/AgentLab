# 12 — Мультиагентная валидация (Worker + Validator)

**Tier:** 3  
**Источник:** AWS / Elizabeth Fuentes (техника 4)  
**Проблема:** Одиночный агент молча подтверждает незавершённые или failed операции

> **v2 (фикс после аудита, baseline SR=11%, CSR=12% — худший CSR из всех вариантов):**
> `_run_turn_with_validation()` возвращал только ПОСЛЕДНЮЮ попытку worker'а
> (`last_result`), а `_run_worker_turn()` создаёт `tool_calls`/`tool_call_details`/
> `rounds`/токены **с нуля** на каждый вызов. Если валидатор реджектил
> ответ, а worker на retry не вызывал tool снова (данные уже в контексте,
> просто переформулировал текст), финальный `TurnResult.tool_calls` терял
> факт вызова — `expect_tool_called` в `benchmark/scoring.py::check_turn`
> проваливался, хотя tool реально был вызван в этом же логическом ходе.
> Это объясняет, почему CSR (критические/adversarial сценарии — ровно те,
> где валидатор активен через `enabled_for_tags: [critical, adversarial]`)
> был хуже даже среднего SR. Исправлено: `tool_calls`/`tool_call_details`/
> `rounds`/токены/latency накапливаются через ВСЕ попытки. Заодно исправлена
> обратная сторона той же проблемы — forbidden-tool нарушение из
> отклонённой попытки больше не "исчезает", если retry его не повторяет.
> Также убран дублирующийся мёртвый код и двойной `continue` (та же
> категория багов, что чинили в 05/07/09/10/11).
>
> **v3 (три направления улучшения, реализованы через параллельных агентов):**
>
> 1. **Двухуровневая проверка (tier-1/tier-2).** `symbolic_precheck.py` —
>    бесплатная детерминированная проверка (decoy tools, false success,
>    `[POLICY_OK]` без факта, untraceable markers, missing tool result),
>    адаптированная из guardrails.py эксперимента 11. Вызывается ПЕРЕД
>    дорогим LLM-валидатором — если находит нарушение, LLM не вызывается
>    вообще. LLM-валидатор остаётся только для пункта 1 чеклиста
>    ("соответствие запросу") — семантики, которую символическое правило
>    не проверит.
> 2. **Production-совместимый триггер (`risk_signals.py`).** Раньше решение
>    "нужен ли валидатор" принималось ИСКЛЮЧИТЕЛЬНО по `scenario_tags` —
>    тестовой метаинформации, которой в продакшене просто негде взяться.
>    Теперь `detect_risk_signals()` анализирует содержание самого хода
>    (tool error в trace, policy/security-контекст, side-effecting tool,
>    финансовые значения, adversarial-паттерны, маркер без backing tool) —
>    работает и без тегов сценария. Активация валидатора = tag OR risk-signal.
> 3. **Структурированный retry-feedback.** `reasons` теперь объект
>    `{code, detail}` из фиксированного enum вместо произвольной строки —
>    `format_retry_feedback()` даёт worker'у конкретную actionable
>    подсказку по коду (`tool_not_called` → "Вызови обязательный
>    инструмент...", `format_missing` → "Добавь обязательный формат...")
>    вместо общей фразы "исправь с учётом замечаний".
>
> Итог: 66/66 тестов (было 15 в v1), включая интеграционные тесты на все
> три направления вместе. См. IMPLEMENTATION.md.

## Суть проблемы

**Functional hallucination:** агент возвращает SUCCESS когда:

- Tool вернул ошибку
- Обязательный шаг пропущен
- Результат логически противоречит запросу

Один агент оптимизирует «закончить задачу», а не «быть правым».

## Паттерн Worker + Validator

```
┌─────────────┐     tool calls      ┌──────────────┐
│ Worker Agent│ ──────────────────► │ MCP / APIs   │
└──────┬──────┘                     └──────────────┘
       │ draft result
       ▼
┌─────────────┐
│  Validator  │  проверяет: логика, tool traces, формат
└──────┬──────┘
       │ approve / reject + feedback
       ▼
   User (только если approved)
```

Исследование (документ): **два несогласных агента безопаснее одного**, который делает что хочет.

## Что проверяет Validator

1. **Соответствие запросу** — ответ отвечает на вопрос пользователя
2. **Tool trace** — нужные tools вызваны, forbidden — нет
3. **Консистентность** — факты в ответе совпадают с tool results
4. **Формат** — `[POLICY_OK]`, JSON schema вывода
5. **Политика STOP** — при ошибке tool агент не «додумал» данные

Validator получает историю хода и результаты tool calls, не только финальный текст.

## Отличие от 13 (Buddy System)

| 12 Worker+Validator | 13 Buddy System |
|----------------------|-----------------|
| Проверка **результата задачи** | Коррекция **дрейфа** от правил по ходу |
| Один проход в конце хода | Может вмешаться mid-turn |
| Фокус: factual/tool correctness | Фокус: style, compliance steps, SOP |
| AWS техника 4 | AWS Steering SDK |

Можно объединить: Buddy следит за процессом, Validator — за итогом.

## Что менять в Basic

| Место | Действие |
|-------|----------|
| `validator_agent.py` | Отдельный prompt + доступ к trace |
| `agent_core.py` | Pipeline: worker → validator → user |
| `benchmark/scoring.py` | Опционально: validator как oracle |

## Критерии успеха

- 0 случаев ответа с `violet-99` (decoy) при expect `violet-42`
- Validator ловит false success на `s10_adversarial`
- Latency: +1 LLM call на ход (приемлемо для critical)

## Конфигурация

```yaml
validator:
  enabled_for_tags: [critical, adversarial]
  max_reject_retries: 2
  model: same_or_smaller  # validator может быть дешевле
```

## Зависимости

- **01** (критерии проверки из Секции 3–4 промпта)

## Чеклист

- [x] Prompt validator с чеклистом по доменам
- [x] Вход: user msg + worker trace + draft answer
- [x] Reject → worker retry с feedback
- [ ] Метрика: validator catch rate на synthetic false success
- [x] Не зацикливаться (max 2 retries)
