from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from .domain import ChecksumError, Payment


class SendSemantics(StrEnum):
    """What this adapter can prove about reusing an external send request."""

    IDEMPOTENT = "idempotent"
    UNKNOWN = "unknown"


class StatusLookup(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    send_semantics: SendSemantics
    status_lookup: StatusLookup


class BankAdapter(Protocol):
    capabilities: AdapterCapabilities

    def send(self, payment: Payment) -> str: ...


def payment_envelope(payment: Payment) -> dict[str, object]:
    payload = {
        "operation_id": payment.operation_id(),
        "request_ref": payment.request_ref,
        "number": payment.number,
        "date": payment.date.isoformat(),
        "amount": format(payment.amount, "f"),
        "currency": payment.currency.upper(),
        "recipient_account": payment.recipient.account,
        "recipient_bik": payment.recipient.bank_bik,
        "purpose": payment.payment_purpose,
    }
    raw = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    return {
        "schema": "payment.v1",
        "payload": payload,
        "checksum": sha256(raw).hexdigest(),
    }


def verify_envelope(envelope: dict[str, object]) -> None:
    payload = envelope.get("payload")
    raw = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    if sha256(raw).hexdigest() != envelope.get("checksum"):
        raise ChecksumError("bank exchange envelope was modified")


class MockBankAdapter:
    capabilities = AdapterCapabilities(
        SendSemantics.IDEMPOTENT, StatusLookup.UNSUPPORTED
    )

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.sent: dict[str, str] = {}
        self.fail_with = fail_with

    def send(self, payment: Payment) -> str:
        if self.fail_with:
            raise self.fail_with
        operation_id = payment.operation_id()
        self.sent.setdefault(
            operation_id, f"mock-{sha256(operation_id.encode()).hexdigest()[:12]}"
        )
        return self.sent[operation_id]


class FileClientBankAdapter:
    """Deterministic local file adapter; it never calls a real bank."""

    capabilities = AdapterCapabilities(
        SendSemantics.IDEMPOTENT, StatusLookup.UNSUPPORTED
    )

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def send(self, payment: Payment) -> str:
        operation_id = payment.operation_id()
        name = sha256(operation_id.encode()).hexdigest()[:16] + ".json"
        path = self.directory / name
        envelope = payment_envelope(payment)
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            verify_envelope(existing)
            if existing != envelope:
                raise RuntimeError("deterministic file collision")
        else:
            path.write_text(
                json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return f"file:{name}:{str(envelope['checksum'])[:12]}"
