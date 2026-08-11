"""Show durable recovery after an uncertain external send result.

This uses only SQLite and MockBankAdapter.  It never connects to 1C, n8n or a bank.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from payment_orchestration.adapters import MockBankAdapter
from payment_orchestration.domain import Party, Payment
from payment_orchestration.repository import SQLiteIntegrationRepository
from payment_orchestration.service import PaymentService


def sample_payment() -> Payment:
    return Payment(
        "restart-demo",
        "restart-demo-request",
        "DEMO-001",
        date(2026, 8, 11),
        Party("Demo payer", "PAYER", "PAYER-KPP", "PAYER-ACCOUNT"),
        Party("Demo recipient", "RECIPIENT", "RECIPIENT-KPP", "RECIPIENT-ACCOUNT", "BIK"),
        Decimal("100.00"),
        "RUB",
        "Local durable-recovery demo; no client data.",
    )


def run(database: Path) -> dict[str, object]:
    item = sample_payment()
    faulting = SQLiteIntegrationRepository(str(database), fault_at="send_response_before_persist")
    try:
        PaymentService(faulting, MockBankAdapter()).send(item)
    except RuntimeError as error:
        if "send_response_before_persist" not in str(error):
            raise
    before_restart = faulting.get(item.operation_id()).status
    faulting.close()

    adapter = MockBankAdapter()
    recovered = PaymentService(SQLiteIntegrationRepository(str(database)), adapter)
    after_restart = recovered.send(item)
    return {
        "operation_id": item.operation_id(),
        "status_before_restart": before_restart,
        "status_after_recovery": after_restart.status,
        "recovered_operations": len(recovered.recovered_operations),
        "external_send_attempts_after_recovery": len(adapter.sent),
        "recovery_contract": recovered.recovery_contract,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    if args.database:
        result = run(args.database)
    else:
        with TemporaryDirectory(prefix="payment-restart-demo-") as temporary:
            result = run(Path(temporary) / "operations.sqlite")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
