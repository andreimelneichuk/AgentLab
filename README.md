# Basic Agent Experiments

Репозиторий для экспериментов с агентом Basic.

## Структура

```
experiments/
├── benchmark/           # общий бенчмарк (сценарии, mock MCP, отчёты)
├── config.benchmark.yml # конфиг LLM/MCP для прогонов
├── tests/               # unit-тесты бенчмарка
├── requirements.txt
├── original/            # эталонный вариант агента (не менять)
│   ├── agent_core.py
│   ├── prompts/
│   └── ...
└── <variant>/           # копии для доработок
```

## Бенчмарк (из корня experiments/)

Каталог: **~500+ ходов**, **4 MCP-сервера**, **~25 инструментов** (core, hr, crm, sse).

```bash
source original/.venv/bin/activate
pip install -r requirements.txt

# перегенерация каталога (опционально)
PYTHONPATH=. python -m benchmark.generate_catalog --turns 500

# терминал 1 — mock MCP (все 4 mount на :19100)
python -m benchmark.mcp_mock_server

# терминал 2 — полный прогон (долго, часы)
PYTHONPATH=. python -m benchmark.compare --variant original

# быстрый smoke (~5 сценариев с тегом smoke + legacy critical)
PYTHONPATH=. python -m benchmark.compare --variant original --tags smoke

# только сгенерированный каталог
PYTHONPATH=. python -m benchmark.compare --variant original --tags catalog --limit 20

# эталон
PYTHONPATH=. python -m benchmark.compare --variant original

# другой вариант
PYTHONPATH=. python -m benchmark.compare --variant my-tweak-v1

# сравнить два варианта
PYTHONPATH=. python -m benchmark.compare --variant original --variant my-tweak-v1 --metrics comparative
```

## CLI агента (внутри варианта)

```bash
cd original
source .venv/bin/activate
python basic_agent.py -i
```

## Новый эксперимент

```bash
cp -r original my-tweak-v1
# правки в my-tweak-v1/, затем:
PYTHONPATH=. python -m benchmark.compare --variant my-tweak-v1
```

Папку `original` не меняйте — она база для сравнения.
