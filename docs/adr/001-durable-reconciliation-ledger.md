# ADR-001: durable reconciliation ledger

## Context

Повтор строки выписки ранее подавлялся только в памяти одного процесса.

## Decision

Сохранять identity строки, payload hash и решение в SQLite в существующем
repository слое. Повтор с тем же payload возвращает сохранённый результат;
повтор identity с иным payload требует ручной проверки.

## Consequences

Replay protection переживает reopen базы. Это не заменяет банковский статус и
не делает сверку гарантией exactly-once платежа.
