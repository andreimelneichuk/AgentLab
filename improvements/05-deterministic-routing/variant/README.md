# Baseline Agent Agent — deterministic routing (05)

Вариант с rule-based router: intent → domain → filtered tools.

## Запуск

```bash
source .venv/bin/activate
python basic_agent.py "Найди сотрудника в HR-системе"
python basic_agent.py -i
```

## Бенчмарк

Из корня `experiments/`:

```bash
PYTHONPATH=. python -m benchmark.compare --variant improvements/05-deterministic-routing/variant
```

## Тесты

```bash
PYTHONPATH=. pytest tests/ -q
```

Подробности реализации: [IMPLEMENTATION.md](../IMPLEMENTATION.md).
