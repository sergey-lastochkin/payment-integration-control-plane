from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Protocol

from .domain import (
    TRANSITIONS,
    AuditEvent,
    DuplicatePaymentError,
    IntegrationRecord,
    Payment,
    PaymentStatus,
    TransitionError,
)


class IntegrationRepository(Protocol):
    def register(self, payment: Payment) -> IntegrationRecord: ...
    def get(self, operation_id: str) -> IntegrationRecord: ...
    def save(self, record: IntegrationRecord) -> None: ...
    def claim_send(self, operation_id: str) -> IntegrationRecord | None: ...


def now() -> str:
    return datetime.now(UTC).isoformat()


def transition_record(
    record: IntegrationRecord,
    new_status: str,
    source: str,
    message: str = "",
    external_id: str | None = None,
    event_id: str | None = None,
) -> IntegrationRecord:
    if new_status == record.status:
        return record
    if new_status not in TRANSITIONS.get(record.status, set()):
        raise TransitionError(f"{record.status} -> {new_status}")
    old_status = record.status
    record.status = new_status
    record.last_message = message
    if external_id:
        record.external_id = external_id
    record.audit.append(
        AuditEvent(old_status, new_status, source, message, external_id, event_id)
    )
    return record


class Registry:
    """In-memory repository used only by narrow unit tests and the simulator."""

    def __init__(self) -> None:
        self.records: dict[str, IntegrationRecord] = {}
        self._lock = threading.RLock()

    def register(self, payment: Payment) -> IntegrationRecord:
        with self._lock:
            operation_id = payment.operation_id()
            fingerprint = payment.business_fingerprint()
            existing = self.records.get(operation_id)
            if existing:
                if existing.payment_fingerprint and existing.payment_fingerprint != fingerprint:
                    raise DuplicatePaymentError(
                        f"operation {operation_id} has different immutable fields"
                    )
                return existing
            record = IntegrationRecord(
                operation_id,
                payment.document_ref,
                payment.request_ref,
                payment.status,
                fingerprint,
            )
            self.records[operation_id] = record
            return record

    def get(self, operation_id: str) -> IntegrationRecord:
        with self._lock:
            return self.records[operation_id]

    def save(self, record: IntegrationRecord) -> None:
        with self._lock:
            self.records[record.operation_id] = record

    def transition(
        self,
        operation_id: str,
        new_status: str,
        source: str,
        message: str = "",
        external_id: str | None = None,
        event_id: str | None = None,
    ) -> IntegrationRecord:
        with self._lock:
            record = transition_record(
                self.get(operation_id),
                new_status,
                source,
                message,
                external_id,
                event_id,
            )
            self.save(record)
            return record

    def claim_send(self, operation_id: str) -> IntegrationRecord | None:
        with self._lock:
            record = self.get(operation_id)
            if record.status != PaymentStatus.READY_TO_SEND:
                return None
            record.attempts += 1
            return transition_record(
                record,
                PaymentStatus.SENDING,
                "send-claim",
                "external side effect claimed",
            )


