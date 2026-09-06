# Baseline Agent — original (эталонный пайплайн)

Замороженная эталонная версия стандартного пайплайна: tool loop + MCP.

Бенчмарк запускается **из корня** `experiments/`:

```bash
cd ..
PYTHONPATH=. python -m benchmark.compare --variant original
```

## Запуск агента (CLI)

```bash
source .venv/bin/activate
python basic_agent.py "Привет"
python basic_agent.py -i
```

## Тесты agent_core

```bash
PYTHONPATH=. pytest tests/ -q
```
