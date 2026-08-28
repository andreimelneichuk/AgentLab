# Basic Agent — tool validation layer (02)

Вариант с pre-execution валидацией аргументов MCP-инструментов.

## Запуск

```bash
source .venv/bin/activate
python basic_agent.py "Привет"
```

## Бенчмарк

Из корня `experiments/`:

```bash
PYTHONPATH=. python -m benchmark.compare --variant improvements/02-tool-validation-layer/variant --tags smoke
```

## Тесты

```bash
source .venv/bin/activate
PYTHONPATH=. pytest tests/ -q
```

См. также `IMPLEMENTATION.md` в родительской папке `02-tool-validation-layer/`.
