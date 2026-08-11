from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256


class PaymentStatus(StrEnum):
    PREPARED = "prepared"
    READY_TO_SEND = "ready_to_send"
    SENDING = "sending"
    OUTCOME_UNKNOWN = "outcome_unknown"
    SENT = "sent"
    ACCEPTED = "accepted"
    EXECUTED = "executed"
    REJECTED = "rejected"
    RETURNED = "returned"
    MANUAL_CHECK = "manual_check"


STATUSES = {status.value for status in PaymentStatus}
TRANSITIONS: dict[str, set[str]] = {
    PaymentStatus.PREPARED: {PaymentStatus.READY_TO_SEND, PaymentStatus.MANUAL_CHECK},
    PaymentStatus.READY_TO_SEND: {PaymentStatus.SENDING, PaymentStatus.MANUAL_CHECK},
    # A claimed send deliberately remains ``sending`` after an adapter exception:
    # the remote side effect may have happened even when its response was lost.
    # A bank status can arrive through another channel before the sender gets
    # the HTTP response. It is stronger evidence than the missing response.
    PaymentStatus.SENDING: {
        PaymentStatus.OUTCOME_UNKNOWN,
        PaymentStatus.SENT,
        PaymentStatus.ACCEPTED,
        PaymentStatus.REJECTED,
        PaymentStatus.MANUAL_CHECK,
    },
    # ``outcome_unknown`` means a local worker cannot prove whether the remote
    # side effect happened. It is deliberately not a retry queue.
    PaymentStatus.OUTCOME_UNKNOWN: {
        PaymentStatus.ACCEPTED,
        PaymentStatus.EXECUTED,
        PaymentStatus.REJECTED,
        PaymentStatus.RETURNED,
        PaymentStatus.MANUAL_CHECK,
    },
    PaymentStatus.SENT: {
        PaymentStatus.ACCEPTED,
        PaymentStatus.REJECTED,
        PaymentStatus.MANUAL_CHECK,
    },
    PaymentStatus.ACCEPTED: {
        PaymentStatus.EXECUTED,
        PaymentStatus.REJECTED,
        PaymentStatus.RETURNED,
        PaymentStatus.MANUAL_CHECK,
    },
    PaymentStatus.EXECUTED: set(),
    PaymentStatus.REJECTED: set(),
    PaymentStatus.RETURNED: set(),
    # Manual resolution must leave an auditable terminal decision instead of
    # turning the operation into a permanent dead end.
    PaymentStatus.MANUAL_CHECK: {
        PaymentStatus.EXECUTED,
        PaymentStatus.REJECTED,
        PaymentStatus.RETURNED,
    },
}


class PaymentError(RuntimeError):
    """Base error with a stable code suitable for an integration response."""

    code = "PAYMENT_ERROR"


class ValidationError(PaymentError, ValueError):
    code = "VALIDATION_FAILED"


class TransitionError(PaymentError, ValueError):
    code = "ILLEGAL_TRANSITION"


class DuplicatePaymentError(PaymentError):
    code = "DUPLICATE_OPERATION_CONFLICT"


class ChecksumError(PaymentError):
    code = "CHECKSUM_MISMATCH"


class CallbackConflictError(PaymentError):
    """An event id was reused with a payload different from its durable inbox row."""

    code = "CALLBACK_PAYLOAD_CONFLICT"


@dataclass(frozen=True, slots=True)
class Party:
    name: str
    inn: str
    kpp: str
    account: str
    bank_bik: str = ""
    bank_name: str = ""


@dataclass(slots=True)
class Payment:
    document_ref: str
    request_ref: str
    number: str
    date: date
    payer: Party
    recipient: Party
    amount: Decimal
    currency: str
    payment_purpose: str
    status: str = PaymentStatus.PREPARED
    external_id: str | None = None

    def operation_id(self) -> str:
        # The business identifier is independent from mutable amount/purpose fields.
        return f"upp-payment:{self.document_ref}"

    def business_fingerprint(self) -> str:
        payload = {
            "operation_id": self.operation_id(),
            "request_ref": self.request_ref,
            "number": self.number,
            "date": self.date.isoformat(),
            "payer_account": self.payer.account,
            "recipient_account": self.recipient.account,
            "amount": format(self.amount, "f"),
            "currency": self.currency.upper(),
            "purpose": " ".join(self.payment_purpose.split()),
        }
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return sha256(raw.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditEvent:
    old_status: str
    new_status: str
    source: str
    message: str = ""
    external_id: str | None = None
    event_id: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


@dataclass(slots=True)
class IntegrationRecord:
    operation_id: str
    payment_ref: str
    request_ref: str
    status: str = PaymentStatus.PREPARED
    payment_fingerprint: str = ""
    external_id: str | None = None
    attempts: int = 0
    last_error: str = ""
    last_message: str = ""
    callback_ids: set[str] = field(default_factory=set)
    audit: list[AuditEvent] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class BankStatementRow:
    row_id: str
    booking_date: date
    account: str
    amount: Decimal
    currency: str
    external_bank_id: str = ""
    operation_id: str = ""
    document_ref: str = ""
    purpose: str = ""
    source: str = "bank_statement"
    statement_reference: str = ""

    def stable_identity(self) -> str:
        """Prefer the bank transaction id; otherwise use stable business fields.

        A physical row number can change when a statement is regenerated, so it
        is intentionally not part of the fallback identity.
        """

        source = self.source.strip() or "bank_statement"
        if self.external_bank_id.strip():
            return f"{source}:external:{self.external_bank_id.strip()}"
        stable_fields = {
            "source": source,
            "statement_reference": self.statement_reference.strip(),
            "account": self.account.strip(),
            "amount": format(self.amount, "f"),
            "currency": self.currency.upper(),
            "booking_date": self.booking_date.isoformat(),
            "purpose": " ".join(self.purpose.split()),
            "operation_id": self.operation_id.strip(),
            "document_ref": self.document_ref.strip(),
        }
        encoded = json.dumps(stable_fields, ensure_ascii=False, sort_keys=True)
        return f"{source}:fingerprint:{sha256(encoded.encode()).hexdigest()}"

    def payload_hash(self) -> str:
        """Hash all supplied fields, including a transport row id when present."""

        payload = {
            "row_id": self.row_id,
            "booking_date": self.booking_date.isoformat(),
            "account": self.account,
            "amount": format(self.amount, "f"),
            "currency": self.currency.upper(),
            "external_bank_id": self.external_bank_id,
            "operation_id": self.operation_id,
            "document_ref": self.document_ref,
            "purpose": " ".join(self.purpose.split()),
            "source": self.source,
            "statement_reference": self.statement_reference,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return sha256(encoded.encode()).hexdigest()

    def fingerprint(self) -> str:
        """Compatibility alias for callers that previously persisted a fingerprint."""

        return self.payload_hash()


def validate(payment: Payment) -> None:
    errors: list[str] = []
    for label, party in (("payer", payment.payer), ("recipient", payment.recipient)):
        for attribute in ("name", "inn", "kpp", "account"):
            if not getattr(party, attribute).strip():
                errors.append(f"{label}.{attribute}:required")
    if not payment.recipient.bank_bik.strip():
        errors.append("recipient.bank_bik:required")
    if payment.amount <= 0:
        errors.append("amount:must_be_positive")
    if len(payment.currency.strip()) != 3:
        errors.append("currency:expected_iso_4217")
    if not payment.payment_purpose.strip():
        errors.append("payment_purpose:required")
    if errors:
        raise ValidationError(";".join(errors))
