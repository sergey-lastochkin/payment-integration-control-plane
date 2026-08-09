from __future__ import annotations

from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

from .domain import BankStatementRow, Payment


class Reconciler:
    """Composite deterministic matcher with replay protection and explicit ambiguity."""

    def __init__(self) -> None:
        self.processed_rows: dict[str, dict[str, object]] = {}

    @staticmethod
    def _row(value: BankStatementRow | dict[str, object]) -> dict[str, object]:
        if isinstance(value, BankStatementRow):
            return {
                "row_id": value.row_id,
                "external_bank_id": value.external_bank_id,
                "operation_id": value.operation_id,
                "document_ref": value.document_ref,
                "account": value.account,
                "amount": str(value.amount),
                "date": value.booking_date.isoformat(),
                "currency": value.currency,
                "purpose": value.purpose,
                "fingerprint": value.fingerprint(),
            }
        row = dict(value)
        row.setdefault("fingerprint", "")
        return row

    @staticmethod
    def _score(row: dict[str, object], payment: Payment) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []
        if (
            row.get("external_bank_id")
            and row["external_bank_id"] == payment.external_id
        ):
            score += 100
            reasons.append("external_bank_id")
        if row.get("operation_id") == payment.operation_id():
            score += 95
            reasons.append("operation_id")
        if row.get("document_ref") == payment.document_ref:
            score += 70
            reasons.append("document_ref")
        if row.get("account") == payment.recipient.account:
            score += 25
            reasons.append("recipient_account")
        try:
            if Decimal(str(row.get("amount"))) == payment.amount:
                score += 25
                reasons.append("amount")
        except InvalidOperation:
            reasons.append("invalid_amount")
        if row.get("date") == payment.date.isoformat():
            score += 10
            reasons.append("booking_date")
        if (
            str(row.get("currency", payment.currency)).upper()
            == payment.currency.upper()
        ):
            score += 5
            reasons.append("currency")
        purpose = str(row.get("purpose", ""))
        if (
            purpose
            and SequenceMatcher(
                None, purpose.lower(), payment.payment_purpose.lower()
            ).ratio()
            >= 0.75
        ):
            score += 10
            reasons.append("purpose_similarity")
        return score, reasons

    def match(
        self, rows: list[BankStatementRow | dict[str, object]], payments: list[Payment]
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for input_row in rows:
            row = self._row(input_row)
            fingerprint = str(row.get("fingerprint") or row.get("row_id") or "")
            if fingerprint and fingerprint in self.processed_rows:
                replayed = dict(self.processed_rows[fingerprint])
                replayed["replayed"] = True
                results.append(replayed)
                continue
            ranked = sorted(
                ((self._score(row, payment), payment) for payment in payments),
                key=lambda item: item[0][0],
                reverse=True,
            )
            best_score = ranked[0][0][0] if ranked else 0
            candidates = [
                item for item in ranked if item[0][0] == best_score and best_score >= 60
            ]
            matched = candidates[0][1] if len(candidates) == 1 else None
            result = {
                "row": row,
                "payment": matched,
                "status": "matched" if matched else "manual_check",
                "reason": "+".join(candidates[0][0][1])
                if candidates
                else "no_confident_candidate",
                "candidate_count": len(candidates),
                "score": best_score,
                "replayed": False,
            }
            if fingerprint:
                self.processed_rows[fingerprint] = result
            results.append(result)
        return results
