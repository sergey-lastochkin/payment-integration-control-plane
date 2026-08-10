from __future__ import annotations

from .adapters import BankAdapter
from .domain import IntegrationRecord, Payment, PaymentStatus, TransitionError, validate
from .repository import IntegrationRepository, transition_record


class PaymentService:
    def __init__(self, registry: IntegrationRepository, adapter: BankAdapter) -> None:
        self.registry = registry
        self.adapter = adapter

    def _transition(
        self,
        operation_id: str,
        new_status: str,
        source: str,
        message: str = "",
        external_id: str | None = None,
        event_id: str | None = None,
    ) -> IntegrationRecord:
        transition = getattr(self.registry, "transition", None)
        if transition:
            return transition(
                operation_id, new_status, source, message, external_id, event_id
            )
        record = transition_record(
            self.registry.get(operation_id),
            new_status,
            source,
            message,
            external_id,
            event_id,
        )
        self.registry.save(record)
        return record

    def prepare(self, payment: Payment) -> IntegrationRecord:
        validate(payment)
        record = self.registry.register(payment)
        if record.status == PaymentStatus.PREPARED:
            try:
                record = self._transition(
                    record.operation_id,
                    PaymentStatus.READY_TO_SEND,
                    "1c",
                    "validated",
                )
            except TransitionError:
                # Another worker may have claimed the operation after this
                # worker observed ``prepared``.  Re-read instead of regressing
                # a newer durable state back to ``ready_to_send``.
                record = self.registry.get(record.operation_id)
                if record.status == PaymentStatus.PREPARED:
                    raise
        return record

    def send(self, payment: Payment) -> IntegrationRecord:
        record = self.prepare(payment)
        if record.status in {
            PaymentStatus.SENT,
            PaymentStatus.ACCEPTED,
            PaymentStatus.EXECUTED,
        }:
            return record
        claimed = self.registry.claim_send(record.operation_id)
        if claimed is None:
            # Another worker has either claimed the side effect or completed it.
            return self.registry.get(record.operation_id)
        try:
            external_id = self.adapter.send(payment)
        except Exception as exc:
            # The remote side effect is now unknown.  Keep the claim rather than
            # reopening it and risking a duplicate external payment.
            current = self.registry.get(claimed.operation_id)
            current.last_error = f"{type(exc).__name__}:{exc}"
            current.last_message = "send outcome unknown; reconcile before retry"
            self.registry.save(current)
            raise
        current = self.registry.get(claimed.operation_id)
        if current.status != PaymentStatus.SENDING:
            # A status callback can win the race with the adapter response.
            # Do not overwrite accepted/rejected evidence with a weaker `sent`.
            return current
        return self._transition(
            claimed.operation_id,
            PaymentStatus.SENT,
            "bank_adapter",
            "submitted",
            external_id,
        )

    def callback(
        self,
        operation_id: str,
        status: str,
        external_id: str | None = None,
        message: str = "",
        event_id: str | None = None,
    ) -> IntegrationRecord:
        record = self.registry.get(operation_id)
        stable_event_id = event_id or f"{external_id or ''}:{status}:{message}"
        if stable_event_id in record.callback_ids:
            return record
        if status == record.status:
            record.callback_ids.add(stable_event_id)
            self.registry.save(record)
            return record
        record = self._transition(
            operation_id, status, "callback", message, external_id, stable_event_id
        )
        record.callback_ids.add(stable_event_id)
        self.registry.save(record)
        return record

    def resolve_manual(
        self, operation_id: str, status: str, decision_note: str, decided_by: str
    ) -> IntegrationRecord:
        record = self.registry.get(operation_id)
        if record.status != PaymentStatus.MANUAL_CHECK:
            raise TransitionError("manual resolution requires manual_check")
        if not decision_note.strip() or not decided_by.strip():
            raise ValueError("manual decision requires note and actor reference")
        return self._transition(
            operation_id,
            status,
            "manual_resolution",
            f"{decided_by}: {decision_note}",
        )
