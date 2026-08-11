"""Local HTTP control-plane runtime used by the real n8n/bank evidence run.

The module has no 1C or production-bank connection.  It exposes real TCP HTTP
endpoints over the durable payment state machine and uses only synthetic data.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .adapters import HTTPBankAdapter
from .domain import CallbackConflictError, Party, Payment, PaymentError
from .repository import SQLiteIntegrationRepository
from .service import PaymentService


def _trace(path: Path, kind: str, **fields: object) -> None:
    from datetime import UTC, datetime

    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "component": "payment",
        "kind": kind,
        **fields,
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _payment(body: dict[str, Any]) -> Payment:
    document_ref = str(body.get("document_ref", ""))
    run_id = str(body.get("run_id", ""))
    if not document_ref.startswith("TEST-") or not run_id.startswith("real-n8n-bank-"):
        raise ValueError(
            "only synthetic TEST document_ref and real-n8n-bank run_id are accepted"
        )
    return Payment(
        document_ref=document_ref,
        request_ref=str(body.get("request_id", "")),
        number=str(body.get("number", "TEST-001")),
        date=date.fromisoformat(str(body.get("date", "2026-08-11"))),
        payer=Party("TEST PAYER", "TEST-PAYER", "TEST-KPP", "TEST-PAYER-ACCOUNT"),
        recipient=Party(
            "TEST RECIPIENT",
            "TEST-RECIPIENT",
            "TEST-KPP",
            "TEST-RECIPIENT-ACCOUNT",
            "TEST-BIK",
        ),
        amount=Decimal(str(body.get("amount", "1.00"))),
        currency="RUB",
        payment_purpose=f"Synthetic integration test {run_id}",
    )


def _record(record) -> dict[str, object]:
    return {
        "operation_id": record.operation_id,
        "status": record.status,
        "external_id": record.external_id,
        "attempts": record.attempts,
        "audit": [
            {
                "from": event.old_status,
                "to": event.new_status,
                "source": event.source,
                "event_id": event.event_id,
                "timestamp": event.timestamp,
            }
            for event in record.audit
        ],
    }


class ControlPlaneRuntime:
    def __init__(
        self, database: Path, bank_url: str, callback_url: str, trace: Path
    ) -> None:
        self.repository = SQLiteIntegrationRepository(str(database))
        self.trace = trace
        self.adapter = HTTPBankAdapter(bank_url, callback_url)
        self.service = PaymentService(self.repository, self.adapter)
        _trace(
            trace,
            "startup",
            recovered_operations=len(self.service.recovered_operations),
        )

    def close(self) -> None:
        self.repository.close()


def create_app(
    database: Path, bank_url: str, callback_url: str, trace: Path
) -> FastAPI:
    runtime = ControlPlaneRuntime(database, bank_url, callback_url, trace)
    app = FastAPI(title="Synthetic Payment Control Plane", version="1.0")

    @app.on_event("shutdown")
    def shutdown() -> None:
        runtime.close()

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"ok": True, "component": "payment-control-plane"}

    @app.post("/v1/payments")
    async def submit(request: Request) -> dict[str, object]:
        body = await request.json()
        try:
            payment = _payment(body)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        runtime.adapter.test_mode = str(body.get("test_mode", "normal"))
        runtime.adapter.desired_status = str(body.get("desired_status", "accepted"))
        correlation_id = str(body.get("correlation_id", payment.operation_id()))
        runtime.adapter.correlation_id = correlation_id
        _trace(
            runtime.trace,
            "submit_received",
            correlation_id=correlation_id,
            operation_id=payment.operation_id(),
            test_mode=runtime.adapter.test_mode,
        )
        try:
            # A bank can synchronously return a status callback through n8n
            # before completing this request.  The durable service is blocking
            # by design, so keep it off the ASGI event loop and let the
            # callback request reach the same runtime concurrently.
            record = await run_in_threadpool(runtime.service.send, payment)
        except Exception as error:
            current = runtime.repository.get(payment.operation_id())
            _trace(
                runtime.trace,
                "submit_unknown_outcome",
                correlation_id=correlation_id,
                operation_id=payment.operation_id(),
                exception=type(error).__name__,
                status=current.status,
            )
            raise HTTPException(
                status_code=502, detail="external outcome unknown"
            ) from error
        _trace(
            runtime.trace,
            "submit_completed",
            correlation_id=correlation_id,
            operation_id=record.operation_id,
            status=record.status,
            attempts=record.attempts,
        )
        return _record(record)

    @app.post("/v1/callbacks")
    async def callback(request: Request) -> dict[str, object]:
        body = await request.json()
        operation_id = str(body.get("operation_id", ""))
        correlation_id = str(body.get("correlation_id", operation_id))
        try:
            record = await run_in_threadpool(
                runtime.service.callback,
                operation_id,
                str(body.get("status", "")),
                str(body.get("external_id", "")) or None,
                str(body.get("message", "")),
                str(body.get("event_id", "")) or None,
            )
        except CallbackConflictError as error:
            record = runtime.repository.get(operation_id)
            _trace(
                runtime.trace,
                "callback_conflict",
                correlation_id=correlation_id,
                operation_id=operation_id,
                status=record.status,
            )
            raise HTTPException(status_code=409, detail=error.code) from error
        except (KeyError, PaymentError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        _trace(
            runtime.trace,
            "callback_applied",
            correlation_id=correlation_id,
            operation_id=record.operation_id,
            status=record.status,
            event_id=str(body.get("event_id", "")),
        )
        return _record(record)

    @app.get("/v1/payments/{operation_id}")
    def get_payment(operation_id: str) -> dict[str, object]:
        try:
            return _record(runtime.repository.get(operation_id))
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="operation not found"
            ) from error

    @app.post("/v1/payments/{operation_id}/recover")
    def recover(operation_id: str) -> dict[str, object]:
        try:
            record = runtime.service.reconcile_unknown(operation_id)
        except (KeyError, RuntimeError, PaymentError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        _trace(
            runtime.trace,
            "status_lookup_applied",
            operation_id=operation_id,
            status=record.status,
        )
        return _record(record)

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--bank-url", required=True)
    parser.add_argument("--callback-url", required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(
        create_app(args.database, args.bank_url, args.callback_url, args.trace),
        host=args.host,
        port=args.port,
        log_level=os.environ.get("PAYMENT_HTTP_LOG_LEVEL", "warning"),
    )


if __name__ == "__main__":
    main()
