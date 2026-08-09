from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from typing import Protocol

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
    """In-memory repository used in unit tests and the simulator."""

    def __init__(self) -> None:
        self.records: dict[str, IntegrationRecord] = {}
        self._lock = threading.RLock()

    def register(self, payment: Payment) -> IntegrationRecord:
        with self._lock:
            operation_id = payment.operation_id()
            fingerprint = payment.business_fingerprint()
            existing = self.records.get(operation_id)
            if existing:
                if (
                    existing.payment_fingerprint
                    and existing.payment_fingerprint != fingerprint
                ):
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
        """Claim the one local worker allowed to call the external adapter."""
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
    """Durable single-process repository with transactional claims for sends."""

    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
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
        self.connection.commit()

    @contextmanager
    def _write_transaction(self):
        """Serialize local writers and acquire SQLite's writer lock before reads."""
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

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
        # INSERT OR IGNORE is the idempotency boundary.  A competing register
        # never writes an UPSERT payload over attempts, callbacks or audit data.
        with self._write_transaction():
            self.connection.execute(
                """
                INSERT OR IGNORE INTO payment_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
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
            self._write_unlocked(record)
            return record

    def claim_send(self, operation_id: str) -> IntegrationRecord | None:
        """Compare-and-set ``ready_to_send`` before the external side effect."""
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
                    values[3],
                    values[5],
                    values[6],
                    values[7],
                    values[8],
                    values[9],
                    values[10],
                    operation_id,
                    PaymentStatus.READY_TO_SEND,
                ),
            )
            return record if changed.rowcount == 1 else None
