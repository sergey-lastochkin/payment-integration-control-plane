# Подготовка реального тестового прогона

Статус: `LOCAL_N8N_BANK_PASS / UPP_BLOCKED`. Официальный Docker image n8n,
local FastAPI control plane и local bank emulator реально прогнаны по HTTP.
Тестовая УПП в этом прогоне не участвовала.

Здесь нет доступа к тестовой УПП или реальному банку. Каталог содержит
последовательность проверки УПП и воспроизводимый local stack. Маршрут n8n
отправляет данные только в bank emulator. Реальные платежи, рабочие реквизиты
и production credentials в сценарий не входят.

```text
real_run/
  checklist.md                 # десять шагов теста
  scripts/evidence.py          # создаёт и проверяет журнал
  local_stack/                 # pinned n8n + local FastAPI services + runner
  capture/                     # игнорируется Git
  templates/                   # JSON-шаблоны без реквизитов
```

Для УПП прогона координатор создаёт закрытый журнал. Команда запишет `run_id`,
время и commit SHA, но не подставит версию 1С и конфигурации: их нужно считать
из тестового контура.

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

## Локальный n8n + bank emulator

`local_stack` использует официальный `n8nio/n8n:1.82.3`. Все сервисы живут в
одной временной Docker network; с Mac опубликованы только
`127.0.0.1:5678`, `127.0.0.1:18080` и `127.0.0.1:18081`. n8n и обе SQLite базы
лежат в scoped named volumes, поэтому restart control plane и n8n не стирает
состояние в ходе прогона. Workflow состоит из стандартных Webhook, Set, IF и
HTTP Request узлов — Function node нет.

```bash
PYTHONPATH=src .venv/bin/python real_run/local_stack/run_e2e.py \
  --run-id real-n8n-bank-YYYY-MM-DD-01 \
  --public-output real_run/runs/local-n8n-bank-YYYY-MM-DD/summary.json
```

Runner сам проверяет восемь synthetic-сценариев и сохраняет подробные SQLite,
HTTP traces, container logs и build log только в `real_run/private/`. Перед
созданием public summary он откажется записывать строки с адресами local stack
или secret-shaped полями. Публичный summary указывает, что `one_c_participated`
равно `false`; его нельзя использовать как доказательство УПП.

Последний очищенный результат: [summary.json](runs/local-n8n-bank-2026-08-11/summary.json).
Он содержит восемь пройденных сценариев и n8n execution IDs, но не содержит
endpoint, credential, HTTP body, SQLite или container log.
