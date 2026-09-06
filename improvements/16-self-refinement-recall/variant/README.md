# Baseline Agent Agent — variant 16 (Self-Refinement Recall)

Скопировано из `improvements/12-multi-agent-validation/variant/` (Worker +
Validator + tier-1/tier-2 gating scaffolding) и дополнено новым модулем
`recall_refine.py`: детектор молчаливого пропуска факта (omission) +
одно дополнительное LLM-переписывание черновика, если детектор сработал.

Worker + Validator (`risk_signals.py`, `symbolic_precheck.py`,
`validator_agent.py`) ловит FABRICATION — агент утверждает что-то неверное.
`recall_refine.py` ловит другой режим — OMISSION: агент ничего не
утверждает неверно, он просто не включает в ответ факт, который
пользователь просит напомнить/уточнить.

## Запуск

```bash
python basic_agent.py "Привет"
python basic_agent.py -i
```

## Тесты

```bash
PYTHONPATH=. pytest tests/test_recall_refine.py -q
PYTHONPATH=. pytest tests/ -q
```

## Бенчмарк

Из корня `experiments/`:

```bash
PYTHONPATH=. python -m benchmark.compare \
  --variant improvements/16-self-refinement-recall/variant \
  --tags critical
```

Подробности: [IMPLEMENTATION.md](../IMPLEMENTATION.md)
