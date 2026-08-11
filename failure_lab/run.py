"""Exercise local payment failure scenarios without contacting 1C, n8n or a bank."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import platform
import sqlite3
import statistics
import subprocess
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from payment_orchestration.adapters import (
    FileClientBankAdapter,
    MockBankAdapter,
    verify_envelope,
)
from payment_orchestration.domain import (
    BankStatementRow,
    ChecksumError,
    Party,
    Payment,
    TransitionError,
)
from payment_orchestration.reconciliation import Reconciler
from payment_orchestration.repository import Registry, SQLiteIntegrationRepository
from payment_orchestration.service import PaymentService


def payment(reference: str, amount: str = "100.00") -> Payment:
    return Payment(
        document_ref=reference,
        request_ref=f"request-{reference}",
        number="T-001",
        date=date(2026, 8, 10),
        payer=Party("Test payer", "TEST-INN-01", "TEST-KPP-01", "TEST-PAYER"),
        recipient=Party(
            "Test supplier",
            "TEST-INN-02",
            "TEST-KPP-02",
            "TEST-RECIPIENT",
            "TEST-BIK-01",
            "Test bank",
        ),
        amount=Decimal(amount),
        currency="RUB",
        payment_purpose="Test payment. No client data.",
    )


def scenario_duplicate_operation(_: Path) -> dict[str, object]:
    service = PaymentService(Registry(), MockBankAdapter())
    first = service.prepare(payment("case-duplicate-operation"))
    second = service.prepare(payment("case-duplicate-operation"))
    assert first.operation_id == second.operation_id
    return {"operation_id": first.operation_id, "attempts": second.attempts}


def scenario_duplicate_http(workdir: Path) -> dict[str, object]:
    item = payment("case-duplicate-http")
    store = Registry()
    service = PaymentService(store, FileClientBankAdapter(workdir / "outbox"))
    first = service.send(item)
    second = service.send(item)
    files = sorted((workdir / "outbox").glob("*.json"))
    assert first.status == second.status == "sent" and len(files) == 1
    return {"attempts": second.attempts, "outbox_files": len(files)}


def scenario_status_before_lost_response(_: Path) -> dict[str, object]:
    repository = Registry()

    class CallbackThenTimeout:
        service: PaymentService

        def send(self, item: Payment) -> str:
            self.service.callback(
                item.operation_id(),
                "accepted",
                external_id="test-bank-42",
                event_id="status-before-response",
            )
            raise TimeoutError("test response was lost after acceptance")

    adapter = CallbackThenTimeout()
    service = PaymentService(repository, adapter)
    adapter.service = service
    item = payment("case-status-before-response")
    try:
        service.send(item)
    except TimeoutError:
        pass
    record = repository.get(item.operation_id())
    assert record.status == "accepted" and record.attempts == 1
    return {"status": record.status, "attempts": record.attempts}


def scenario_repeated_bank_status(_: Path) -> dict[str, object]:
    item = payment("case-repeated-status")
    repository = Registry()
    service = PaymentService(repository, MockBankAdapter())
    service.send(item)
    service.callback(item.operation_id(), "accepted", "test-bank-43", event_id="bank-43")
    repeated = service.callback(
        item.operation_id(), "accepted", "test-bank-43", event_id="bank-43"
    )
    accepted = [event for event in repeated.audit if event.new_status == "accepted"]
    assert len(accepted) == 1
    return {"accepted_events": len(accepted), "status": repeated.status}


def scenario_repeated_statement(_: Path) -> dict[str, object]:
    item = payment("case-repeated-statement")
    row = BankStatementRow(
        "test-row-1",
        item.date,
        item.recipient.account,
        item.amount,
        "RUB",
        operation_id=item.operation_id(),
    )
    repository = SQLiteIntegrationRepository(":memory:")
    matcher = Reconciler(repository)
    first = matcher.match([row], [item])[0]
    repeated = matcher.match([row], [item])[0]
    assert first["status"] == "matched" and repeated["replayed"] is True
    return {"first_status": first["status"], "replayed": repeated["replayed"]}


def scenario_ambiguous_statement(_: Path) -> dict[str, object]:
    first, second = payment("case-ambiguous-a"), payment("case-ambiguous-b")
    result = Reconciler(SQLiteIntegrationRepository(":memory:")).match(
        [{"account": "TEST-RECIPIENT", "amount": "100.00", "date": "2026-08-10"}],
        [first, second],
    )[0]
    assert result["status"] == "manual_check" and result["candidate_count"] == 2
    return {"status": result["status"], "candidate_count": result["candidate_count"]}


def scenario_illegal_transition(_: Path) -> dict[str, object]:
    item = payment("case-illegal-transition")
    service = PaymentService(Registry(), MockBankAdapter())
    service.send(item)
    try:
        service.callback(item.operation_id(), "executed", event_id="too-early")
    except TransitionError:
        return {"blocked": True, "from_status": "sent", "to_status": "executed"}
    raise AssertionError("illegal transition was accepted")


def scenario_restart(workdir: Path) -> dict[str, object]:
    path = workdir / "restart.sqlite"
    item = payment("case-restart")
    first = SQLiteIntegrationRepository(str(path))
    PaymentService(first, MockBankAdapter()).send(item)
    first.connection.close()
    reopened = SQLiteIntegrationRepository(str(path))
    record = reopened.get(item.operation_id())
    reopened.connection.close()
    assert record.status == "sent" and record.attempts == 1
    return {"status_after_restart": record.status, "attempts": record.attempts}


def scenario_storage_unavailable(workdir: Path) -> dict[str, object]:
    obstacle = workdir / "not-a-directory"
    obstacle.write_text("not a sqlite directory", encoding="utf-8")
    try:
        SQLiteIntegrationRepository(str(obstacle / "operations.sqlite"))
    except (NotADirectoryError, sqlite3.OperationalError):
        return {"blocked": True}
    raise AssertionError("storage unexpectedly opened")


def scenario_route_unavailable(_: Path) -> dict[str, object]:
    item = payment("case-route-unavailable")
    repository = Registry()
    service = PaymentService(repository, MockBankAdapter(ConnectionError("route unavailable")))
    try:
        service.send(item)
    except ConnectionError:
        pass
    record = repository.get(item.operation_id())
    assert record.status == "sending" and record.attempts == 1
    return {"status": record.status, "retry_action": "reconcile_before_retry"}


def scenario_auth_error(_: Path) -> dict[str, object]:
    item = payment("case-auth-error")
    repository = Registry()
    service = PaymentService(repository, MockBankAdapter(PermissionError("auth rejected")))
    try:
        service.send(item)
    except PermissionError:
        pass
    record = repository.get(item.operation_id())
    assert record.status == "sending"
    return {"status": record.status, "retry_action": "reconcile_before_retry"}


def scenario_corrupted_exchange(workdir: Path) -> dict[str, object]:
    adapter = FileClientBankAdapter(workdir / "checksum")
    adapter.send(payment("case-corrupted-exchange"))
    path = next((workdir / "checksum").glob("*.json"))
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["payload"]["amount"] = "999.00"
    try:
        verify_envelope(envelope)
    except ChecksumError:
        return {"blocked": True, "check": "checksum_mismatch"}
    raise AssertionError("corrupted envelope was accepted")


def scenario_manual_resolution(_: Path) -> dict[str, object]:
    item = payment("case-manual-resolution")
    service = PaymentService(Registry(), MockBankAdapter())
    service.send(item)
    service.callback(item.operation_id(), "manual_check", event_id="disputed-statement")
    record = service.resolve_manual(
        item.operation_id(),
        "executed",
        "test statement and bank status agree",
        "test-operator",
    )
    assert record.status == "executed"
    return {"status": record.status, "audit_source": record.audit[-1].source}


def scenario_durable_statement_restart(workdir: Path) -> dict[str, object]:
    path = workdir / "statements.sqlite"
    item = payment("case-durable-statement")
    row = BankStatementRow(
        "transport-row-1",
        item.date,
        item.recipient.account,
        item.amount,
        "RUB",
        external_bank_id="test-transaction-1",
        operation_id=item.operation_id(),
        source="test_bank_file",
        statement_reference="test-statement-1",
    )
    first = SQLiteIntegrationRepository(str(path))
    assert Reconciler(first).match([row], [item])[0]["replayed"] is False
    first.close()
    reopened = SQLiteIntegrationRepository(str(path))
    repeated = Reconciler(reopened).match([row], [item])[0]
    assert repeated["replayed"] is True
    return {"replayed_after_reopen": repeated["replayed"]}


def scenario_durable_callback_restart(workdir: Path) -> dict[str, object]:
    path = workdir / "callbacks.sqlite"
    item = payment("case-durable-callback")
    first = SQLiteIntegrationRepository(str(path))
    service = PaymentService(first, MockBankAdapter())
    service.send(item)
    service.callback(item.operation_id(), "accepted", event_id="callback-once")
    first.close()
    reopened = SQLiteIntegrationRepository(str(path))
    repeated = PaymentService(reopened, MockBankAdapter()).callback(
        item.operation_id(), "accepted", event_id="callback-once"
    )
    accepted = [event for event in repeated.audit if event.new_status == "accepted"]
    assert len(accepted) == 1
    return {"accepted_events": len(accepted), "replayed_after_reopen": True}


def scenario_unknown_outcome_recovery(workdir: Path) -> dict[str, object]:
    path = workdir / "unknown.sqlite"
    item = payment("case-unknown-outcome")
    faulting = SQLiteIntegrationRepository(str(path), fault_at="send_response_before_persist")
    try:
        PaymentService(faulting, MockBankAdapter()).send(item)
    except RuntimeError:
        pass
    assert faulting.get(item.operation_id()).status == "sending"
    faulting.close()
    reopened = SQLiteIntegrationRepository(str(path))
    service = PaymentService(reopened, MockBankAdapter())
    record = service.send(item)
    assert record.status == "outcome_unknown"
    return {"status": record.status, "blind_resend": False, "recovered": len(service.recovered_operations)}


def _claim_worker(path: str, outbox: str, item: Payment, ready, start, result) -> None:
    repository = SQLiteIntegrationRepository(path)
    service = PaymentService(repository, FileClientBankAdapter(Path(outbox)))
    ready.put(True)
    start.wait(10)
    record = service.send(item)
    result.put(record.status)
    repository.close()


def scenario_multiprocess_claim(workdir: Path) -> dict[str, object]:
    context = multiprocessing.get_context("spawn")
    path, outbox = str(workdir / "claims.sqlite"), str(workdir / "outbox")
    item = payment("case-multiprocess-claim")
    ready, result, start = context.Queue(), context.Queue(), context.Event()
    workers = [
        context.Process(target=_claim_worker, args=(path, outbox, item, ready, start, result))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    ready.get(timeout=10)
    ready.get(timeout=10)
    start.set()
    for worker in workers:
        worker.join(15)
        assert worker.exitcode == 0
    final = SQLiteIntegrationRepository(path).get(item.operation_id())
    assert final.attempts == 1 and len(list(Path(outbox).glob("*.json"))) == 1
    return {
        "workers_started": 2,
        "claims_successful": final.attempts,
        "send_attempts": final.attempts,
        "worker_states": sorted([result.get(timeout=5), result.get(timeout=5)]),
    }


def scenario_reconciliation_ledger_smoke(workdir: Path) -> dict[str, object]:
    repository = SQLiteIntegrationRepository(str(workdir / "ledger.sqlite"))
    matcher = Reconciler(repository)
    latencies: list[float] = []
    for index in range(10_000):
        started = time.perf_counter()
        matcher.match(
            [
                {
                    "source": "performance_smoke",
                    "statement_reference": "synthetic",
                    "external_bank_id": f"synthetic-{index}",
                    "amount": "1.00",
                    "currency": "RUB",
                    "date": "2026-08-11",
                }
            ],
            [],
        )
        latencies.append((time.perf_counter() - started) * 1000)
    ordered = sorted(latencies)
    return {
        "inserts": len(latencies),
        "p50_ms": round(statistics.median(latencies), 4),
        "p95_ms": round(ordered[round((len(ordered) - 1) * 0.95)], 4),
    }


SCENARIOS = {
    "duplicate_operation": scenario_duplicate_operation,
    "duplicate_http_request": scenario_duplicate_http,
    "status_before_lost_response": scenario_status_before_lost_response,
    "repeated_bank_status": scenario_repeated_bank_status,
    "repeated_statement": scenario_repeated_statement,
    "ambiguous_statement": scenario_ambiguous_statement,
    "illegal_transition": scenario_illegal_transition,
    "worker_restart": scenario_restart,
    "storage_unavailable": scenario_storage_unavailable,
    "route_unavailable": scenario_route_unavailable,
    "auth_error": scenario_auth_error,
    "corrupted_exchange": scenario_corrupted_exchange,
    "manual_resolution": scenario_manual_resolution,
    "durable_statement_restart": scenario_durable_statement_restart,
    "durable_callback_restart": scenario_durable_callback_restart,
    "unknown_outcome_recovery": scenario_unknown_outcome_recovery,
    "multiprocess_claim": scenario_multiprocess_claim,
    "reconciliation_ledger_smoke": scenario_reconciliation_ledger_smoke,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=Path, default=Path("failure_lab/runs"))
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    run_dir = args.runs / args.run_id
    if run_dir.exists():
        raise FileExistsError(f"run already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    records = []
    for name, scenario in SCENARIOS.items():
        with TemporaryDirectory(prefix=f"{name}-", dir=run_dir) as temporary:
            evidence = scenario(Path(temporary))
        record = {"scenario": name, "status": "passed", "evidence": evidence}
        (run_dir / f"{name}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        records.append(record)
    summary = {
        "run_id": args.run_id,
        "executed_at": datetime.now(UTC).isoformat(),
        "code_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "scope": "local deterministic failure lab; no 1C, n8n or bank connection",
        "scenarios": records,
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"run_id": args.run_id, "passed": len(records)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
