# ADR-002: unknown external outcome recovery

## Context

После перехода в `sending` внешний эффект мог произойти до потери ответа.

## Decision

При следующем запуске незавершённый claim становится `outcome_unknown`. Он не
возвращается автоматически в очередь отправки.

## Consequences

Следующий шаг — status lookup, reconciliation или manual resolution. Повторная
внешняя отправка допускается только отдельной политикой при доказанной
идемпотентности канала.
