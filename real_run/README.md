# Подготовка реального тестового прогона

Статус: `REAL_RUN_BLOCKED` — локально не обнаружены доступные runtime 1С или n8n и не предоставлен изолированный тестовый контур.

Здесь нет доступа к тестовой УПП, n8n и банку. Каталог готовит последовательность проверки и журнал, который можно заполнить в закрытом тестовом контуре. Маршрут n8n отправляет данные только в bank emulator. Реальные платежи, рабочие реквизиты и production credentials в сценарий не входят.

```text
real_run/
  checklist.md                 # десять шагов теста
  scripts/evidence.py          # создаёт и проверяет журнал
  capture/                     # игнорируется Git
  templates/                   # JSON-шаблоны без реквизитов
```

Перед стартом координатор создаёт пустой журнал. Команда запишет `run_id`, время и commit SHA, но не подставит версию 1С и конфигурации: их нужно считать из тестового контура.

```bash
PYTHONPATH=src .venv/bin/python real_run/scripts/evidence.py init \
  --run-id upp-n8n-YYYY-MM-DD-01 \
  --out real_run/capture/upp-n8n-YYYY-MM-DD-01/run.json
```

В `capture/<run_id>/` появляются папки для обезличенных скриншотов 1С, выгрузки n8n workflow, HTTP-подтверждений и трассировок. Git игнорирует всё содержимое `capture/`; до публикации можно переносить только отдельно проверенные и обезличенные материалы.

После каждого шага журнал дополняется событием из JSON-файла. Разрешены `http`, `operation`, `n8n`, `transition`, `error` и `duplicate`. В событие попадает идентификатор или результат проверки, а не HTTP-заголовки, платёжные реквизиты и токены.

```bash
PYTHONPATH=src .venv/bin/python real_run/scripts/evidence.py add-event \
  --run real_run/capture/upp-n8n-YYYY-MM-DD-01/run.json \
  --event /secure/test-artifacts/one-redacted-event.json

PYTHONPATH=src .venv/bin/python real_run/scripts/evidence.py set \
  --run real_run/capture/upp-n8n-YYYY-MM-DD-01/run.json \
  --field platform_1c_version --value '"8.3.x"'
```

Проверка в конце не даёт пометить прогон готовым, пока отсутствуют версия платформы, версия конфигурации, request id, `operation_id`, id выполнения n8n, история переходов, длительность или результат подавления дублей.

```bash
PYTHONPATH=src .venv/bin/python real_run/scripts/evidence.py check \
  --run real_run/capture/upp-n8n-YYYY-MM-DD-01/run.json
```

Шаблон n8n находится в [templates/n8n-bank-emulator-workflow.json](templates/n8n-bank-emulator-workflow.json). Перед импортом в n8n требуется задать `BANK_EMULATOR_URL` для безопасного эмулятора, а не адрес банка.
