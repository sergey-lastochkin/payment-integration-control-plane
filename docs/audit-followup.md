# Durability follow-up

This note records the current implementation facts and the tests used to prove
the follow-up changes. It does not make claims about an external payment
channel.

| Finding | Current code | Assessment | Required change | Proof |
| --- | --- | --- | --- | --- |
| Statement replay is process-local | `Reconciler.processed_rows` is an in-memory dictionary. | Confirmed. | Store reconciliation intake and its decision in SQLite. | Reopen the database and submit the same row. |
| A statement row identity is too weak | The fallback fingerprint includes `row_id`; a reused external id with changed fields is not detected as a conflict. | Confirmed. | Prefer an external transaction id; otherwise derive a stable statement fingerprint and store a payload hash. | Re-submit an event id with different payload. |
| Callback deduplication lacks an inbox | Callback ids are serialized with the operation, but no payload hash is retained and state change plus dedup write use two writes. | Confirmed. | Persist a callback inbox row and transition atomically in one SQLite transaction. | Reopen, retry an event, then submit the same id with a changed payload. |
| A claimed send has no explicit restart recovery | A failed adapter call deliberately leaves `sending`, but startup does not classify it. | Partially correct. Keeping `sending` prevents a blind retry, but the recovery state is implicit. | Mark unfinished claims as `outcome_unknown` on repository recovery and prohibit blind resend. | Reopen after a fault injected after claim. |
| Send claiming is conditional | SQLite uses `UPDATE ... WHERE status = ready_to_send` and checks `rowcount` inside `BEGIN IMMEDIATE`. | Correct, but not proven across processes. | Keep the compare-and-set design and add a multiprocessing test. | Two independent SQLite connections race for one operation. |
| Database evolution is implicit | Schema setup only uses `CREATE TABLE IF NOT EXISTS`. | Confirmed. | Add a small numbered migration mechanism that upgrades an old operations-only database. | Open a legacy database and retain its operation. |
| Reconciliation weights are hidden magic numbers | Scores and thresholds live in `Reconciler._score`. | Confirmed. | Use an explicit heuristic policy and document that it is not production-calibrated. | Table-driven match matrix. |
| Operation identity and mutable content | `operation_id` is document-based while `business_fingerprint` contains mutable business fields; duplicate registration rejects a changed fingerprint. | Correct. | Preserve this behaviour and add regression coverage. | Register the same document after amount/purpose changes. |
