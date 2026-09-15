"""Atomic, versioned deduplication history and per-channel delivery outbox."""
from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timezone

STATE_FILE = os.environ.get("JOBRADAR_STATE", "seen_jobs.json")


class StateError(ValueError):
    """Persisted state cannot safely be interpreted without losing history."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def key(job: dict) -> str:
    return f"{job.get('company')}::{job.get('id')}"


def _validate(data: dict) -> None:
    if not isinstance(data, dict) or data.get("version") != 2:
        raise StateError("Unsupported delivery state version")
    if type(data.get("initialized")) is not bool:
        raise StateError("State initialized flag must be a boolean")
    seen, pending = data.get("seen"), data.get("pending")
    if not isinstance(seen, dict) or not all(isinstance(v, str) for v in seen.values()):
        raise StateError("State seen history must map job keys to timestamps")
    if not isinstance(pending, dict):
        raise StateError("State pending outbox must be an object")
    for job_key, entry in pending.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("job"), dict):
            raise StateError("Pending delivery must contain a job snapshot")
        if key(entry["job"]) != job_key or job_key in seen:
            raise StateError("Pending delivery conflicts with job identity or seen history")
        for field in ("channels", "delivered"):
            values = entry.get(field)
            if (not isinstance(values, list)
                    or not all(isinstance(value, str) and value for value in values)
                    or len(set(values)) != len(values)):
                raise StateError(f"Pending delivery {field} must contain unique channel names")
        if not set(entry["delivered"]).issubset(entry["channels"]):
            raise StateError("Pending delivery contains an unrequested successful channel")
    if not data["initialized"] and (seen or pending):
        raise StateError("Uninitialized delivery state must not contain history")


def load() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return {"version": 2, "initialized": False, "seen": {}, "pending": {}}
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise StateError(f"Cannot decode delivery state: {STATE_FILE}") from exc
    if isinstance(data, dict) and "version" not in data:
        # An existing legacy file, even an empty one, represents a completed
        # baseline. Never re-baseline merely because its seen map is empty.
        if set(data) != {"seen"}:
            raise StateError("Unrecognized legacy delivery state")
        data = {**data, "version": 2, "initialized": True, "pending": {}}
    _validate(data)
    return data


def is_new(state: dict, job: dict) -> bool:
    job_key = key(job)
    return job_key not in state["seen"] and job_key not in state["pending"]


def initialize(state: dict, jobs: list[dict]) -> None:
    """Explicitly record the first successful scan without sending alerts."""
    if state["initialized"]:
        raise StateError("Delivery baseline is already initialized")
    timestamp = _now()
    state["seen"].update({key(job): timestamp for job in jobs})
    state["initialized"] = True


def reject(state: dict, job: dict) -> None:
    """Finalize a matching rejection, never an undelivered queued alert."""
    job_key = key(job)
    if not state["initialized"] or job_key in state["pending"]:
        raise StateError("Cannot reject an uninitialized or pending alert")
    state["seen"][job_key] = _now()


def enqueue(state: dict, job: dict, channels: list[str]) -> None:
    """Retain a retryable snapshot; freeze required channels once available."""
    if not state["initialized"]:
        raise StateError("Initialize the delivery baseline before queuing alerts")
    job_key = key(job)
    if job_key in state["seen"]:
        return
    required = list(dict.fromkeys(channels))
    entry = state["pending"].get(job_key)
    if entry is None:
        snapshot = {name: value for name, value in job.items()
                    if not name.startswith("_") or name == "_india"}
        state["pending"][job_key] = {
            "job": copy.deepcopy(snapshot), "channels": required, "delivered": []}
    elif not entry["channels"]:
        # Jobs discovered with no configured destination must remain pending;
        # a later run can bind channels even if the provider removed the role.
        entry["channels"] = required


def pending_jobs(state: dict) -> list[dict]:
    """Return saved alerts independently of the current provider response."""
    return [copy.deepcopy(entry["job"]) for entry in state["pending"].values()]


def channels_due(state: dict, job: dict) -> list[str]:
    entry = state["pending"].get(key(job))
    if entry is None:
        return []
    return [channel for channel in entry["channels"] if channel not in entry["delivered"]]


def record_delivery(state: dict, outcomes: dict[str, dict[str, bool]]) -> None:
    """Acknowledge only explicit successes; finalize all required channels."""
    for channel, results in outcomes.items():
        for job_key, success in results.items():
            entry = state["pending"].get(job_key)
            if (success is True and entry is not None and channel in entry["channels"]
                    and channel not in entry["delivered"]):
                entry["delivered"].append(channel)
    for job_key, entry in list(state["pending"].items()):
        if entry["channels"] and set(entry["channels"]).issubset(entry["delivered"]):
            state["seen"][job_key] = _now()
            del state["pending"][job_key]


def save(state: dict) -> None:
    """Replace state atomically after flushing a sibling temporary file."""
    _validate(state)
    destination = os.path.abspath(STATE_FILE)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=os.path.dirname(destination),
                                         prefix=".jobradar-", suffix=".tmp",
                                         delete=False) as stream:
            temporary = stream.name
            json.dump(state, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
