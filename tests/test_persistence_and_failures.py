import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest

from payment_orchestration.core import (
    BankStatementRow,
    ChecksumError,
    DuplicatePaymentError,
    FileClientBankAdapter,
    MockBankAdapter,
    Party,
    Payment,
    PaymentService,
    Reconciler,
    Registry,
    SQLiteIntegrationRepository,
    verify_envelope,
)


def make_payment(ref="doc-1", amount="100.00"):
    return Payment(
        ref,
        "request-1",
        "1",
        date(2026, 1, 10),
        Party("Synthetic Org", "SYNTH-INN-PAYER", "SYNTH-KPP-PAYER", "SYNTH-PAYER"),
        Party(
            "Synthetic Vendor",
            "SYNTH-INN-RECIPIENT",
            "SYNTH-KPP-RECIPIENT",
            "SYNTH-RECIPIENT",
            "044000000",
            "Synthetic Bank",
        ),
        Decimal(amount),
        "RUB",
        "Synthetic services under demo contract",
    )


def test_sqlite_repository_survives_reopen(tmp_path):
    path = tmp_path / "operations.sqlite"
    payment = make_payment()
    first = SQLiteIntegrationRepository(str(path))
    PaymentService(first, MockBankAdapter()).send(payment)
    first.connection.close()
    reopened = SQLiteIntegrationRepository(str(path))
    assert reopened.get(payment.operation_id()).status == "sent"
    assert reopened.get(payment.operation_id()).attempts == 1


def test_same_operation_changed_amount_is_conflict():
    from payment_orchestration.core import Registry

    repository = Registry()
    repository.register(make_payment(amount="100"))
    with pytest.raises(DuplicatePaymentError):
        repository.register(make_payment(amount="101"))


def test_timeout_records_attempt_and_error():
    from payment_orchestration.core import Registry

    payment = make_payment()
    repository = Registry()
    service = PaymentService(
        repository, MockBankAdapter(TimeoutError("synthetic timeout"))
    )
    with pytest.raises(TimeoutError):
        service.send(payment)
    record = repository.get(payment.operation_id())
    assert record.attempts == 1 and "synthetic timeout" in record.last_error


def test_callback_event_is_idempotent():
    from payment_orchestration.core import Registry

    payment = make_payment()
    service = PaymentService(Registry(), MockBankAdapter())
    service.send(payment)
    service.callback(payment.operation_id(), "accepted", event_id="event-42")
    service.callback(payment.operation_id(), "accepted", event_id="event-42")
    record = service.registry.get(payment.operation_id())
    assert len(record.callback_ids) == 1
    assert len([event for event in record.audit if event.new_status == "accepted"]) == 1


def test_file_envelope_detects_tampering(tmp_path):
    adapter = FileClientBankAdapter(tmp_path)
    adapter.send(make_payment())
    path = next(tmp_path.iterdir())
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["payload"]["amount"] = "999"
    with pytest.raises(ChecksumError):
        verify_envelope(envelope)


def test_statement_row_replay_is_reported():
    payment = make_payment()
    row = BankStatementRow(
        "row-1",
        date(2026, 1, 10),
        payment.recipient.account,
        payment.amount,
        "RUB",
        operation_id=payment.operation_id(),
    )
    reconciler = Reconciler()
    assert reconciler.match([row], [payment])[0]["status"] == "matched"
    assert reconciler.match([row], [payment])[0]["replayed"] is True


def test_composite_match_requires_confident_evidence():
    payment = make_payment()
    row = {
        "account": "OTHER",
        "amount": "100.00",
        "date": "2026-01-10",
        "currency": "RUB",
    }
    result = Reconciler().match([row], [payment])[0]
    assert result["status"] == "manual_check" and result["score"] < 60


def test_audit_has_every_legal_transition():
    from payment_orchestration.core import Registry

    payment = make_payment()
    service = PaymentService(Registry(), MockBankAdapter())
    service.send(payment)
    service.callback(payment.operation_id(), "accepted", event_id="accepted")
    service.callback(payment.operation_id(), "executed", event_id="executed")
    assert [
        (event.old_status, event.new_status)
        for event in service.registry.get(payment.operation_id()).audit
    ] == [
        ("prepared", "ready_to_send"),
        ("ready_to_send", "sending"),
        ("sending", "sent"),
        ("sent", "accepted"),
        ("accepted", "executed"),
    ]


def test_sqlite_parallel_register_preserves_existing_state(tmp_path):
    path = str(tmp_path / "operations.sqlite")
    payment = make_payment()
    first = SQLiteIntegrationRepository(path)
    service = PaymentService(first, MockBankAdapter())
    service.send(payment)
    service.callback(payment.operation_id(), "accepted", event_id="callback-1")

    repositories = [first] + [SQLiteIntegrationRepository(path) for _ in range(7)]
    start = threading.Barrier(len(repositories) + 1)

    def register(repository):
        start.wait()
        return repository.register(payment)

    with ThreadPoolExecutor(max_workers=len(repositories)) as pool:
        futures = [pool.submit(register, repository) for repository in repositories]
        start.wait()
        records = [future.result() for future in futures]

    persisted = first.get(payment.operation_id())
    assert all(record.status == "accepted" for record in records)
    assert persisted.attempts == 1
    assert persisted.callback_ids == {"callback-1"}
    assert [(event.old_status, event.new_status) for event in persisted.audit] == [
        ("prepared", "ready_to_send"),
        ("ready_to_send", "sending"),
        ("sending", "sent"),
        ("sent", "accepted"),
    ]


def test_parallel_send_claims_external_side_effect_once(tmp_path):
    class BlockingAdapter:
        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()
            self.entered = threading.Event()
            self.release = threading.Event()

        def send(self, payment):
            with self.lock:
                self.calls += 1
            self.entered.set()
            assert self.release.wait(2)
            return "bank-once"

    payment = make_payment()
    adapter = BlockingAdapter()
    service = PaymentService(
        SQLiteIntegrationRepository(str(tmp_path / "ops.sqlite")), adapter
    )
    workers = 8
    start = threading.Barrier(workers + 1)

    def send_once():
        start.wait()
        return service.send(payment)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(send_once) for _ in range(workers)]
        start.wait()
        assert adapter.entered.wait(2)
        adapter.release.set()
        records = [future.result() for future in futures]

    assert adapter.calls == 1
    assert service.registry.get(payment.operation_id()).status == "sent"
    assert service.registry.get(payment.operation_id()).attempts == 1
    assert {record.status for record in records} <= {"sending", "sent"}


def test_status_callback_before_send_response_keeps_stronger_state(tmp_path):
    payment = make_payment()
    repository = SQLiteIntegrationRepository(str(tmp_path / "ops.sqlite"))

    class CallbackFirstAdapter:
        service: PaymentService

        def send(self, value):
            self.service.callback(
                value.operation_id(), "accepted", "bank-42", event_id="status-42"
            )
            return "bank-42"

    adapter = CallbackFirstAdapter()
    service = PaymentService(repository, adapter)
    adapter.service = service
    assert service.send(payment).status == "accepted"
    assert repository.get(payment.operation_id()).attempts == 1


def test_manual_check_can_be_closed_with_audited_decision():
    payment = make_payment()
    service = PaymentService(Registry(), MockBankAdapter())
    service.send(payment)
    service.callback(payment.operation_id(), "manual_check", event_id="dispute-1")
    closed = service.resolve_manual(
        payment.operation_id(), "executed", "statement and bank status agree", "operator-1"
    )
    assert closed.status == "executed"
    assert closed.audit[-1].source == "manual_resolution"
