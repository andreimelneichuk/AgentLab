# Basic Agent — subtask isolation variant

Multi-agent orchestrator: изолированные субагенты по доменам MCP (core / hr / crm / sse).

## Запуск

```bash
source .venv/bin/activate
python basic_agent.py --orchestrator "Найди сотрудника Anna в HR"
python basic_agent.py --orchestrator -i
```

## Бенчмарк (из корня `experiments/`)

```bash
PYTHONPATH=. python -m benchmark.compare \
  --variant improvements/10-subtask-isolation/variant \
  --backends basic_orchestrator \
  --tags multiserver
```

## Тесты

```bash
PYTHONPATH=. pytest tests/ -q
```

Подробности: [IMPLEMENTATION.md](../IMPLEMENTATION.md)
