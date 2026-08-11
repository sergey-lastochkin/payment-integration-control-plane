from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from hashlib import sha256
from typing import Any, Protocol

from .domain import BankStatementRow, Payment


class ReconciliationLedger(Protocol):
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
    ) -> tuple[str, dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class ReconciliationPolicy:
    """Initial heuristic policy; it is not calibrated on production history."""

    external_id_weight: int = 100
    operation_id_weight: int = 95
    document_ref_weight: int = 70
    account_weight: int = 25
    amount_weight: int = 25
    date_weight: int = 10
    currency_weight: int = 5
    purpose_similarity_weight: int = 10
    minimum_score: int = 60
    purpose_similarity_threshold: float = 0.75


class Reconciler:
    """Durable statement matcher with explicit ambiguity and conflict outcomes."""

    def __init__(self, ledger: ReconciliationLedger, policy: ReconciliationPolicy | None = None) -> None:
        self.ledger = ledger
        self.policy = policy or ReconciliationPolicy()

    @staticmethod
    def _row(value: BankStatementRow | dict[str, object]) -> dict[str, object]:
        if isinstance(value, BankStatementRow):
            return {
                "row_id": value.row_id,
                "source": value.source,
                "statement_reference": value.statement_reference,
                "external_bank_id": value.external_bank_id,
                "operation_id": value.operation_id,
                "document_ref": value.document_ref,
                "account": value.account,
                "amount": format(value.amount, "f"),
                "date": value.booking_date.isoformat(),
                "currency": value.currency.upper(),
                "purpose": value.purpose,
                "row_identity": value.stable_identity(),
                "payload_hash": value.payload_hash(),
            }
        row = dict(value)
        row["source"] = str(row.get("source") or "bank_statement")
        row["statement_reference"] = str(row.get("statement_reference") or "")
        row["currency"] = str(row.get("currency") or "").upper()
        stable = {
            "source": row["source"],
            "statement_reference": row["statement_reference"],
            "account": str(row.get("account") or ""),
            "amount": str(row.get("amount") or ""),
            "currency": row["currency"],
            "date": str(row.get("date") or ""),
            "purpose": " ".join(str(row.get("purpose") or "").split()),
            "operation_id": str(row.get("operation_id") or ""),
            "document_ref": str(row.get("document_ref") or ""),
        }
        external_id = str(row.get("external_bank_id") or "").strip()
        row["row_identity"] = (
            f"{row['source']}:external:{external_id}"
            if external_id
            else f"{row['source']}:fingerprint:{sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}"
        )
        row["payload_hash"] = sha256(
            json.dumps(row, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest()
        return row

    def _score(self, row: dict[str, object], payment: Payment) -> tuple[int, list[str]]:
        policy = self.policy
        score = 0
        reasons: list[str] = []
        external_id = str(row.get("external_bank_id") or "")
        if external_id and external_id == payment.external_id:
            score += policy.external_id_weight
            reasons.append("external_bank_id")
        if row.get("operation_id") == payment.operation_id():
            score += policy.operation_id_weight
            reasons.append("operation_id")
        if row.get("document_ref") == payment.document_ref:
            score += policy.document_ref_weight
            reasons.append("document_ref")
        if row.get("account") == payment.recipient.account:
            score += policy.account_weight
            reasons.append("recipient_account")
        try:
            if Decimal(str(row.get("amount"))) == payment.amount:
                score += policy.amount_weight
                reasons.append("amount")
        except InvalidOperation:
            reasons.append("invalid_amount")
        if row.get("date") == payment.date.isoformat():
            score += policy.date_weight
            reasons.append("booking_date")
        if str(row.get("currency") or "").upper() == payment.currency.upper():
            score += policy.currency_weight
            reasons.append("currency")
        purpose = str(row.get("purpose") or "")
        if purpose and SequenceMatcher(None, purpose.lower(), payment.payment_purpose.lower()).ratio() >= policy.purpose_similarity_threshold:
            score += policy.purpose_similarity_weight
            reasons.append("purpose_similarity")
        return score, reasons

    def _calculate(
        self, row: dict[str, object], payments: list[Payment]
    ) -> tuple[dict[str, Any], list[str]]:
        ranked = sorted(
            ((self._score(row, payment), payment) for payment in payments),
            key=lambda item: (item[0][0], item[1].operation_id()),
            reverse=True,
        )
        best_score = ranked[0][0][0] if ranked else 0
        candidates = [
            item
            for item in ranked
            if item[0][0] == best_score and best_score >= self.policy.minimum_score
        ]
        matched = candidates[0][1] if len(candidates) == 1 else None
        return (
            {
                "status": "matched" if matched else "manual_check",
                "reason": "+".join(candidates[0][0][1]) if candidates else "no_confident_candidate",
                "candidate_count": len(candidates),
                "score": best_score,
                "matched_operation_id": matched.operation_id() if matched else None,
            },
            [payment.operation_id() for _, payment in candidates],
        )

    @staticmethod
    def _public_result(
        row: dict[str, object], stored: dict[str, Any], payments: list[Payment], replayed: bool
    ) -> dict[str, object]:
        operation_id = stored.get("matched_operation_id")
        payment = next(
            (item for item in payments if item.operation_id() == operation_id), None
        )
        return {
            "row": row,
            "payment": payment,
            "status": stored["status"],
            "reason": stored["reason"],
            "candidate_count": stored["candidate_count"],
            "score": stored["score"],
            "replayed": replayed,
        }

    def match(
        self, rows: list[BankStatementRow | dict[str, object]], payments: list[Payment]
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for input_row in rows:
            row = self._row(input_row)
            calculated, candidate_operation_ids = self._calculate(row, payments)
            outcome, stored = self.ledger.persist_reconciliation(
                row_identity=str(row["row_identity"]),
                source=str(row["source"]),
                statement_reference=str(row["statement_reference"]),
                payload_hash=str(row["payload_hash"]),
                result=calculated,
                matched_operation_id=calculated["matched_operation_id"],
                resolution="automatic" if calculated["status"] == "matched" else "manual_check",
                metadata={
                    "policy": asdict(self.policy),
                    "candidate_operation_ids": candidate_operation_ids,
                },
            )
            results.append(self._public_result(row, stored, payments, outcome == "replay"))
        return results
