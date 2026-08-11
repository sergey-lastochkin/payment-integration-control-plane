"""Run the local real-HTTP n8n and synthetic-bank acceptance scenarios.

This is intentionally a local test harness, not a 1C connector.  It starts
two FastAPI services, imports two native-node workflows into the pinned
official n8n image, and sends real localhost HTTP requests through the route
below.  All durable traces stay under ``real_run/private``; a redacted summary
can be written separately into ``real_run/runs`` after review.

    HTTP client -> n8n -> payment control plane -> bank emulator
                <- n8n callback <- bank emulator
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = Path(__file__).with_name("compose.yml")
N8N_URL = "http://127.0.0.1:5678"
CONTROL_PLANE_URL = "http://127.0.0.1:18080"
BANK_URL = "http://127.0.0.1:18081"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def wait_for(url: str, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=1.5)
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}"
        except httpx.HTTPError as error:
            last_error = type(error).__name__
        time.sleep(0.25)
    raise RuntimeError(f"service did not become ready: {url} ({last_error})")


class DockerAPI:
    """Small Unix-socket client used only around a known Docker CLI start hang."""

    def __init__(self) -> None:
        endpoint = subprocess.check_output(
            [
                "docker",
                "context",
                "inspect",
                "--format",
                '{{(index .Endpoints "docker").Host}}',
            ],
            text=True,
        ).strip()
        if not endpoint.startswith("unix://"):
            raise RuntimeError(
                f"only a local Unix Docker socket is supported: {endpoint}"
            )
        socket_path = endpoint.removeprefix("unix://")
        if not os.path.exists(socket_path):
            raise RuntimeError("Docker Unix socket is unavailable")
        self.client = httpx.Client(
            transport=httpx.HTTPTransport(uds=socket_path),
            base_url="http://docker",
            timeout=12,
        )

    def _request(self, method: str, path: str) -> httpx.Response:
        response = self.client.request(method, f"/v1.53{path}")
        if response.status_code >= 400:
            raise RuntimeError(
                f"Docker API {method} {path}: {response.status_code} {response.text}"
            )
        return response

    def start(self, container_id: str) -> None:
        self._request("POST", f"/containers/{container_id}/start")

    def stop(self, container_id: str) -> None:
        self._request("POST", f"/containers/{container_id}/stop?t=8")

    def remove(self, container_id: str) -> None:
        self._request("DELETE", f"/containers/{container_id}?force=1&v=0")

    def inspect(self, container_id: str) -> dict[str, Any]:
        return self._request("GET", f"/containers/{container_id}/json").json()

    def close(self) -> None:
        self.client.close()


class LocalRealRun:
    def __init__(
        self, run_id: str, private_root: Path, public_output: Path | None
    ) -> None:
        self.run_id = run_id
        self.private_root = private_root
        self.public_output = public_output
        self.run_dir = private_root / run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.scenarios: list[dict[str, object]] = []
        self.project_name = run_id.replace("_", "-")
        self.duration_ms: int | None = None
        self.docker = DockerAPI()
        self.import_container_id: str | None = None
        self.n8n_container_id: str | None = None
        self.payment_container_id: str | None = None
        self.bank_container_id: str | None = None
        self.n8n_execution_ids: list[str] = []

    def compose(
        self, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "docker",
                "compose",
                "--project-name",
                self.project_name,
                "-f",
                str(COMPOSE),
                *args,
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=check,
        )

    def container_id(self, service: str) -> str:
        result = self.compose("ps", "--all", "--quiet", service)
        container_id = result.stdout.strip()
        if not container_id:
            raise RuntimeError(f"Docker Compose did not create service {service}")
        return container_id

    def wait_container(
        self, container_id: str, expected: str, timeout: float = 35.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            state = self.docker.inspect(container_id).get("State", {})
            if state.get("Status") == expected:
                return state
            time.sleep(0.2)
        raise RuntimeError(
            f"container {container_id} did not reach {expected}: {state}"
        )

    def start_n8n(self) -> None:
        self.compose("create", "workflow-import")
        self.import_container_id = self.container_id("workflow-import")
        subprocess.run(
            [
                "docker",
                "cp",
                str(COMPOSE.with_name("workflow.json")),
                f"{self.import_container_id}:/tmp/workflow.json",
            ],
            cwd=ROOT,
            check=True,
        )
        self.docker.start(self.import_container_id)
        state = self.wait_container(self.import_container_id, "exited", timeout=50)
        if state.get("ExitCode") != 0:
            raise RuntimeError(f"n8n workflow import failed: {state}")
        self.activate_imported_workflows()
        self.compose("create", "n8n")
        self.n8n_container_id = self.container_id("n8n")
        self.docker.start(self.n8n_container_id)
        wait_for(f"{N8N_URL}/healthz", timeout=75)
        self.wait_n8n_workflows()

    def wait_n8n_workflows(self) -> None:
        if not self.n8n_container_id:
            raise RuntimeError("n8n container id is unavailable")
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            logs = subprocess.run(
                ["docker", "logs", self.n8n_container_id],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            ).stdout
            if logs.count("=> Started") >= 2:
                return
            time.sleep(0.5)
        raise RuntimeError(
            "n8n became healthy but did not register both active workflows"
        )

    def activate_imported_workflows(self) -> None:
        """Enable the test workflows which ``n8n import:workflow`` deactivates.

        This changes only the named volume belonging to this run, while the
        importer is stopped.  No user account, credential, or remote n8n is
        involved.  The local database is then closed and checkpointed before
        copying it back, so n8n registers ordinary active webhooks at startup.
        """

        if not self.import_container_id:
            raise RuntimeError("workflow importer container id is unavailable")
        staging = self.run_dir / "n8n-activation"
        subprocess.run(
            [
                "docker",
                "cp",
                f"{self.import_container_id}:/home/node/.n8n/.",
                str(staging),
            ],
            cwd=ROOT,
            check=True,
        )
        database = staging / "database.sqlite"
        with sqlite3.connect(database) as connection:
            updated = connection.execute(
                "UPDATE workflow_entity SET active = 1 "
                "WHERE id IN ('payment-real-run-intake', 'payment-real-run-callback')"
            ).rowcount
            if updated != 2:
                raise RuntimeError(
                    f"expected two imported workflows, updated {updated}"
                )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for filename in ("database.sqlite", "database.sqlite-wal"):
            source = staging / filename
            if source.exists():
                subprocess.run(
                    [
                        "docker",
                        "cp",
                        str(source),
                        f"{self.import_container_id}:/home/node/.n8n/{filename}",
                    ],
                    cwd=ROOT,
                    check=True,
                )
        self.restore_n8n_volume_owner()

    def restore_n8n_volume_owner(self) -> None:
        """Return copied test-store files to the official image's ``node`` user."""

        helper_name = f"{self.project_name}-n8n-volume-owner"
        volume_name = f"{self.project_name}_n8n-data"
        created = subprocess.check_output(
            [
                "docker",
                "create",
                "--name",
                helper_name,
                "--user",
                "0:0",
                "--mount",
                f"type=volume,source={volume_name},target=/data",
                "alpine:3.21",
                "sh",
                "-c",
                "chown -R 1000:1000 /data",
            ],
            cwd=ROOT,
            text=True,
        ).strip()
        try:
            self.docker.start(created)
            state = self.wait_container(created, "exited")
            if state.get("ExitCode") != 0:
                raise RuntimeError(f"n8n volume ownership repair failed: {state}")
        finally:
            self.docker.remove(created)

    def build_runtime_image(self) -> None:
        result = subprocess.run(
            [
                "docker",
                "build",
                "--tag",
                "payment-real-run-control-plane:dev",
                "--file",
                str(COMPOSE.with_name("Dockerfile")),
                str(ROOT),
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        (self.run_dir / "runtime-image-build.log").write_text(
            result.stdout, encoding="utf-8"
        )
        result.check_returncode()

    def start_services(self) -> None:
        self.build_runtime_image()
        self.compose("create", "bank")
        self.bank_container_id = self.container_id("bank")
        self.docker.start(self.bank_container_id)
        wait_for(f"{BANK_URL}/health")
        self.compose("create", "payment")
        self.payment_container_id = self.container_id("payment")
        self.docker.start(self.payment_container_id)
        wait_for(f"{CONTROL_PLANE_URL}/health")
        self.start_n8n()

    def restart_payment(self) -> None:
        if not self.payment_container_id:
            raise RuntimeError("payment container was not started")
        self.docker.stop(self.payment_container_id)
        self.docker.start(self.payment_container_id)
        wait_for(f"{CONTROL_PLANE_URL}/health")

    def close(self) -> None:
        try:
            containers = {
                "n8n": self.n8n_container_id,
                "payment": self.payment_container_id,
                "bank": self.bank_container_id,
            }
            for name, container_id in containers.items():
                if container_id:
                    with (self.run_dir / f"{name}.log").open(
                        "w", encoding="utf-8"
                    ) as stream:
                        subprocess.run(
                            ["docker", "logs", container_id],
                            stdout=stream,
                            stderr=subprocess.STDOUT,
                            text=True,
                            check=False,
                        )
            for container_id, source, target in (
                (
                    self.payment_container_id,
                    "/data/payment.trace.jsonl",
                    "payment.trace.jsonl",
                ),
                (self.bank_container_id, "/data/bank.trace.jsonl", "bank.trace.jsonl"),
            ):
                if container_id:
                    self.docker.stop(container_id)
                    subprocess.run(
                        [
                            "docker",
                            "cp",
                            f"{container_id}:{source}",
                            str(self.run_dir / target),
                        ],
                        cwd=ROOT,
                        check=False,
                    )
            for container_id in (
                self.n8n_container_id,
                self.import_container_id,
                self.payment_container_id,
                self.bank_container_id,
            ):
                if container_id:
                    self.docker.remove(container_id)
        finally:
            self.docker.close()

    def payment_record(self, operation_id: str) -> dict[str, Any]:
        response = httpx.get(
            f"{CONTROL_PLANE_URL}/v1/payments/{operation_id}", timeout=3
        )
        response.raise_for_status()
        return response.json()

    def wait_status(
        self, operation_id: str, expected: str, timeout: float = 12.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                last = self.payment_record(operation_id)
                if last["status"] == expected:
                    return last
            except httpx.HTTPError:
                pass
            time.sleep(0.15)
        raise AssertionError(f"{operation_id} did not reach {expected}: {last}")

    def send_webhook(
        self, document_ref: str, mode: str, *, request_id: str | None = None
    ) -> httpx.Response:
        request_id = request_id or f"request:{document_ref}"
        body = {
            "run_id": self.run_id,
            "request_id": request_id,
            "correlation_id": f"correlation:{document_ref}",
            "document_ref": document_ref,
            "test_mode": mode,
            "desired_status": "accepted",
            "amount": "1.00",
        }
        response = httpx.post(f"{N8N_URL}/webhook/payment", json=body, timeout=20)
        if response.status_code == 200:
            payload = response.json()
            execution_id = str(payload.get("n8n_execution_id", ""))
            if not execution_id or execution_id == "undefined":
                raise AssertionError("n8n did not return a concrete execution id")
            self.n8n_execution_ids.append(execution_id)
        return response

    @staticmethod
    def operation_id(document_ref: str) -> str:
        return f"upp-payment:{document_ref}"

    def bank_rows(self, operation_id: str) -> int:
        response = httpx.get(
            f"{BANK_URL}/testing/payments/{operation_id}/count", timeout=3
        )
        response.raise_for_status()
        return int(response.json()["count"])

    def add_scenario(self, name: str, **result: object) -> None:
        self.scenarios.append({"name": name, "passed": True, **result})

    def run_scenarios(self) -> None:
        normal_doc = "TEST-LOCAL-NORMAL-001"
        normal_response = self.send_webhook(normal_doc, "normal")
        normal = self.wait_status(self.operation_id(normal_doc), "accepted")
        assert normal_response.status_code == 200
        self.add_scenario(
            "A_normal_route",
            n8n_http_status=normal_response.status_code,
            final_status=normal["status"],
            attempts=normal["attempts"],
        )

        duplicate = self.send_webhook(normal_doc, "normal")
        duplicate_record = self.wait_status(self.operation_id(normal_doc), "accepted")
        assert duplicate.status_code == 200
        assert duplicate_record["attempts"] == 1
        assert self.bank_rows(self.operation_id(normal_doc)) == 1
        self.add_scenario(
            "B_duplicate_submit",
            n8n_http_status=duplicate.status_code,
            attempts=duplicate_record["attempts"],
            bank_rows=1,
        )

        delayed_doc = "TEST-LOCAL-DELAYED-001"
        started = time.monotonic()
        delayed = self.send_webhook(delayed_doc, "delayed_response")
        delayed_record = self.wait_status(self.operation_id(delayed_doc), "accepted")
        assert delayed.status_code == 200
        assert time.monotonic() - started >= 0.3
        self.add_scenario(
            "C_delayed_response",
            n8n_http_status=delayed.status_code,
            final_status=delayed_record["status"],
        )

        before_doc = "TEST-LOCAL-CALLBACK-FIRST-001"
        before = self.send_webhook(before_doc, "callback_before_response")
        before_record = self.wait_status(self.operation_id(before_doc), "accepted")
        sources = [event["source"] for event in before_record["audit"]]
        assert before.status_code == 200
        assert "callback" in sources and "bank_adapter" not in sources
        self.add_scenario(
            "D_callback_before_response",
            n8n_http_status=before.status_code,
            final_status=before_record["status"],
            stronger_status_preserved=True,
        )

        callback_duplicate_doc = "TEST-LOCAL-DUPLICATE-CALLBACK-001"
        duplicate_callback = self.send_webhook(
            callback_duplicate_doc, "duplicate_callback"
        )
        duplicate_callback_record = self.wait_status(
            self.operation_id(callback_duplicate_doc), "accepted"
        )
        accepted_events = [
            event
            for event in duplicate_callback_record["audit"]
            if event["to"] == "accepted"
        ]
        assert duplicate_callback.status_code == 200
        assert len(accepted_events) == 1
        self.add_scenario(
            "E_duplicate_callback",
            n8n_http_status=duplicate_callback.status_code,
            accepted_transitions=len(accepted_events),
        )

        lost_doc = "TEST-LOCAL-RESPONSE-LOST-001"
        lost_operation = self.operation_id(lost_doc)
        lost = self.send_webhook(lost_doc, "response_lost_after_commit")
        self.wait_status(lost_operation, "sending")
        assert lost.status_code >= 500
        self.restart_payment()
        unknown = self.wait_status(lost_operation, "outcome_unknown")
        recovered = httpx.post(
            f"{CONTROL_PLANE_URL}/v1/payments/{lost_operation}/recover", timeout=6
        )
        recovered.raise_for_status()
        recovered_record = self.wait_status(lost_operation, "accepted")
        replay = self.send_webhook(lost_doc, "response_lost_after_commit")
        assert replay.status_code == 200
        assert self.bank_rows(lost_operation) == 1
        assert recovered_record["attempts"] == 1
        self.add_scenario(
            "F_response_lost_after_commit",
            n8n_http_status=lost.status_code,
            recovered_from=unknown["status"],
            final_status=recovered_record["status"],
            bank_rows=1,
            blind_resend=False,
        )

        repeated_doc = "TEST-LOCAL-REPEATED-EVENT-001"
        repeated_operation = self.operation_id(repeated_doc)
        repeated = self.send_webhook(repeated_doc, "normal")
        repeated_record = self.wait_status(repeated_operation, "accepted")
        event_id = f"repeat-event:{repeated_operation}"
        callback_request = {"status": "accepted", "event_id": event_id}
        first = httpx.post(
            f"{BANK_URL}/testing/payments/{repeated_operation}/status",
            json=callback_request,
            timeout=8,
        )
        second = httpx.post(
            f"{BANK_URL}/testing/payments/{repeated_operation}/status",
            json=callback_request,
            timeout=8,
        )
        settled = self.wait_status(repeated_operation, "accepted")
        assert repeated.status_code == first.status_code == second.status_code == 200
        assert len(settled["audit"]) == len(repeated_record["audit"])
        self.add_scenario(
            "G_repeated_bank_event",
            callback_http_statuses=[first.status_code, second.status_code],
            state_transition_replayed=False,
        )

        conflict_doc = "TEST-LOCAL-CONFLICTING-CALLBACK-001"
        conflict = self.send_webhook(conflict_doc, "conflicting_callback")
        conflict_record = self.wait_status(
            self.operation_id(conflict_doc), "manual_check"
        )
        assert conflict.status_code == 200
        assert any(
            event["source"] == "callback_conflict" for event in conflict_record["audit"]
        )
        self.add_scenario(
            "H_conflicting_callback",
            n8n_http_status=conflict.status_code,
            final_status=conflict_record["status"],
            manual_check=True,
        )

    def n8n_executions(self) -> list[dict[str, object]]:
        database = self.run_dir / "n8n.database.sqlite"
        if not database.exists():
            return []
        try:
            with sqlite3.connect(database) as connection:
                rows = connection.execute(
                    """
                    SELECT execution_entity.id, workflow_entity.name, execution_entity.status
                    FROM execution_entity
                    LEFT JOIN workflow_entity ON workflow_entity.id = execution_entity.workflowId
                    ORDER BY CAST(execution_entity.id AS INTEGER)
                    """
                ).fetchall()
            return [
                {"id": str(row[0]), "workflow": row[1], "status": row[2]}
                for row in rows
            ]
        except sqlite3.Error:
            return []

    def capture_n8n_database(self) -> None:
        if not self.n8n_container_id:
            raise RuntimeError("n8n container id is unavailable")
        self.docker.stop(self.n8n_container_id)
        subprocess.run(
            [
                "docker",
                "cp",
                f"{self.n8n_container_id}:/home/node/.n8n/database.sqlite",
                str(self.run_dir / "n8n.database.sqlite"),
            ],
            cwd=ROOT,
            check=True,
        )

    def report(self) -> dict[str, object]:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        return {
            "schema_version": 1,
            "kind": "local_real_http_n8n_bank_validation",
            "status": "PASS",
            "run_id": self.run_id,
            "timestamp": utc_now(),
            "code_commit": commit,
            "duration_ms": self.duration_ms,
            "scope": {
                "synthetic_only": True,
                "components": [
                    "HTTP client",
                    "n8n:1.82.3",
                    "payment control plane",
                    "bank emulator",
                ],
                "one_c_participated": False,
            },
            "scenarios": self.scenarios,
            "n8n_executions": self.n8n_executions(),
            "n8n_execution_ids": self.n8n_execution_ids,
            "private_trace_collected": True,
        }

    def write_reports(self) -> dict[str, object]:
        report = self.report()
        write_json(self.run_dir / "summary.private.json", report)
        if self.public_output:
            forbidden = (
                "127.0.0.1",
                "localhost",
                "host.docker.internal",
                "token",
                "password",
            )
            serialized = json.dumps(report, ensure_ascii=False).lower()
            if any(value in serialized for value in forbidden):
                raise ValueError(
                    "refusing to publish environment or secret-shaped value"
                )
            write_json(self.public_output, report)
        return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--private-root", type=Path, default=ROOT / "real_run" / "private" / "runs"
    )
    parser.add_argument("--public-output", type=Path)
    args = parser.parse_args()
    if not args.run_id.startswith("real-n8n-bank-"):
        raise SystemExit("run-id must begin with real-n8n-bank-")
    run = LocalRealRun(args.run_id, args.private_root, args.public_output)
    started = time.monotonic()
    try:
        run.start_services()
        run.run_scenarios()
        run.duration_ms = round((time.monotonic() - started) * 1000)
        run.capture_n8n_database()
        report = run.write_reports()
    except BaseException as error:
        write_json(
            run.run_dir / "failure.private.json",
            {
                "run_id": args.run_id,
                "timestamp": utc_now(),
                "error": f"{type(error).__name__}: {error}",
            },
        )
        raise
    finally:
        run.close()
    print(
        json.dumps(
            {"status": report["status"], "run_id": report["run_id"]}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
