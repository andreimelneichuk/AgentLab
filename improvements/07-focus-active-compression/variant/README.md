# Basic Agent — variant 07 (Focus Active Compression)

Активная компрессия контекста: `start_focus` / `complete_focus`, Knowledge block, withdraw.

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
