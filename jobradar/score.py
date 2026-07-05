"""Optional AI scoring layer (feature-flagged, OFF by default).

Enable by setting BOTH:
    AI_SCORING=on
    ANTHROPIC_API_KEY=sk-ant-...   (a dedicated key; Haiku costs ~pennies/month)

When off, jobs pass through unchanged and alerts are keyword-only.
When on, each NEW job gets a 0-100 fit score vs your profile + a one-line note,
using Claude Haiku (cheap, fast). Runs only on new postings, so cost is tiny.
"""
from __future__ import annotations
import json
import os
import sys

# Short profile the model scores against. Edit to taste.
PROFILE = (
    "Abdulhussain Kanchwala — Senior DevOps & Platform Engineer, 5+ yrs. "
    "AWS + Azure, Terraform/Terragrunt, Kubernetes/Helm, CI/CD (GitHub Actions/"
    "Jenkins), Prometheus/Grafana/ELK, plus AI/LLMOps (MCP servers). "
    "Targeting senior DevOps/SRE/Platform roles at high-end product companies, "
    "Mumbai first then Bangalore or remote-India. Not interested in junior, "
    "pure-support, or non-infra roles."
)

MODEL = os.environ.get("JOBRADAR_MODEL", "claude-haiku-4-5")
MIN_SCORE = int(os.environ.get("AI_MIN_SCORE", "0"))  # drop below this if >0


def _enabled() -> bool:
    return (os.environ.get("AI_SCORING", "").lower() in ("1", "true", "on", "yes")
            and bool(os.environ.get("ANTHROPIC_API_KEY")))


def score_jobs(jobs: list[dict]) -> list[dict]:
    if not _enabled() or not jobs:
        return jobs
    try:
        import anthropic
    except ImportError:
        print("[ai] anthropic not installed; skipping AI scoring", file=sys.stderr)
        return jobs

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    schema = {
        "type": "object",
        "properties": {
            "score": {"type": "integer"},
            "note": {"type": "string"},
        },
        "required": ["score", "note"],
        "additionalProperties": False,
    }
    kept = []
    for job in jobs:
        prompt = (
            f"Candidate profile:\n{PROFILE}\n\n"
            f"Job posting:\nTitle: {job['title']}\nCompany: {job['company']}\n"
            f"Location: {job['location']}\n\n"
            "Rate fit 0-100 for this candidate and give a one-sentence reason "
            "(mention any seniority/comp/location mismatch)."
        )
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=300,
                output_config={"format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": prompt}],
            )
            text = next(b.text for b in resp.content if b.type == "text")
            data = json.loads(text)
            job["ai_score"] = int(data["score"])
            job["ai_note"] = data["note"]
        except Exception as e:  # noqa: BLE001 — never let scoring break alerts
            print(f"[ai] score error for {job['title']}: {e}", file=sys.stderr)
            job["ai_score"] = None
            job["ai_note"] = ""
        if job.get("ai_score") is None or job["ai_score"] >= MIN_SCORE:
            kept.append(job)
    # Note: final ordering is by recency (newest first), applied in main.py.
    # AI_MIN_SCORE only filters here; it does not reorder.
    return kept
