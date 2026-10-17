"""Fetch, match, optionally score, deliver, and persist retryable alerts."""
from __future__ import annotations

import os
import hashlib
import json
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

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
    preview = _truthy(os.environ.get("JOBRADAR_PREVIEW", ""))
    preview_company = os.environ.get("JOBRADAR_PREVIEW_COMPANY", "").strip()
    if preview and preview_company:
        companies = [company for company in companies
                     if company["name"].casefold() == preview_company.casefold()]
        if len(companies) != 1:
            raise ValueError("preview_company must identify exactly one configured employer")
    policy_version = hashlib.sha256(json.dumps([2, cfg.get("profile"), match,
        os.environ.get("AI_SCORING", ""), os.environ.get("AI_MIN_SCORE", "0"),
        os.environ.get("JOBRADAR_MODEL", "")], sort_keys=True).encode()).hexdigest()

    if not preview and _truthy(os.environ.get("JOBRADAR_TEST_ALERT", "")):
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
    baseline = not preview and not resend and not st["initialized"]
    fetchers.FETCH_ERRORS.clear()
    matched_new: list[dict] = []
    fetched_keys: set[str] = set()
    total_seen = 0
    source_health = []
    for company in companies:
        before = len(fetchers.FETCH_ERRORS)
        jobs = fetchers.fetch_company(company)
        errors = fetchers.FETCH_ERRORS[before:]
        source_health.append({"company": company["name"], "roles": len(jobs),
                              "status": ("partial" if jobs else "failed") if errors else "healthy",
                              "errors": errors})
        for job in jobs:
            total_seen += 1
            key = state.key(job)
            if key in fetched_keys:
                continue
            fetched_keys.add(key)
            job["_finance"] = bool(company.get("finance"))
            if not resend and not state.is_new(st, job, policy_version):
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
    healthy = sum(source["status"] == "healthy" for source in source_health)
    health_cfg = cfg.get("health", {})
    minimum = float(health_cfg.get("minimum_healthy_fraction", 0.75))
    if not 0 <= minimum <= 1:
        raise ValueError("minimum_healthy_fraction must be between zero and one")
    critical = set(health_cfg.get("critical_companies", []))
    coverage_failed = (not companies or healthy == 0 or healthy / len(companies) < minimum
                       or any(source["company"] in critical and source["status"] != "healthy" for source in source_health))
    coverage_note = (f"Coverage {'incomplete' if degraded else 'healthy'}: {healthy}/{len(companies)} healthy sources. "
                     + ("Unavailable or partial: " + ", ".join(source["company"] for source in source_health if source["status"] != "healthy") if degraded else ""))

    def finish(delivery_failed=False, phase="complete", **details):
        report = {"at": datetime.now(timezone.utc).isoformat(), "mode": "preview" if preview else "monitor",
                  "phase": phase,
                  "coverage": "failed" if coverage_failed else "degraded" if degraded else "healthy",
                  "sources": source_health, "roles_scanned": total_seen, "new_candidates": len(matched_new),
                  "delivery_failed": delivery_failed, **details}
        destination = Path(os.environ.get("JOBRADAR_REPORT", ".state/jobradar-run.json"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2) + "\n")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary and phase == "complete":
            with open(summary, "a") as stream:
                stream.write(f"### JobRadar\n\n{coverage_note}\n\nScanned {total_seen} roles; {len(matched_new)} new candidates. "
                             f"Delivery failed: {delivery_failed}. Mode: {report['mode']}.\n")
        return 1 if coverage_failed or delivery_failed else 0
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
        return finish(baseline=True)

    # Keep source evidence available to the always-run artifact step even if
    # enrichment/assessment is interrupted before a final delivery outcome.
    finish(phase="assessment_pending", delivery="not_requested" if preview else "not_started")
    for job in matched_new:
        job["_jd"] = describe.enrich_jd(job)
        job["fit"] = jfit.devops_fit(job, job["_jd"])
        job["_india"] = jf.location_is_india(
            job["location"], match,
            job.get("country_code") or job.get("countryCode") or job.get("country"),
        )
        job["_location_status"] = jf.location_status(job, match)

    scored = score.score_jobs(matched_new, cfg)
    ai_keys = {state.key(job) for job in scored if job.get("ai_decision")}
    scored = [job for job in matched_new if state.key(job) in ai_keys]
    for job in matched_new:
        if not job.get("ai_decision"):
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
        assessed_count = sum(bool(job.get("ai_decision")) for job in matched_new)
        mode = "AI" if assessed_count == len(matched_new) else "mixed AI/heuristic" if assessed_count else "heuristic fallback"
        print(f"[selection] {mode}: retained {len(scored)} of {len(matched_new)} candidates",
              file=sys.stderr)
    kept_keys = {state.key(job) for job in scored}
    assessment = {"ai_assessed": sum(bool(job.get("ai_decision")) for job in matched_new),
                  "heuristic": sum(not job.get("ai_decision") for job in matched_new)}
    if preview:
        print(f"[preview] {len(scored)} selected; no delivery or history changes", file=sys.stderr)
        return finish(selected=len(scored), delivery="not_requested", assessment=assessment)
    if not resend:
        for job in matched_new:
            if state.key(job) not in kept_keys:
                state.reject(st, job, policy_version=policy_version, temporary=not bool(job.get("ai_decision")))
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
        job["_coverage"] = coverage_note
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
        failed = bool(st["pending"])
        if st["pending"]:
            print(f"[delivery] {len(st['pending'])} alert(s) pending; "
                  + ("failed channels will retry" if channels else "no channels configured"),
                  file=sys.stderr)
    return finish(failed, selected=len(alerts), outcomes=outcomes, review_pending=len(st.get("review", {})), assessment=assessment)


if __name__ == "__main__":
    raise SystemExit(run())
