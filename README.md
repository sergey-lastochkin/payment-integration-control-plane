# 1C Payment Orchestration

## What

Clean-room reference implementation of an outgoing-payment control plane for a legacy 1C system. It validates a payment, records a durable integration state, produces a deterministic bank envelope, consumes idempotent callbacks, and reconciles synthetic statement rows.

## Why

A direct “send document and mark paid” script loses the distinction between a business operation and a transport attempt. Timeouts, duplicate callbacks, repeated statement files and ambiguous matches can then create duplicate payments or false execution statuses. This project makes those boundaries explicit.

## Architecture

- `domain.py`: payment model, stable operation ID, validation and legal state graph.
- `repository.py`: in-memory and SQLite integration-status repositories plus audit history and an atomic local send claim.
- `adapters.py`: local mock/file bank adapters and SHA-256 exchange envelopes.
- `service.py`: guarded orchestration and callback idempotency.
- `reconciliation.py`: composite matching, ambiguity escalation and statement replay protection.
- `bsl/PaymentIntegration.bsl`: 1C-side reference handlers and register contracts.

## Key engineering decisions

- Idempotency is keyed by an immutable business operation, not by a retry attempt.
- Registering the same operation with changed payment fields is a conflict.
- Every state change is guarded and appended to the audit trail.
- SQLite registration uses `INSERT OR IGNORE` followed by a read, so a repeated register cannot overwrite attempts, callbacks or audit.
- `ready_to_send → sending` is a compare-and-set claim before `adapter.send()`; concurrent local workers do not call the adapter twice.
- A bank timeout leaves the claim in `sending`: the remote outcome is unknown and must be reconciled before retry.
- Reconciliation requires a confident unique candidate; ties go to `manual_check`.
- File payloads are deterministic and checksum-verified before reuse.

## Run

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

The adapters call no real bank. `examples/payment.json` and all test identifiers are synthetic.

## Test

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

The suite covers state guards, a ThreadPoolExecutor single-send claim, parallel SQLite registration, callbacks, SQLite reopen, timeout evidence, checksum tampering, operation conflicts, composite ambiguity, transition-spec/BSL parity and statement replay.

## Limitations

- BSL was statically reviewed but **not runtime-tested on a 1C platform**.
- The file adapter is a local protocol demonstrator, not a client-bank format.
- Authentication, signing, bank-specific status mappings and production migrations are not included.
- Local claim safety is tested for one process/SQLite database. It is not an exactly-once guarantee across an arbitrary bank API; a real adapter must accept the operation/batch ID as an idempotency key.
- No real client, bank account, company or payment data is present.
