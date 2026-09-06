# Baseline Agent Agent — semantic tool selection (08)

Вариант с top-k фильтрацией инструментов перед `bind_tools` (TF-IDF retriever).

Бенчмарк запускается **из корня** `experiments/`:

```bash
cd ../..
PYTHONPATH=. python -m benchmark.compare --variant improvements/08-semantic-tool-selection/variant
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
PYTHONPATH=. pytest tests/test_tool_retriever.py -q
```
