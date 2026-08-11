"""Test-only HTTP bank emulator with durable side effects and real callbacks."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def trace(path: Path, kind: str, **fields: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "timestamp": timestamp(),
                    "component": "bank_emulator",
                    "kind": kind,
                    **fields,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )


class BankLedger:
    def __init__(self, database: Path) -> None:
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(database, check_same_thread=False, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bank_payments (
              operation_id TEXT PRIMARY KEY,
              external_id TEXT NOT NULL,
              status TEXT NOT NULL,
              payload_hash TEXT NOT NULL,
              committed_at TEXT NOT NULL,
              callback_url TEXT NOT NULL,
              correlation_id TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bank_callback_attempts (
              event_id TEXT NOT NULL,
              operation_id TEXT NOT NULL,
              status TEXT NOT NULL,
              attempted_at TEXT NOT NULL,
              http_status INTEGER,
              PRIMARY KEY(event_id, status)
            )
            """
        )
        self.connection.commit()

    @contextmanager
    def write(self):
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def commit_payment(
        self,
        operation_id: str,
        payload_hash: str,
        callback_url: str,
        correlation_id: str,
        status: str,
    ) -> dict[str, object]:
        external_id = f"bank-{sha256(operation_id.encode()).hexdigest()[:16]}"
        with self.write():
            existing = self.connection.execute(
                "SELECT * FROM bank_payments WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise ValueError(
                        "same operation_id has a different immutable payload"
                    )
                return dict(existing)
            self.connection.execute(
                "INSERT INTO bank_payments VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    operation_id,
                    external_id,
                    status,
                    payload_hash,
                    timestamp(),
                    callback_url,
                    correlation_id,
                ),
            )
            return dict(
                self.connection.execute(
                    "SELECT * FROM bank_payments WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
            )

    def get(self, operation_id: str) -> dict[str, object]:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM bank_payments WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        if not row:
            raise KeyError(operation_id)
        return dict(row)

    def count(self, operation_id: str) -> int:
        with self._lock:
            return int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM bank_payments WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()[0]
            )

    def set_status(self, operation_id: str, status: str) -> dict[str, object]:
        if status not in {"accepted", "executed", "rejected"}:
            raise ValueError("unsupported test bank status")
        with self.write():
            changed = self.connection.execute(
                "UPDATE bank_payments SET status = ? WHERE operation_id = ?",
                (status, operation_id),
            )
            if changed.rowcount != 1:
                raise KeyError(operation_id)
            return dict(
                self.connection.execute(
                    "SELECT * FROM bank_payments WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
            )

    def record_callback(
        self, event_id: str, operation_id: str, status: str, http_status: int | None
    ) -> None:
        with self.write():
            self.connection.execute(
                "INSERT OR REPLACE INTO bank_callback_attempts VALUES (?, ?, ?, ?, ?)",
                (event_id, operation_id, status, timestamp(), http_status),
            )


def _callback(
    row: dict[str, object], status: str, event_id: str, trace_path: Path
) -> int | None:
    import httpx

    payload = {
        "operation_id": row["operation_id"],
        "external_id": row["external_id"],
        "status": status,
        "event_id": event_id,
        "message": "synthetic local bank callback",
        "correlation_id": row["correlation_id"],
    }
    try:
        response = httpx.post(str(row["callback_url"]), json=payload, timeout=3)
        trace(
            trace_path,
            "callback_sent",
            operation_id=row["operation_id"],
            status=status,
            event_id=event_id,
            http_status=response.status_code,
        )
        return response.status_code
    except httpx.HTTPError as error:
        trace(
            trace_path,
            "callback_failed",
            operation_id=row["operation_id"],
            status=status,
            event_id=event_id,
            exception=type(error).__name__,
        )
        return None


def _delayed_callback(
    ledger: BankLedger,
    row: dict[str, object],
    status: str,
    event_id: str,
    trace_path: Path,
) -> None:
    http_status = _callback(row, status, event_id, trace_path)
    ledger.record_callback(event_id, str(row["operation_id"]), status, http_status)


def _truncated_response() -> Any:
    yield b'{"external_id":"'
    raise ConnectionResetError("test response lost after durable bank commit")


def create_app(database: Path, trace_path: Path) -> FastAPI:
    ledger = BankLedger(database)
    app = FastAPI(title="Synthetic HTTP Bank Emulator", version="1.0")

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"ok": True, "component": "bank-emulator"}

    @app.post("/payments")
    async def submit(request: Request, background: BackgroundTasks):
        body = await request.json()
        payment = body.get("payment", {})
        payload = payment.get("payload", {}) if isinstance(payment, dict) else {}
        operation_id = str(payload.get("operation_id", ""))
        if not operation_id.startswith("upp-payment:TEST-"):
            raise HTTPException(
                status_code=422, detail="only synthetic TEST operations are accepted"
            )
        mode = str(body.get("test_mode", "normal"))
        desired_status = str(body.get("desired_status", "accepted"))
        callback_url = str(body.get("callback_url", ""))
        correlation_id = str(body.get("correlation_id", operation_id))
        if not callback_url.startswith(
            ("http://127.0.0.1:5678/webhook/", "http://n8n:5678/webhook/")
        ):
            raise HTTPException(
                status_code=422, detail="callback must target local n8n webhook"
            )
        payload_hash = sha256(
            json.dumps(
                payment, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        try:
            row = ledger.commit_payment(
                operation_id, payload_hash, callback_url, correlation_id, desired_status
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        trace(
            trace_path,
            "payment_committed",
            operation_id=operation_id,
            mode=mode,
            status=row["status"],
        )

        event_id = f"bank-event:{operation_id}:accepted"
        if mode == "response_lost_after_commit":
            return StreamingResponse(
                _truncated_response(), media_type="application/json"
            )
        if mode == "delayed_response":
            await asyncio.sleep(0.35)
        if mode == "callback_before_response":
            _delayed_callback(ledger, row, str(row["status"]), event_id, trace_path)
        elif mode == "duplicate_callback":
            background.add_task(
                _delayed_callback, ledger, row, str(row["status"]), event_id, trace_path
            )
            background.add_task(
                _delayed_callback,
                ledger,
                row,
                str(row["status"]),
                event_id + ":duplicate",
                trace_path,
            )
        elif mode == "conflicting_callback":
            background.add_task(
                _delayed_callback, ledger, row, "accepted", event_id, trace_path
            )
            background.add_task(
                _delayed_callback, ledger, row, "rejected", event_id, trace_path
            )
        else:
            background.add_task(
                _delayed_callback, ledger, row, str(row["status"]), event_id, trace_path
            )
        return {
            "operation_id": row["operation_id"],
            "external_id": row["external_id"],
            "status": row["status"],
            "committed_at": row["committed_at"],
        }

    @app.get("/payments/{operation_id}")
    def lookup(operation_id: str) -> dict[str, object]:
        try:
            row = ledger.get(operation_id)
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="bank payment not found"
            ) from error
        return {
            "operation_id": row["operation_id"],
            "external_id": row["external_id"],
            "status": row["status"],
            "committed_at": row["committed_at"],
        }

    @app.get("/testing/payments/{operation_id}/count")
    def test_count(operation_id: str) -> dict[str, object]:
        return {"operation_id": operation_id, "count": ledger.count(operation_id)}

    @app.post("/testing/payments/{operation_id}/status")
    async def set_status(operation_id: str, request: Request):
        body = await request.json()
        try:
            row = ledger.set_status(operation_id, str(body.get("status", "")))
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        event_id = str(
            body.get("event_id", f"bank-event:{operation_id}:{row['status']}")
        )
        http_status = _callback(row, str(row["status"]), event_id, trace_path)
        ledger.record_callback(event_id, operation_id, str(row["status"]), http_status)
        return {
            "operation_id": operation_id,
            "status": row["status"],
            "callback_http_status": http_status,
        }

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(
        create_app(args.database, args.trace),
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
