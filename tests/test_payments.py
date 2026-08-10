from datetime import date
from decimal import Decimal

import pytest

from payment_orchestration.adapters import FileClientBankAdapter, MockBankAdapter
from payment_orchestration.domain import (
    Party,
    Payment,
    TransitionError,
    ValidationError,
    validate,
)
from payment_orchestration.reconciliation import Reconciler
from payment_orchestration.repository import Registry
from payment_orchestration.service import PaymentService


def payment(ref="doc-1", amount="100.00"):
    return Payment(
        ref,
        "req-1",
        "1",
        date(2026, 1, 10),
        Party("Org", "111", "222", "PA"),
        Party("Vendor", "333", "444", "RA", "BIK", "Bank"),
        Decimal(amount),
        "RUB",
        "Services",
    )


def test_stable_operation_id_and_duplicate_creation():
    p = payment()
    r = Registry()
    assert p.operation_id() == p.operation_id()
    assert r.register(p) is r.register(p)


def test_invalid_details():
    p = payment()
    p.recipient = Party("Vendor", "333", "444", "", "", "")
    with pytest.raises(ValidationError):
        validate(p)


def test_state_transition_validation():
    r = Registry()
    p = payment()
    r.register(p)
    with pytest.raises(TransitionError):
        r.transition(p.operation_id(), "executed", "x")


def test_duplicate_send(tmp_path):
    p = payment()
    r = Registry()
    a = FileClientBankAdapter(tmp_path)
    s = PaymentService(r, a)
    s.send(p)
    s.send(p)
    assert (
        len(list(tmp_path.iterdir())) == 1 and r.records[p.operation_id()].attempts == 1
    )


def test_callback_duplicate_and_flow():
    p = payment()
    r = Registry()
    s = PaymentService(r, MockBankAdapter())
    s.send(p)
    s.callback(p.operation_id(), "accepted", "x")
    s.callback(p.operation_id(), "accepted", "x")
    s.callback(p.operation_id(), "executed", "x")
    assert r.records[p.operation_id()].status == "executed"


def test_rejected_and_returned():
    for terminal in ("rejected", "returned"):
        p = payment(terminal)
        r = Registry()
        s = PaymentService(r, MockBankAdapter())
        s.send(p)
        s.callback(p.operation_id(), "accepted") if terminal == "returned" else None
        s.callback(p.operation_id(), terminal)
        assert r.records[p.operation_id()].status == terminal


def test_ambiguous_reconciliation():
    p1 = payment("a")
    p2 = payment("b")
    rows = [{"account": "RA", "amount": "100.00", "date": "2026-01-10"}]
    out = Reconciler().match(rows, [p1, p2])
    assert out[0]["status"] == "manual_check" and out[0]["candidate_count"] == 2


def test_reconciliation_operation_id():
    p = payment()
    out = Reconciler().match([{"operation_id": p.operation_id()}], [p])
    assert out[0]["payment"] is p
