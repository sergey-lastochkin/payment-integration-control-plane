# ADR-003: external adapter capabilities

## Context

SQLite может защитить локальный claim, но не может доказать поведение внешнего
банковского канала после потери ответа.

## Decision

Адаптер явно объявляет `SendSemantics` и `StatusLookup`. Mock и file adapters
честно объявляют детерминированную локальную идемпотентность и отсутствие
status lookup.

## Consequences

Новый банковский adapter не получает capabilities по умолчанию. Для неизвестного
канала blind retry после `outcome_unknown` запрещён.
