# Baseline Agent Agent — Graph-RAG variant

Вариант с in-memory Graph-RAG для HR/policy агрегатов и связей сущностей.

## Отличия от original

- Локальный tool `graph_query` (не MCP)
- Prompt rules: статистика только из графа; пустой результат → честный отказ
- Dict-based knowledge graph без Neo4j

## Запуск

```bash
source .venv/bin/activate
python basic_agent.py "Сколько сотрудников в Engineering с политикой Remote Work?"
python basic_agent.py -i
```

## Тесты

```bash
PYTHONPATH=. pytest tests/ -q
```

## Бенчмарк

```bash
cd ../../..
PYTHONPATH=. python -m benchmark.compare --variant improvements/14-graph-rag/variant --tags smoke
```

Подробности: `../IMPLEMENTATION.md`