class SQLiteIntegrationRepository:
    """SQLite persistence with explicit local transaction boundaries.

    It provides at-least-once event intake with durable duplicate suppression.
    It does not claim exactly-once execution by an external payment channel.
    """

    LATEST_SCHEMA_VERSION = 3

    def __init__(self, path: str, *, fault_at: str | None = None) -> None:
        self._lock = threading.RLock()
        self._fault_at = fault_at
        self.connection = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=5000")
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            # Two processes can open a new database at exactly the same time.
            # The competing connection is enabling WAL; the subsequent schema
            # transaction waits on SQLite's busy timeout instead of failing the
            # worker before it reaches the real compare-and-set claim.
            if "locked" not in str(exc).lower():
                raise
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    @property
    def schema_version(self) -> int:
        row = self.connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def _migrate(self) -> None:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                existing = self.connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                has_operations = self.connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='payment_operations'"
                ).fetchone()
                version = int(existing["value"]) if existing else (1 if has_operations else 0)
                if version < 1:
                    self._create_operations_table()
                    version = 1
                if version < 2:
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS reconciliation_entries (
                            row_identity TEXT PRIMARY KEY,
                            source TEXT NOT NULL,
                            statement_reference TEXT NOT NULL,
                            payload_hash TEXT NOT NULL,
                            first_seen_at TEXT NOT NULL,
                            processed_at TEXT NOT NULL,
                            result_json TEXT NOT NULL,
                            matched_operation_id TEXT,
                            resolution TEXT NOT NULL,
                            metadata_json TEXT NOT NULL
                        )
                        """
                    )
                    version = 2
                if version < 3:
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS callback_inbox (
                            event_id TEXT PRIMARY KEY,
                            operation_id TEXT NOT NULL,
                            payload_hash TEXT NOT NULL,
                            received_at TEXT NOT NULL,
                            processed_at TEXT NOT NULL,
                            processing_result TEXT NOT NULL,
                            conflict INTEGER NOT NULL DEFAULT 0
                        )
                        """
                    )
                    version = 3
                self.connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(version),),
                )
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def _create_operations_table(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_operations (
                operation_id TEXT PRIMARY KEY,
                payment_ref TEXT NOT NULL,
                request_ref TEXT NOT NULL,
                status TEXT NOT NULL,
                payment_fingerprint TEXT NOT NULL,
                external_id TEXT,
                attempts INTEGER NOT NULL,
                last_error TEXT NOT NULL,
                last_message TEXT NOT NULL,
                callback_ids TEXT NOT NULL,
                audit TEXT NOT NULL
            )
            """
        )

    @contextmanager
    def _write_transaction(self):
        """Acquire SQLite's cross-process writer lock before making a decision."""

        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def _fault(self, point: str) -> None:
        if self._fault_at == point:
            raise RuntimeError(f"controlled fault at {point}")

    @staticmethod
    def _decode(row: sqlite3.Row) -> IntegrationRecord:
        return IntegrationRecord(
            operation_id=row["operation_id"],
            payment_ref=row["payment_ref"],
            request_ref=row["request_ref"],
            status=row["status"],
            payment_fingerprint=row["payment_fingerprint"],
            external_id=row["external_id"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            last_message=row["last_message"],
            callback_ids=set(json.loads(row["callback_ids"])),
            audit=[AuditEvent(**event) for event in json.loads(row["audit"])],
        )

    def register(self, payment: Payment) -> IntegrationRecord:
        record = IntegrationRecord(
            payment.operation_id(),
            payment.document_ref,
            payment.request_ref,
            payment.status,
            payment.business_fingerprint(),
        )
        with self._write_transaction():
            self.connection.execute(
                "INSERT OR IGNORE INTO payment_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._values(record),
            )
            existing = self._get_unlocked(record.operation_id)
        if existing.payment_fingerprint != record.payment_fingerprint:
            raise DuplicatePaymentError(
                f"operation {record.operation_id} has different immutable fields"
            )
        return existing

    def get(self, operation_id: str) -> IntegrationRecord:
        with self._lock:
            return self._get_unlocked(operation_id)

    def _get_unlocked(self, operation_id: str) -> IntegrationRecord:
        row = self.connection.execute(
            "SELECT * FROM payment_operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return self._decode(row)

    def save(self, record: IntegrationRecord) -> None:
        with self._write_transaction():
            self._write_unlocked(record)

    @staticmethod
    def _values(record: IntegrationRecord) -> tuple[object, ...]:
        return (
            record.operation_id,
            record.payment_ref,
            record.request_ref,
            record.status,
            record.payment_fingerprint,
            record.external_id,
            record.attempts,
            record.last_error,
            record.last_message,
            json.dumps(sorted(record.callback_ids)),
            json.dumps([asdict(event) for event in record.audit], ensure_ascii=False),
        )

    def _write_unlocked(self, record: IntegrationRecord) -> None:
        values = self._values(record)
        self.connection.execute(
            """
            INSERT INTO payment_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(operation_id) DO UPDATE SET
              status=excluded.status, external_id=excluded.external_id,
              attempts=excluded.attempts, last_error=excluded.last_error,
              last_message=excluded.last_message, callback_ids=excluded.callback_ids,
              audit=excluded.audit
            """,
            values,
        )

    def transition(
        self,
        operation_id: str,
        new_status: str,
        source: str,
        message: str = "",
        external_id: str | None = None,
        event_id: str | None = None,
    ) -> IntegrationRecord:
        with self._write_transaction():
            record = transition_record(
                self._get_unlocked(operation_id),
                new_status,
                source,
                message,
                external_id,
                event_id,
            )
            self._fault("transition_before_commit")
            self._write_unlocked(record)
            return record

    def claim_send(self, operation_id: str) -> IntegrationRecord | None:
        """Claim one worker through a durable compare-and-set update."""

        with self._write_transaction():
            record = self._get_unlocked(operation_id)
            if record.status != PaymentStatus.READY_TO_SEND:
                return None
            record.attempts += 1
            transition_record(
                record,
                PaymentStatus.SENDING,
                "send-claim",
                "external side effect claimed",
            )
            values = self._values(record)
            changed = self.connection.execute(
                """
                UPDATE payment_operations SET
                  status=?, external_id=?, attempts=?, last_error=?, last_message=?,
                  callback_ids=?, audit=?
                WHERE operation_id=? AND status=?
                """,
                (
                    values[3], values[5], values[6], values[7], values[8], values[9],
                    values[10], operation_id, PaymentStatus.READY_TO_SEND,
                ),
            )
            return record if changed.rowcount == 1 else None

    def recover_sending(self) -> list[IntegrationRecord]:
        """Classify unfinished send claims without retrying the external effect."""

        recovered: list[IntegrationRecord] = []
        with self._write_transaction():
            rows = self.connection.execute(
                "SELECT operation_id FROM payment_operations WHERE status = ?",
                (PaymentStatus.SENDING,),
            ).fetchall()
            for row in rows:
                record = transition_record(
                    self._get_unlocked(row["operation_id"]),
                    PaymentStatus.OUTCOME_UNKNOWN,
                    "startup_recovery",
                    "send claim survived restart; status lookup, reconciliation, or manual resolution required",
                )
                self._write_unlocked(record)
                recovered.append(record)
        return recovered

    def persist_reconciliation(
        self,
        *,
        row_identity: str,
        source: str,
        statement_reference: str,
        payload_hash: str,
        result: dict[str, Any],
        matched_operation_id: str | None,
        resolution: str,
        metadata: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Persist a reconciliation decision or return its durable replay/conflict."""

        with self._write_transaction():
            existing = self.connection.execute(
                "SELECT * FROM reconciliation_entries WHERE row_identity = ?",
                (row_identity,),
            ).fetchone()
            if existing is None:
                timestamp = now()
                self.connection.execute(
                    """
                    INSERT INTO reconciliation_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_identity,
                        source,
                        statement_reference,
                        payload_hash,
                        timestamp,
                        timestamp,
                        json.dumps(result, ensure_ascii=False, sort_keys=True),
                        matched_operation_id,
                        resolution,
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    ),
                )
                self._fault("reconciliation_before_commit")
                return "new", result
            if existing["payload_hash"] == payload_hash:
                return "replay", json.loads(existing["result_json"])
            conflict = {
                "status": "manual_check",
                "reason": "row_identity_payload_conflict",
                "candidate_count": 0,
                "score": 0,
                "matched_operation_id": None,
            }
            self.connection.execute(
                """
                UPDATE reconciliation_entries
                SET processed_at=?, result_json=?, matched_operation_id=NULL,
                    resolution='payload_conflict', metadata_json=?
                WHERE row_identity=?
                """,
                (
                    now(),
                    json.dumps(conflict, ensure_ascii=False, sort_keys=True),
                    json.dumps(
                        {"first_payload_hash": existing["payload_hash"], "conflict": True},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    row_identity,
                ),
            )
            self._fault("reconciliation_before_commit")
            return "conflict", conflict

    def apply_callback(
        self,
        *,
        operation_id: str,
        status: str,
        external_id: str | None,
        message: str,
        event_id: str,
        payload_hash: str,
    ) -> tuple[IntegrationRecord, str]:
        """Durably deduplicate a callback and its state transition in one commit."""

        with self._write_transaction():
            existing = self.connection.execute(
                "SELECT * FROM callback_inbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            record = self._get_unlocked(operation_id)
            if existing is not None:
                if existing["payload_hash"] == payload_hash:
                    return record, "duplicate"
                if PaymentStatus.MANUAL_CHECK in TRANSITIONS.get(record.status, set()):
                    record = transition_record(
                        record,
                        PaymentStatus.MANUAL_CHECK,
                        "callback_conflict",
                        "event id reused with a different payload",
                        external_id,
                        event_id,
                    )
                    self._write_unlocked(record)
                self.connection.execute(
                    "UPDATE callback_inbox SET conflict=1, processing_result='payload_conflict' WHERE event_id=?",
                    (event_id,),
                )
                self._fault("callback_before_commit")
                return record, "conflict"
            if status != record.status:
                record = transition_record(
                    record, status, "callback", message, external_id, event_id
                )
            record.callback_ids.add(event_id)
            self._write_unlocked(record)
            timestamp = now()
            self.connection.execute(
                """
                INSERT INTO callback_inbox VALUES (?, ?, ?, ?, ?, 'processed', 0)
                """,
                (event_id, operation_id, payload_hash, timestamp, timestamp),
            )
            self._fault("callback_before_commit")
            return record, "processed"
