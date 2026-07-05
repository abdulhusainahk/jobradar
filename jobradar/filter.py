"""Deterministic matching: role + location + seniority tag + exclusions."""
from __future__ import annotations


def _lower(s: str) -> str:
    return (s or "").lower()


def role_matches(title: str, m: dict) -> bool:
    t = _lower(title)
    return any(k.lower() in t for k in m.get("role_keywords", []))


def location_matches(location: str, m: dict) -> bool:
    loc = _lower(location)
    if not loc:
        return False
    if any(k.lower() in loc for k in m.get("location_india_keywords", [])):
        return True
    # bare "remote" only counts if paired with an allowed region
    if "remote" in loc:
        return any(k.lower() in loc for k in m.get("remote_region_keywords", []))
    return False


def is_excluded(job: dict, m: dict) -> bool:
    blob = f"{_lower(job.get('title'))} {_lower(job.get('location'))}"
    if any(k.lower() in blob for k in m.get("exclude_keywords", [])):
        return True
    comp = _lower(job.get("company"))
    if any(k.lower() in comp for k in m.get("exclude_companies", [])):
        return True
    return False


def seniority_tag(title: str, m: dict) -> str:
    t = _lower(title)
    hits = [k for k in m.get("seniority_keywords", []) if k.lower() in t]
    return hits[0].title() if hits else ""


def passes(job: dict, m: dict) -> bool:
    return (
        not is_excluded(job, m)
        and role_matches(job.get("title", ""), m)
        and location_matches(job.get("location", ""), m)
    )
