# Baseline Agent Agent — variant 11 (neurosymbolic guardrails)

Эталон `original/` + **нейросимволические guardrails** (policy-as-code в `rules/`).

## Запуск

```bash
source .venv/bin/activate
python basic_agent.py "Привет"
python basic_agent.py -i
```

Бенчмарк из корня `experiments/`:

```bash
PYTHONPATH=. python -m benchmark.compare --variant improvements/11-neurosymbolic-guardrails/variant
```

## Тесты

```bash
PYTHONPATH=. pytest tests/test_guardrails.py -q
PYTHONPATH=. pytest tests/ -q
```

Подробности реализации: [IMPLEMENTATION.md](./IMPLEMENTATION.md)
