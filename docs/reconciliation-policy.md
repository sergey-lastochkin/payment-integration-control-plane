# Reconciliation scoring policy

`ReconciliationPolicy` — начальная heuristic policy, а не модель с измеренной
точностью. Она применяется только как дополнительная сверка после сохранения
входящей строки в SQLite.

| Признак | Тип | Вес |
| --- | --- | ---: |
| External transaction id | Deterministic | 100 |
| `operation_id` | Deterministic | 95 |
| Ссылка на документ | Deterministic | 70 |
| Счёт получателя | Heuristic context | 25 |
| Сумма | Heuristic context | 25 |
| Дата | Heuristic context | 10 |
| Валюта | Heuristic context | 5 |
| Похожесть назначения | Heuristic context | 10 |

Один кандидат с суммой не ниже 60 может быть сопоставлен автоматически.
Равные лучшие оценки, неполные данные и конфликт внешнего идентификатора всегда
переходят в `manual_check`. Порог и веса не калиброваны на historical production
dataset: будущая калибровка требует обезличенных проверенных кейсов, учёта
ложных совпадений и пропусков.
