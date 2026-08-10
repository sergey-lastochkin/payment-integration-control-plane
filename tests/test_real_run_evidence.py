import importlib.util
from pathlib import Path

MODULE = Path(__file__).parents[1] / "real_run" / "scripts" / "evidence.py"
SPEC = importlib.util.spec_from_file_location("real_run_evidence", MODULE)
assert SPEC and SPEC.loader
evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evidence)


def test_new_record_has_waiting_status_and_required_capture_fields():
    record = evidence.new_record("upp-n8n-2026-08-11-01", Path(__file__).parents[1])
    assert record["status"] == "WAITING_FOR_ENVIRONMENT"
    assert record["code_commit"]
    assert evidence.require_completion_fields(record)


def test_events_and_fields_are_recorded_without_changing_schema():
    record = evidence.new_record("upp-n8n-2026-08-11-02", Path(__file__).parents[1])
    evidence.append_event(record, {"kind": "duplicate", "value": {"suppressed": True}})
    evidence.set_field(record, "duration_ms", 124)
    assert record["duplicate_suppression"][0]["value"]["suppressed"] is True
    assert record["duration_ms"] == 124
