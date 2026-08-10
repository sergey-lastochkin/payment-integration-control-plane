"""Create and validate a redacted evidence record for a future test run."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

EVENT_FIELDS = {
    "http": "http_request_ids",
    "operation": "business_operation_ids",
    "n8n": "n8n_execution_ids",
    "transition": "status_transitions",
    "error": "errors",
    "duplicate": "duplicate_suppression",
}
SETTABLE_FIELDS = {"platform_1c_version", "configuration_version", "duration_ms", "status"}


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def code_commit(repository: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()


def new_record(run_id: str, repository: Path) -> dict[str, object]:
    if not run_id or any(char.isspace() for char in run_id):
        raise ValueError("run_id must be non-empty and contain no spaces")
    return {
        "schema_version": 1,
        "status": "WAITING_FOR_ENVIRONMENT",
        "run_id": run_id,
        "timestamp": timestamp(),
        "code_commit": code_commit(repository),
        "platform_1c_version": None,
        "configuration_version": None,
        "http_request_ids": [],
        "business_operation_ids": [],
        "n8n_execution_ids": [],
        "status_transitions": [],
        "duration_ms": None,
        "errors": [],
        "duplicate_suppression": [],
        "artifacts": {
            "screenshots_1c": [],
            "n8n_workflow": [],
            "http_evidence": [],
            "traces": [],
        },
    }


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_event(record: dict[str, object], event: dict[str, object]) -> None:
    kind = event.get("kind")
    if kind not in EVENT_FIELDS:
        raise ValueError(f"unknown event kind: {kind}")
    value = event.get("value")
    if value in (None, ""):
        raise ValueError("event value is required")
    record[EVENT_FIELDS[str(kind)]].append({"timestamp": timestamp(), "value": value})


def set_field(record: dict[str, object], field: str, value: object) -> None:
    if field not in SETTABLE_FIELDS:
        raise ValueError(f"field cannot be set through this tool: {field}")
    record[field] = value


def require_completion_fields(record: dict[str, object]) -> list[str]:
    required = [
        "platform_1c_version",
        "configuration_version",
        "http_request_ids",
        "business_operation_ids",
        "n8n_execution_ids",
        "status_transitions",
        "duration_ms",
        "duplicate_suppression",
    ]
    return [field for field in required if not record.get(field)]


def main() -> None:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    init = subcommands.add_parser("init")
    init.add_argument("--run-id", required=True)
    init.add_argument("--out", type=Path, required=True)
    init.add_argument("--repository", type=Path, default=Path.cwd())
    add = subcommands.add_parser("add-event")
    add.add_argument("--run", type=Path, required=True)
    add.add_argument("--event", type=Path, required=True)
    update = subcommands.add_parser("set")
    update.add_argument("--run", type=Path, required=True)
    update.add_argument("--field", required=True, choices=sorted(SETTABLE_FIELDS))
    update.add_argument("--value", required=True)
    check = subcommands.add_parser("check")
    check.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "init":
        write_json(args.out, new_record(args.run_id, args.repository))
        for folder in ("screenshots-1c", "n8n-workflow", "http-evidence", "traces"):
            (args.out.parent / folder).mkdir(exist_ok=True)
    else:
        record = json.loads(args.run.read_text(encoding="utf-8"))
        if args.command == "add-event":
            append_event(record, json.loads(args.event.read_text(encoding="utf-8")))
            write_json(args.run, record)
        elif args.command == "set":
            try:
                value = json.loads(args.value)
            except json.JSONDecodeError:
                value = args.value
            set_field(record, args.field, value)
            write_json(args.run, record)
        else:
            missing = require_completion_fields(record)
            print(json.dumps({"ready": not missing, "missing": missing}, ensure_ascii=False))
            if missing:
                raise SystemExit(2)


if __name__ == "__main__":
    main()
