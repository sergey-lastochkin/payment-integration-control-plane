from __future__ import annotations

import json
from hashlib import sha256

from .adapters import BankAdapter, StatusLookup
from .domain import (
    CallbackConflictError,
    IntegrationRecord,
    Payment,
    PaymentStatus,
    TransitionError,
    validate,
)
from .repository import IntegrationRepository, transition_record


class PaymentService:
    def __init__(self, registry: IntegrationRepository, adapter: BankAdapter) -> None:
        self.registry = registry
        self.adapter = adapter
        recover = getattr(registry, "recover_sending", None)
        self.recovered_operations = recover() if recover else []

    @property
    def recovery_contract(self) -> str:
        """Describe the safe next step for claims whose external result is unknown."""

        capabilities = getattr(self.adapter, "capabilities", None)
        if capabilities is None:
            return "manual_or_reconciliation"
        if capabilities.status_lookup == "supported":
            return "status_lookup"
        if capabilities.send_semantics == "idempotent":
            return "idempotent_retry_requires_explicit_operator_policy"
        return "manual_or_reconciliation"

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
        fault = getattr(self.registry, "_fault", None)
        if fault:
            # A controlled crash here models response loss after a remote side
            # effect. The durable claim remains SENDING until a fresh service
            # instance classifies it as OUTCOME_UNKNOWN.
            fault("send_response_before_persist")
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
        canonical = json.dumps(
            {
                "operation_id": operation_id,
                "external_id": external_id or "",
                "status": status,
                "message": message,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        stable_event_id = event_id or f"callback:{sha256(canonical.encode()).hexdigest()}"
        payload_hash = sha256(canonical.encode()).hexdigest()
        durable_apply = getattr(self.registry, "apply_callback", None)
        if durable_apply:
            updated, outcome = durable_apply(
                operation_id=operation_id,
                status=status,
                external_id=external_id,
                message=message,
                event_id=stable_event_id,
                payload_hash=payload_hash,
            )
            if outcome == "conflict":
                raise CallbackConflictError(
                    f"callback event {stable_event_id} has a different payload"
                )
            return updated
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

    def reconcile_unknown(self, operation_id: str) -> IntegrationRecord:
        """Resolve an uncertain external send only through a declared lookup."""

        record = self.registry.get(operation_id)
        if record.status != PaymentStatus.OUTCOME_UNKNOWN:
            return record
        capabilities = getattr(self.adapter, "capabilities", None)
        lookup = getattr(self.adapter, "lookup", None)
        if (
            not capabilities
            or capabilities.status_lookup != StatusLookup.SUPPORTED
            or not lookup
        ):
            raise RuntimeError("status lookup is unavailable for this adapter")
        outcome = lookup(operation_id)
        status = str(outcome.get("status", ""))
        external_id = outcome.get("external_id")
        if status not in {
            PaymentStatus.ACCEPTED,
            PaymentStatus.EXECUTED,
            PaymentStatus.REJECTED,
            PaymentStatus.RETURNED,
        }:
            raise RuntimeError(f"bank lookup returned unresolved status: {status}")
        return self._transition(
            operation_id,
            status,
            "bank_status_lookup",
            "resolved after unknown external outcome",
            str(external_id) if external_id else None,
        )

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
