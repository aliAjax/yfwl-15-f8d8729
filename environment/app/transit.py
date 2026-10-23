#!/usr/bin/env python3
"""Drift-linked transit survey entry point."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--history", required=True)
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    targets = payload.get("targets", [])
    curves = payload.get("light_curves", [])
    batch_logs = payload.get("batch_logs", [])
    report = {
        "schema_version": 1,
        "case_id": payload.get("case_id", ""),
        "catalog": [],
        "status": "uncalibrated" if len(targets) == len(curves) and batch_logs else "needs_review",
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    Path(args.history).parent.mkdir(parents=True, exist_ok=True)
    if not Path(args.history).exists():
        Path(args.history).write_text("", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
