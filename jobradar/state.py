"""Atomic, versioned deduplication history and per-channel delivery outbox."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone, timedelta

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
    review = data.get("review", {})
    if not isinstance(review, dict):
        raise StateError("Review history must be an object")
    for job_key, record in review.items():
        try:
            if job_key in seen or job_key in pending or not isinstance(record, dict):
                raise ValueError()
            deadline = datetime.fromisoformat(record["next_review_at"])
            if deadline.tzinfo is None or not all(isinstance(record[field], str) for field in ("signature", "policy_version", "reason", "mode")):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise StateError("Invalid review history record") from None
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
    if not data["initialized"] and (seen or pending or review):
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


def listing_signature(job):
    return hashlib.sha256(json.dumps([job.get(field, "") for field in ("title", "location", "url")],
                                     ensure_ascii=False).encode()).hexdigest()


def is_new(state: dict, job: dict, policy_version="") -> bool:
    job_key = key(job)
    if job_key in state["seen"] or job_key in state["pending"]:
        return False
    review = state.get("review", {}).get(job_key)
    return (not review or review["signature"] != listing_signature(job)
            or review["policy_version"] != policy_version
            or datetime.fromisoformat(review["next_review_at"]) <= datetime.now(timezone.utc))


def initialize(state: dict, jobs: list[dict]) -> None:
    """Explicitly record the first successful scan without sending alerts."""
    if state["initialized"]:
        raise StateError("Delivery baseline is already initialized")
    timestamp = _now()
    state["seen"].update({key(job): timestamp for job in jobs})
    state["initialized"] = True


def reject(state: dict, job: dict, *, policy_version="", temporary=False) -> None:
    """Keep an auditable, expiring decision separate from actual delivery history."""
    job_key = key(job)
    if not state["initialized"] or job_key in state["pending"]:
        raise StateError("Cannot reject an uninitialized or pending alert")
    if job_key in state["seen"]:
        return
    state.setdefault("review", {})[job_key] = {
        "at": _now(), "next_review_at": (datetime.now(timezone.utc) + timedelta(hours=6 if temporary else 168)).isoformat(),
        "mode": "heuristic" if temporary else "ai", "reason": job.get("ai_note") or job.get("exp_note") or "Heuristic matching rejected this role",
        "signature": listing_signature(job), "policy_version": policy_version,
        "jd_hash": hashlib.sha256(job.get("_jd", "").encode()).hexdigest(),
        "ai_assessment": {field: value for field, value in job.items() if field.startswith("ai_")},
    }


def enqueue(state: dict, job: dict, channels: list[str]) -> None:
    """Retain a retryable snapshot; freeze required channels once available."""
    if not state["initialized"]:
        raise StateError("Initialize the delivery baseline before queuing alerts")
    job_key = key(job)
    if job_key in state["seen"]:
        return
    state.get("review", {}).pop(job_key, None)
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
