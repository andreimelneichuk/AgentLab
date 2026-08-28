# Судьи оконченности доработок

Каждая доработка оценивается по рубрике [`RUBRIC.md`](./RUBRIC.md).

## Артефакты

| Файл | Назначение |
|------|------------|
| `improvements/NN-*/JUDGE.md` | Краткая сводка (балл, вердикт, топ пробелов) |
| `improvement_reports/judge/judge_NN.md` | Полный отчёт судьи |
| `improvement_reports/judge/JUDGE_SUMMARY.md` | Сводная таблица всех 15 |

## Критерии (кратко)

5 измерений × 20 баллов: **спека**, **код**, **тесты**, **интеграция**, **production** (включая SR vs baseline 26%).

Вердикты: Stub → Draft → Alpha → Beta → Production-ready.

## Перезапуск судей

Запустить 15 code-reviewer субагентов с промптом из `RUBRIC.md` и данными бенчмарка из `improvement_reports/SUMMARY.md`.
