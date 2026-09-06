# 10 — Изоляция подзадач и субагенты

**Tier:** 2  
**Источник:** EmergentMind, GetMaxim, общая практика agentic systems  
**Проблема:** Одна длинная сессия накапливает шум; подзадачи мешают друг другу

> **v2 (переработка после провала v1, SR=15%):** найдены и исправлены 5 причин:
> (1) `decompose_request()` возвращал один план на ход — сообщения с двумя доменами
> терялись частично; (2) `is_recall_turn()` покрывал только **27.4%** реальных
> recall-ходов каталога (64/234) — теперь **66.7%** при false-positive rate
> **0.6%** (было бы 10.8% с агрессивной pronoun-эвристикой, которую пришлось
> отклонить); (3) handoff переносил только name+arguments вызова tool, не сам
> ответ — "структурированный факт" был текстом; (4) recall-субагент жёстко
> требовал "отвечай ТОЛЬКО из фактов" даже для general-knowledge ходов;
> (5) `spawn_session(domain)` создавал новую сессию (пустая история) на КАЖДЫЙ
> вызов. См. IMPLEMENTATION.md для деталей и разбора отклонённых подходов.

## Суть проблемы

Вместо одного агента на 500+ ходов:

- Ошибки из задачи A влияют на задачу B
- Context rot и lost-in-the-middle усиливаются
- Невозможно параллелить независимые шаги

**Решение:** изолированные субагенты с **чистым контекстом** и узким набором tools.

## Паттерн

```
Orchestrator (тонкий)
    │
    ├── Subagent: policy_lookup
    │     context: только policy tools + задача
    │     output: structured fact
    │
    ├── Subagent: hr_employee
    │     context: HR tools + employee_id из шага 1
    │
    └── Subagent: crm_update
          context: CRM tools + validated payload
```

Orchestrator не вызывает tools напрямую — только делегирует и собирает результаты.

## Когда применять

| Сигнал | Действие |
|--------|----------|
| Разные MCP-сервера в одной задаче | Отдельный subagent на сервер |
| `s08_multiserver` failures | Изоляция по domain |
| Длинный creative/adversarial сценарий | Subagent на фазу (research → act → verify) |
| Compliance-цепочка | Worker + validator (см. 12) |

## Контракт между агентами

Строго **структурированный** handoff (JSON), не свободный текст:

```json
{
  "subtask": "policy_fact",
  "status": "ok",
  "data": { "policy_code": "violet-42" },
  "tools_used": ["get_policy_fact"],
  "errors": []
}
```

Снижает галлюцинации при передаче между агентами.

## Что менять в Baseline Agent

| Место | Действие |
|-------|----------|
| Новый `orchestrator.py` | Декомпозиция + spawn subagents |
| Копии `agent_core` с урезанными tools | `subagents/hr_agent.py`, etc. |
| `benchmark/compare.py` | Опциональный режим multi-agent variant |

## Критерии успеха

- `s08_multiserver` pass rate ↑
- Меньше cross-contamination (`tool_forbidden` между доменами)
- Субагент policy не видит decoy tools CRM

## Стоимость

- Больше LLM-вызовов (каждый subagent — отдельный thread)
- Экономия на контексте часто перекрывает (меньше токенов на вызов)
- Latency: возможен параллелизм независимых subagents

## Связь с 05, 08

- **05** — orchestrator использует deterministic routing к subagent
- **08** — внутри subagent достаточно top-3 tools, не top-5 из 25

## Чеклист

- [x] Определить границы subagents по 4 MCP mount
- [x] Structured handoff schema (v2: несёт реальное значение tool response, не только name+args)
- [x] Persist context per subagent в рамках диалога (v2: было "чистый на каждый вызов" — ломало multi-turn continuity внутри домена)
- [x] Orchestrator trace (какой subagent, какой результат)
- [x] Multi-domain decomposition (v2: один ход может дать несколько execute-планов)
- [x] Regression-тесты на реальном каталоге (recall coverage / false-positive rate)
- [ ] Бенчмарк: multiserver + длинные сценарии (требует доступ к llm-server)
