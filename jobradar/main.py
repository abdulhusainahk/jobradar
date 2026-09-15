"""Fetch, match, optionally score, deliver, and persist retryable alerts."""
from __future__ import annotations

import os
import sys
import time

import yaml

from . import (describe, experience, fetchers, filter as jf, fit as jfit,
               notify, score, state)


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def ranking_score(job: dict) -> int:
    ai = job.get("ai_score")
    return ai if ai is not None else (job.get("fit") or {}).get("score", 0)


def _delivered(jobs: list[dict], outcomes: dict, channels: list[str]) -> bool:
    return bool(channels) and all(
        outcomes.get(channel, {}).get(state.key(job)) is True
        for job in jobs for channel in channels
    )


def run() -> int:
    cfg = load_config(os.environ.get("JOBRADAR_CONFIG", "config.yaml"))
    match = cfg.get("match", {})
    companies = cfg.get("companies", [])
    channels = notify.configured_channels()

    if _truthy(os.environ.get("JOBRADAR_TEST_ALERT", "")):
        test_job = {
            "id": "self-test",
            "title": "[TEST] Senior DevOps Engineer",
            "company": "JobRadar Self-Test",
            "tier": "delivery check",
            "location": "Mumbai, India",
            "url": "https://github.com/abdulhusainahk/jobradar",
            "posted_ts": time.time(),
            "seniority": "Senior",
            "_india": True,
        }
        outcomes = notify.dispatch([test_job])
        success = _delivered([test_job], outcomes, channels)
        print(f"[test] delivery {'succeeded' if success else 'failed or unconfigured'}; "
              "AI scoring was not exercised", file=sys.stderr)
        return 0 if success else 1

    resend = _truthy(os.environ.get("JOBRADAR_RESEND_ALL", ""))
    st = state.load()
    baseline = not resend and not st["initialized"]
    fetchers.FETCH_ERRORS.clear()
    matched_new: list[dict] = []
    fetched_keys: set[str] = set()
    total_seen = 0
    for company in companies:
        for job in fetchers.fetch_company(company):
            total_seen += 1
            key = state.key(job)
            if key in fetched_keys:
                continue
            fetched_keys.add(key)
            job["_finance"] = bool(company.get("finance"))
            if not resend and not state.is_new(st, job):
                continue
            if not jf.is_candidate(job, match):
                continue
            job["tier"] = company.get("tier", "")
            job["seniority"] = jf.seniority_tag(job["title"], match)
            job["_c"] = company
            matched_new.append(job)

    print(f"\nScanned {total_seen} roles across {len(companies)} sources; "
          f"{len(matched_new)} new candidate(s).", file=sys.stderr)
    degraded = bool(fetchers.FETCH_ERRORS)
    if degraded:
        print(f"[sources] degraded: {len(fetchers.FETCH_ERRORS)} fetch failure(s); "
              "results are incomplete (see source logs)", file=sys.stderr)

    if baseline:
        # An empty healthy baseline is still initialized. A completely failed
        # scan must not silently establish an empty baseline.
        if total_seen or not degraded:
            state.initialize(st, matched_new)
        state.save(st)
        print(f"[baseline] recorded {len(matched_new)} existing matches; no alerts sent",
              file=sys.stderr)
        return 1 if degraded else 0

    for job in matched_new:
        job["_jd"] = describe.enrich_jd(job)
        job["fit"] = jfit.devops_fit(job, job["_jd"])
        job["_india"] = jf.location_is_india(
            job["location"], match,
            job.get("country_code") or job.get("countryCode") or job.get("country"),
        )
        job["_location_status"] = jf.location_status(job, match)

    scored = score.score_jobs(matched_new, cfg)
    ai_used = bool(matched_new) and all(job.get("ai_decision") for job in matched_new)
    if not ai_used:
        scored = []
        for job in matched_new:
            assessment = experience.assess(
                job["_jd"], match.get("candidate_years", 6), match.get("max_required_years", 11)
            )
            job["exp_note"] = assessment["note"]
            threshold = match.get("drop_monitoring_below", 0)
            monitoring_drop = (
                threshold and job["fit"].get("monitoring_only")
                and job["fit"]["score"] < threshold
            )
            if (not jf.passes(job, match) or monitoring_drop
                    or (match.get("drop_out_of_band") and assessment["drop"])):
                continue
            scored.append(job)
    if matched_new:
        mode = "AI" if ai_used else "heuristic fallback"
        print(f"[selection] {mode}: retained {len(scored)} of {len(matched_new)} candidates",
              file=sys.stderr)
    kept_keys = {state.key(job) for job in scored}
    if not resend:
        for job in matched_new:
            if state.key(job) not in kept_keys:
                state.reject(st, job)
        for job in scored:
            state.enqueue(st, job, channels)
        alerts = state.pending_jobs(st)
        for job in alerts:
            state.enqueue(st, job, channels)  # Bind an initially channel-less backlog.
        state.save(st)  # Durable outbox before external side effects.
    else:
        alerts = scored

    alerts.sort(key=lambda job: (ranking_score(job), job.get("posted_ts", 0.0)),
                reverse=True)
    for job in alerts:
        print(f"  → [{ranking_score(job):>3}] {job['company']}: {job['title']} "
              f"[{job['location']}]", file=sys.stderr)

    routes = None if resend else {
        state.key(job): state.channels_due(st, job) for job in alerts
    }
    outcomes = notify.dispatch(alerts, routes)
    if resend:
        failed = bool(alerts) and not _delivered(alerts, outcomes, channels)
    else:
        state.record_delivery(st, outcomes)
        state.save(st)
        failed = bool(channels and st["pending"])
        if st["pending"]:
            print(f"[delivery] {len(st['pending'])} alert(s) pending; "
                  + ("failed channels will retry" if channels else "no channels configured"),
                  file=sys.stderr)
    return 1 if degraded or failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
