from __future__ import annotations

import multiprocessing
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from payment_orchestration.adapters import FileClientBankAdapter, MockBankAdapter
from payment_orchestration.domain import (
    BankStatementRow,
    CallbackConflictError,
    Party,
    Payment,
    PaymentStatus,
)
from payment_orchestration.reconciliation import Reconciler
from payment_orchestration.repository import SQLiteIntegrationRepository
from payment_orchestration.service import PaymentService


def payment(reference: str = "doc-durable", amount: str = "100.00") -> Payment:
    return Payment(
        reference,
        "request-durable",
        "42",
        date(2026, 8, 11),
        Party("Synthetic Payer", "PAYER", "PAYER-KPP", "PAYER-ACCOUNT"),
        Party("Synthetic Recipient", "RECIPIENT", "RECIPIENT-KPP", "RECIPIENT-ACCOUNT", "BIK"),
        Decimal(amount),
        "RUB",
        "Synthetic durable payment",
    )


def statement(item: Payment, *, amount: str = "100.00") -> BankStatementRow:
    return BankStatementRow(
        "transport-row-1",
        item.date,
        item.recipient.account,
        Decimal(amount),
        "RUB",
        external_bank_id="bank-transaction-1",
        operation_id=item.operation_id(),
        source="bank_file",
        statement_reference="statement-2026-08-11",
    )


def _send_worker(path: str, outbox: str, item: Payment, ready, start, output) -> None:
    repository = SQLiteIntegrationRepository(path)
    service = PaymentService(repository, FileClientBankAdapter(Path(outbox)))
    ready.put("started")
    start.wait(10)
    record = service.send(item)
    output.put({"status": record.status, "attempts": record.attempts})
    repository.close()


def _callback_worker(path: str, item: Payment, ready, start, output) -> None:
    repository = SQLiteIntegrationRepository(path)
    service = PaymentService(repository, MockBankAdapter())
    ready.put("started")
    start.wait(10)
    record = service.callback(
        item.operation_id(), "accepted", "bank-accepted", event_id="event-accepted"
    )
    output.put(record.status)
    repository.close()


def _statement_worker(path: str, item: Payment, ready, start, output) -> None:
    repository = SQLiteIntegrationRepository(path)
    matcher = Reconciler(repository)
    ready.put("started")
    start.wait(10)
    result = matcher.match([statement(item)], [item])[0]
    output.put({"status": result["status"], "replayed": result["replayed"]})
    repository.close()


def test_reconciliation_ledger_survives_reopen_and_detects_payload_conflict(tmp_path):
    path = tmp_path / "operations.sqlite"
    item = payment()
    first = SQLiteIntegrationRepository(str(path))
    initial = Reconciler(first).match([statement(item)], [item])[0]
    first.close()

    reopened = SQLiteIntegrationRepository(str(path))
    matcher = Reconciler(reopened)
    replayed = matcher.match([statement(item)], [item])[0]
    conflicting = matcher.match([statement(item, amount="101.00")], [item])[0]

    assert initial["status"] == "matched"
    assert replayed["replayed"] is True
    assert conflicting["status"] == "manual_check"
    assert conflicting["reason"] == "row_identity_payload_conflict"


def test_callback_inbox_is_durable_and_conflict_is_not_silent(tmp_path):
    path = tmp_path / "operations.sqlite"
    item = payment()
    first = SQLiteIntegrationRepository(str(path))
    service = PaymentService(first, MockBankAdapter())
    service.send(item)
    service.callback(item.operation_id(), "accepted", "bank-1", event_id="event-1")
    first.close()

    reopened = SQLiteIntegrationRepository(str(path))
    retry = PaymentService(reopened, MockBankAdapter()).callback(
        item.operation_id(), "accepted", "bank-1", event_id="event-1"
    )
    with pytest.raises(CallbackConflictError):
        PaymentService(reopened, MockBankAdapter()).callback(
            item.operation_id(), "accepted", "bank-2", event_id="event-1"
        )

    assert retry.status == PaymentStatus.ACCEPTED
    assert reopened.get(item.operation_id()).status == PaymentStatus.MANUAL_CHECK
    assert reopened.connection.execute(
        "SELECT conflict FROM callback_inbox WHERE event_id = 'event-1'"
    ).fetchone()[0] == 1


def test_unknown_send_outcome_is_recovered_without_blind_resend(tmp_path):
    path = tmp_path / "operations.sqlite"
    item = payment()
    faulting = SQLiteIntegrationRepository(str(path), fault_at="send_response_before_persist")
    with pytest.raises(RuntimeError, match="send_response_before_persist"):
        PaymentService(faulting, MockBankAdapter()).send(item)
    assert faulting.get(item.operation_id()).status == PaymentStatus.SENDING
    faulting.close()

    reopened = SQLiteIntegrationRepository(str(path))
    safe_adapter = MockBankAdapter()
    recovered = PaymentService(reopened, safe_adapter)
    record = recovered.send(item)

    assert [row.operation_id for row in recovered.recovered_operations] == [item.operation_id()]
    assert record.status == PaymentStatus.OUTCOME_UNKNOWN
    assert safe_adapter.sent == {}
    assert recovered.recovery_contract == "idempotent_retry_requires_explicit_operator_policy"


