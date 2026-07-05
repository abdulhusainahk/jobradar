"""Orchestrator: fetch -> filter -> dedup -> (AI score) -> notify -> persist."""
from __future__ import annotations
import os
import sys

import yaml

from . import fetchers, filter as jf, notify, score, state


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run() -> int:
    cfg = load_config(os.environ.get("JOBRADAR_CONFIG", "config.yaml"))
    m = cfg.get("match", {})
    companies = cfg.get("companies", [])
    st = state.load()
    # First ever run: record what's already open as a baseline and DON'T alert,
    # so you only get pinged for roles posted after JobRadar goes live.
    baseline = not st["seen"]

    tier_by_company = {c["name"]: c.get("tier", "") for c in companies}

    matched_new: list[dict] = []
    total_seen = 0
    for c in companies:
        for job in fetchers.fetch_company(c):
            total_seen += 1
            if not jf.passes(job, m):
                continue
            if not state.is_new(st, job):
                continue
            job["tier"] = tier_by_company.get(job["company"], "")
            job["seniority"] = jf.seniority_tag(job["title"], m)
            matched_new.append(job)
            state.mark(st, job)  # mark so we never re-alert, even if notify fails

    print(f"\nScanned {total_seen} roles across {len(companies)} companies; "
          f"{len(matched_new)} match(es).", file=sys.stderr)

    if baseline:
        print(f"[baseline] first run — recorded {len(matched_new)} current "
              "matches as seen; no alerts sent. Future new roles will alert.",
              file=sys.stderr)
        state.save(st)
        return 0

    matched_new = score.score_jobs(matched_new)  # no-op unless AI_SCORING=on

    if matched_new:
        for j in matched_new:
            print(f"  → {j['company']}: {j['title']} [{j['location']}]", file=sys.stderr)
        notify.dispatch(matched_new)

    state.save(st)  # persist even with zero matches (keeps the file/commit fresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
