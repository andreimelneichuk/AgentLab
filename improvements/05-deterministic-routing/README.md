# 05 — Детерминированный routing

**Tier:** 1–2  
**Источник:** Indie Hackers / Claude Code  
**Проблема:** LLM используется как switch-оператор — непредсказуемый выбор ветки

> **Статус: v2.** Первая реализация (hard per-turn gate, first-match-wins,
> узкий unknown-fallback) провалилась на бенчмарке (SR 26%→14%, TSA 95%→64%)
> — router прятал ожидаемый tool в четверти ходов. v2 переработан на
> sticky-домены + union-match + always-on CORE, см.
> [IMPLEMENTATION.md](IMPLEMENTATION.md) для деталей и офлайн-валидации
> (100% покрытие каталога вместо 75.5%).

## Суть проблемы

Один из четырёх системных провалов в продакшене: **routing на основе свободной интерпретации LLM**. Модель хороша в тексте, плоха как детерминированный маршрутизатор.

Примеры плохого routing:

- Запрос в HR → агент идёт в CRM, потому что «похоже на контакты»
- Два похожих lookup-tool → выбирается тот, что ниже в промпте
- Multi-server MCP (core/hr/crm/sse) → случайный сервер

## Принцип

```
Пользовательский запрос
       ↓
Детерминированный router (код: rules / classifier / intent tags)
       ↓
Подмножество tools + узкий system prompt для домена
       ↓
LLM (только выбор среди 2–5 tools, не среди 25)
```

LLM — для формулировок и параметров. **Кто какой домен обслуживает — решает код.**

## Варианты реализации

### A. Rule-based router

```python
def route(message: str) -> Domain:
    if any(k in message.lower() for k in ("политик", "policy", "violet")):
        return Domain.POLICY
    if any(k in message.lower() for k in ("сотрудник", "hr", "отпуск")):
        return Domain.HR
    ...
```

### B. Lightweight classifier

- Embeddings + kNN по размеченным примерам
- Не LLM — дешевле и стабильнее

### C. Explicit user / session context

- UI уже знает модуль (CRM tab) → tools только этого модуля

## Что менять в Baseline Agent

| Место | Действие |
|-------|----------|
| Новый модуль `routing.py` | Intent → allowed_tools + prompt variant |
| `agent_core.py` | Фильтр tools до передачи в LLM |
| `benchmark/scenarios/catalog/s08_multiserver.yaml` | Проверка правильного сервера |

## Критерии успеха

- Снижение `r_dt` на multiserver-сценариях
- Снижение `tool_forbidden` (агент не видит чужие tools)
- Стабильность: один и тот же запрос → тот же route (детерминизм)

## Связь с 08 (semantic tool selection)

| 05 | 08 |
|----|-----|
| Жёсткие правила / intent | Мягкий векторный top-K |
| Нулевая стоимость inference | Нужны embeddings |
| Идеален для известных доменов | Идеален при 30+ tools |

Можно комбинировать: router сужает до домена → semantic selection внутри домена.

## Зависимости

- Нет (rule-based)
- Опционально: embedding model для варианта B

## Чеклист

- [x] Карта intent → domain → tools для 4 MCP-серверов
- [x] Router до загрузки tools в контекст LLM
- [x] Отдельные prompt-фрагменты по доменам
- [x] Тесты на `s08_multiserver`, `s06_crm`, `s05_hr`
- [x] Fallback: неизвестный intent → узкий safe subset + отказ