def test_controlled_callback_and_reconciliation_crashes_roll_back(tmp_path):
    item = payment()
    transition_path = tmp_path / "transition.sqlite"
    transition_repo = SQLiteIntegrationRepository(
        str(transition_path), fault_at="transition_before_commit"
    )
    with pytest.raises(RuntimeError, match="transition_before_commit"):
        PaymentService(transition_repo, MockBankAdapter()).prepare(item)
    transition_repo.close()
    transition_reopened = SQLiteIntegrationRepository(str(transition_path))
    assert transition_reopened.get(item.operation_id()).status == PaymentStatus.PREPARED

    callback_path = tmp_path / "callback.sqlite"
    callback_repo = SQLiteIntegrationRepository(str(callback_path), fault_at="callback_before_commit")
    callback_service = PaymentService(callback_repo, MockBankAdapter())
    callback_service.send(item)
    with pytest.raises(RuntimeError, match="callback_before_commit"):
        callback_service.callback(item.operation_id(), "accepted", event_id="callback-fault")
    callback_repo.close()

    callback_reopened = SQLiteIntegrationRepository(str(callback_path))
    assert callback_reopened.get(item.operation_id()).status == PaymentStatus.SENT
    assert callback_reopened.connection.execute("SELECT COUNT(*) FROM callback_inbox").fetchone()[0] == 0
    assert PaymentService(callback_reopened, MockBankAdapter()).callback(
        item.operation_id(), "accepted", event_id="callback-fault"
    ).status == PaymentStatus.ACCEPTED

    reconciliation_path = tmp_path / "reconciliation.sqlite"
    faulting_ledger = SQLiteIntegrationRepository(
        str(reconciliation_path), fault_at="reconciliation_before_commit"
    )
    with pytest.raises(RuntimeError, match="reconciliation_before_commit"):
        Reconciler(faulting_ledger).match([statement(item)], [item])
    faulting_ledger.close()
    reopened_ledger = SQLiteIntegrationRepository(str(reconciliation_path))
    assert reopened_ledger.connection.execute(
        "SELECT COUNT(*) FROM reconciliation_entries"
    ).fetchone()[0] == 0
    assert Reconciler(reopened_ledger).match([statement(item)], [item])[0]["status"] == "matched"


def test_legacy_operations_database_is_migrated_without_data_loss(tmp_path):
    path = tmp_path / "legacy.sqlite"
    initial = SQLiteIntegrationRepository(str(path))
    item = payment()
    PaymentService(initial, MockBankAdapter()).send(item)
    initial.close()
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE schema_meta")
    raw.execute("DROP TABLE reconciliation_entries")
    raw.execute("DROP TABLE callback_inbox")
    raw.commit()
    raw.close()

    migrated = SQLiteIntegrationRepository(str(path))
    assert migrated.schema_version == SQLiteIntegrationRepository.LATEST_SCHEMA_VERSION
    assert migrated.get(item.operation_id()).status == PaymentStatus.SENT
    assert migrated.connection.execute(
        "SELECT name FROM sqlite_master WHERE name = 'reconciliation_entries'"
    ).fetchone() is not None


def test_multiprocess_claim_allows_one_external_send(tmp_path):
    context = multiprocessing.get_context("spawn")
    path = str(tmp_path / "operations.sqlite")
    outbox = str(tmp_path / "outbox")
    item = payment("doc-multiprocess")
    ready = context.Queue()
    output = context.Queue()
    start = context.Event()
    workers = [
        context.Process(target=_send_worker, args=(path, outbox, item, ready, start, output))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    assert {ready.get(timeout=10), ready.get(timeout=10)} == {"started"}
    start.set()
    for worker in workers:
        worker.join(15)
        assert worker.exitcode == 0

    results = [output.get(timeout=5), output.get(timeout=5)]
    final = SQLiteIntegrationRepository(path)
    assert final.get(item.operation_id()).attempts == 1
    assert final.get(item.operation_id()).status == PaymentStatus.SENT
    assert len(list(Path(outbox).glob("*.json"))) == 1
    assert {row["status"] for row in results} <= {"sending", "sent"}


def test_multiprocess_callback_and_statement_duplicate_intake(tmp_path):
    context = multiprocessing.get_context("spawn")
    callback_path = str(tmp_path / "callbacks.sqlite")
    item = payment("doc-callback-race")
    seed = SQLiteIntegrationRepository(callback_path)
    PaymentService(seed, MockBankAdapter()).send(item)
    seed.close()

    ready, output, start = context.Queue(), context.Queue(), context.Event()
    workers = [
        context.Process(target=_callback_worker, args=(callback_path, item, ready, start, output))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    ready.get(timeout=10)
    ready.get(timeout=10)
    start.set()
    for worker in workers:
        worker.join(15)
        assert worker.exitcode == 0
    assert [output.get(timeout=5), output.get(timeout=5)] == ["accepted", "accepted"]
    final = SQLiteIntegrationRepository(callback_path)
    assert len([event for event in final.get(item.operation_id()).audit if event.new_status == "accepted"]) == 1

    ledger_path = str(tmp_path / "statements.sqlite")
    ready, output, start = context.Queue(), context.Queue(), context.Event()
    workers = [
        context.Process(target=_statement_worker, args=(ledger_path, item, ready, start, output))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    ready.get(timeout=10)
    ready.get(timeout=10)
    start.set()
    for worker in workers:
        worker.join(15)
        assert worker.exitcode == 0
    results = [output.get(timeout=5), output.get(timeout=5)]
    assert [row["replayed"] for row in results].count(False) == 1
    assert [row["replayed"] for row in results].count(True) == 1
