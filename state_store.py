"""Tiny persistent record of what was published, so videos can be deleted 10 days later.

Stored in state/published.json inside the repo; the workflow commits it after every run.
Contains only public video IDs/URLs (no secrets).
"""
from __future__ import annotations

import json
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent / "state"
STATE_FILE = STATE_DIR / "published.json"


def load() -> list[dict]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save(records: list[dict]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
