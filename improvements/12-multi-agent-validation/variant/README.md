# Baseline Agent Agent — variant 12 (Worker + Validator)

Мультиагентная валидация: Worker генерирует черновик, Validator проверяет по tool trace.

## Запуск

```bash
source .venv/bin/activate
python basic_agent.py "Привет"
python basic_agent.py -i
```

Валидатор включается для сценариев с тегами `critical` / `adversarial` (см. `config.yml` → `validator`).

## Тесты

```bash
PYTHONPATH=. pytest tests/test_validator.py -q
PYTHONPATH=. pytest tests/ -q
```

## Бенчмарк

Из корня `experiments/`:

```bash
PYTHONPATH=. python -m benchmark.compare \
  --variant improvements/12-multi-agent-validation/variant \
  --tags critical
```

Подробности: [IMPLEMENTATION.md](../IMPLEMENTATION.md)
