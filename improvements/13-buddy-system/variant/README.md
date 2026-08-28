# Basic Agent — Buddy System (13)

Вариант с **LLM-as-Judge**: напарник корректирует дрейф ответа worker.

Бенчмарк из корня `experiments/`:

```bash
cd ../..
PYTHONPATH=. python -m benchmark.compare --variant improvements/13-buddy-system/variant
```

## Запуск агента (CLI)

```bash
source .venv/bin/activate
python basic_agent.py "Привет"
python basic_agent.py -i
```

## Тесты

```bash
PYTHONPATH=. pytest tests/ -q
```

Подробности реализации: [../IMPLEMENTATION.md](../IMPLEMENTATION.md)
