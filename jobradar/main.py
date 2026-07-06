"""Orchestrator: fetch -> filter -> dedup -> (AI score) -> notify -> persist."""
from __future__ import annotations
import os
import sys

import yaml

from . import (describe, experience, fetchers, filter as jf, fit as jfit,
               notify, score, state)


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _truthy(v: str) -> bool:
    return str(v).lower() in ("1", "true", "on", "yes")


def run() -> int:
    cfg = load_config(os.environ.get("JOBRADAR_CONFIG", "config.yaml"))
    m = cfg.get("match", {})
    companies = cfg.get("companies", [])

    # Self-test: send ONE synthetic alert to prove Telegram/email are wired up,
    # then exit without touching any real state. Triggered by the workflow's
    # "test_alert" input (JOBRADAR_TEST_ALERT=true).
    if _truthy(os.environ.get("JOBRADAR_TEST_ALERT", "")):
        import time
        test_job = {
            "title": "[TEST] Senior DevOps Engineer",
            "company": "JobRadar Self-Test",
            "tier": "delivery check",
            "location": "Mumbai, India",
            "url": "https://github.com/abdulhusainahk/jobradar",
            "posted_ts": time.time(),
            "seniority": "Senior",
        }
        print("[test] sending one synthetic alert to confirm delivery...", file=sys.stderr)
        notify.dispatch([test_job])
        print("[test] done — if you didn't receive it, a secret is wrong "
              "(see the [telegram]/[email] lines above).", file=sys.stderr)
        return 0

    # resend_all: re-send ALL current matches as one fit-ranked digest, without
    # touching saved state (non-destructive, repeatable). For seeing the current
    # open roles in the new format on demand.
    resend = _truthy(os.environ.get("JOBRADAR_RESEND_ALL", ""))

    st = state.load()
    # First ever run: record what's already open as a baseline and DON'T alert,
    # so you only get pinged for roles posted after JobRadar goes live.
    baseline = (not resend) and (not st["seen"])

    tier_by_company = {c["name"]: c.get("tier", "") for c in companies}

    matched_new: list[dict] = []
    total_seen = 0
    for c in companies:
        for job in fetchers.fetch_company(c):
            total_seen += 1
            if not jf.passes(job, m):
                continue
            if not resend and not state.is_new(st, job):
                continue
            job["tier"] = tier_by_company.get(job["company"], "")
            job["seniority"] = jf.seniority_tag(job["title"], m)
            job["_c"] = c  # source config, for on-demand JD fetch
            matched_new.append(job)
            if not resend:
                state.mark(st, job)  # mark so we never re-alert, even if notify fails

    print(f"\nScanned {total_seen} roles across {len(companies)} companies; "
          f"{len(matched_new)} match(es).", file=sys.stderr)

    if baseline:
        print(f"[baseline] first run — recorded {len(matched_new)} current "
              "matches as seen; no alerts sent. Future new roles will alert.",
              file=sys.stderr)
        state.save(st)
        return 0

    # Analyze each new role's description for DevOps alignment vs your resume.
    cand = m.get("candidate_years", 6)
    max_req = m.get("max_required_years", 99)
    for job in matched_new:
        jd = describe.enrich_jd(job)
        job["_jd"] = jd  # cache for the optional AI layer
        job["fit"] = jfit.devops_fit(job, jd)
        job["_india"] = jf.location_is_india(job["location"], m)
        ex = experience.assess(jd, cand, max_req)
        job["exp_note"] = ex["note"]
        job["_exp_drop"] = ex["drop"]

    # Drop monitoring-only roles below the score threshold (their "not worth it"
    # bucket) — boosted senior/DevOps-titled roles can survive it.
    thresh = m.get("drop_monitoring_below", 0)
    if thresh and matched_new:
        kept = [j for j in matched_new
                if not (j["fit"].get("monitoring_only") and j["fit"]["score"] < thresh)]
        dropped = len(matched_new) - len(kept)
        if dropped:
            print(f"[fit] dropped {dropped} monitoring-only role(s) below "
                  f"{thresh}", file=sys.stderr)
        matched_new = kept

    # Drop roles whose required experience is outside your band (too junior/senior).
    if m.get("drop_out_of_band") and matched_new:
        kept = [j for j in matched_new if not j.get("_exp_drop")]
        dropped = len(matched_new) - len(kept)
        if dropped:
            print(f"[exp] dropped {dropped} out-of-band role(s)", file=sys.stderr)
        matched_new = kept

    matched_new = score.score_jobs(matched_new)  # no-op unless AI_SCORING=on

    # Rank: best DevOps fit first, then newest. (Fit is what you asked to lead on;
    # recency breaks ties so fresh strong-fit roles float to the very top.)
    matched_new.sort(key=lambda j: (j["fit"]["score"], j.get("posted_ts", 0.0)),
                     reverse=True)

    if matched_new:
        for j in matched_new:
            print(f"  → [{j['fit']['score']:>3}] {j['company']}: {j['title']} "
                  f"[{j['location']}]", file=sys.stderr)
        notify.dispatch(matched_new)

    if not resend:
        state.save(st)  # persist (keeps the committed state fresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
