"""Persisted dedup store — a JSON file the GitHub Action commits each run."""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone

STATE_FILE = os.environ.get("JOBRADAR_STATE", "seen_jobs.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"seen": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("seen", {})
        return data
    except Exception:
        return {"seen": {}}


def key(job: dict) -> str:
    return f"{job.get('company')}::{job.get('id')}"


def is_new(state: dict, job: dict) -> bool:
    return key(job) not in state["seen"]


def mark(state: dict, job: dict) -> None:
    state["seen"][key(job)] = _now()


def save(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
