"""Render local failure-lab evidence from the saved JSON summary."""

from __future__ import annotations

import argparse
import json
from html import escape
from pathlib import Path

from payment_orchestration.domain import TRANSITIONS


def failure_summary(summary: dict[str, object], output: Path) -> None:
    scenarios = summary["scenarios"]
    width, height = 940, 92 + 34 * len(scenarios)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<text x="24" y="34" font-family="Arial, sans-serif" font-size="20">Локальный failure lab: сохранённый результат</text>',
        f'<text x="24" y="58" font-family="Arial, sans-serif" font-size="13" fill="#475569">run_id: {escape(str(summary["run_id"]))}; только эмулятор, без 1С, n8n и банка</text>',
    ]
    for index, scenario in enumerate(scenarios):
        y = 74 + index * 34
        name = escape(str(scenario["scenario"]))
        status = escape(str(scenario["status"]).upper())
        parts.extend(
            [
                f'<text x="24" y="{y + 20}" font-family="monospace" font-size="14">{name}</text>',
                f'<rect x="425" y="{y}" width="80" height="24" rx="4" fill="#15803d"/>',
                f'<text x="442" y="{y + 17}" font-family="monospace" font-size="13" fill="white">{status}</text>',
            ]
        )
    parts.append("</svg>")
    output.write_text("\n".join(parts), encoding="utf-8")


def status_model(output: Path) -> None:
    lines = ["stateDiagram-v2"]
    for source, targets in TRANSITIONS.items():
        for target in sorted(targets):
            lines.append(f"  {source.value} --> {target.value}")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    failure_summary(summary, args.output_dir / "failure-summary.svg")
    status_model(args.output_dir / "status-model.mmd")


if __name__ == "__main__":
    main()
