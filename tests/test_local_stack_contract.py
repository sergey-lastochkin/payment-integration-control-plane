import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_local_n8n_stack_is_pinned_local_and_uses_native_nodes_only():
    compose = (ROOT / "real_run" / "local_stack" / "compose.yml").read_text(
        encoding="utf-8"
    )
    workflows = json.loads(
        (ROOT / "real_run" / "local_stack" / "workflow.json").read_text(
            encoding="utf-8"
        )
    )

    assert "n8nio/n8n:1.82.3" in compose
    assert "127.0.0.1:5678:5678" in compose
    assert "127.0.0.1:18080:18080" in compose
    assert "127.0.0.1:18081:18081" in compose
    assert len(workflows) == 2
    assert all(
        node["type"].startswith("n8n-nodes-base.")
        for workflow in workflows
        for node in workflow["nodes"]
    )
    assert all(
        "function" not in node["type"].lower()
        for workflow in workflows
        for node in workflow["nodes"]
    )
    assert any(
        node["name"] == "Return n8n execution evidence"
        for node in workflows[0]["nodes"]
    )
