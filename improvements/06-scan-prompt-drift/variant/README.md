# Basic Agent — variant 06 (SCAN)

Доработка против дрейфа системного промпта: метод SCAN.

См. [IMPLEMENTATION.md](../IMPLEMENTATION.md) и [README спецификации](../README.md).

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

## Бенчмарк

Из корня `experiments/`:

```bash
PYTHONPATH=. python -m benchmark.compare --variant improvements/06-scan-prompt-drift/variant
```
