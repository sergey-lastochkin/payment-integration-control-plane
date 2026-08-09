# Architecture

The business operation is the aggregate root. `PaymentService` coordinates validation, an `IntegrationRepository` and a `BankAdapter`; it does not own persistence or transport details. Repository writes preserve attempt/error evidence separately from state transitions. Adapter envelopes contain an immutable operation ID and a checksum. Reconciliation is a separate read-side service because a statement row can arrive long after the send path and can be replayed independently.

The local SQLite repository gives the demo a real crash/reopen boundary. The BSL module mirrors the contract expected from 1C registers and HTTP handlers but does not claim configuration-independent runtime compatibility.
