"""Deterministic DevOps-fit scoring.

Reads the job description and judges how well it aligns with a *DevOps engineer*
profile (Abdulhussain's resume stack) — not just any SRE role. The heuristic:
roles that require real DevOps tools/practices (IaC, CI/CD, Kubernetes,
provisioning, automation) score high; roles that are monitoring/on-call-only
with no automation are demoted as "not worth it".

Free and offline. For deeper semantic judgment, enable the AI layer (score.py).
"""
from __future__ import annotations

# Strong DevOps signals — drawn from the resume (weight each distinct hit).
DEVOPS = {
    "terraform", "terragrunt", "ansible", "kubernetes", "k8s", "helm",
    "docker", "container", "ci/cd", "cicd", "ci cd", "pipeline", "gitops",
    "argocd", "argo cd", "jenkins", "github actions", "azure devops",
    "gitlab ci", "infrastructure as code", "iac", "cloudformation",
    "provisioning", "configuration management", "automation", "eks", "aks",
    "platform engineering", "internal developer platform", "sre tooling",
}
CLOUD = {"aws", "azure", "gcp", "google cloud"}
# Monitoring/ops signals — fine WITH DevOps work, a red flag if that's ALL there is.
MONITORING = {
    "monitoring", "observability", "alerting", "on-call", "on call",
    "incident", "dashboards", "grafana", "prometheus", "datadog", "splunk",
    "nagios", "pagerduty", "elk", "loki",
}

# Pretty display names for the skills we surface in the alert.
_DISPLAY = {
    "ci/cd": "CI/CD", "cicd": "CI/CD", "ci cd": "CI/CD", "k8s": "Kubernetes",
    "iac": "IaC", "aws": "AWS", "azure": "Azure", "gcp": "GCP",
    "aks": "AKS", "eks": "EKS",
}


def _hits(text: str, terms: set[str]) -> list[str]:
    return sorted({t for t in terms if t in text})


def _pretty(terms: list[str]) -> list[str]:
    seen, out = set(), []
    for t in terms:
        d = _DISPLAY.get(t, t.title() if t.islower() else t)
        if d.lower() not in seen:
            seen.add(d.lower())
            out.append(d)
    return out


def devops_fit(job: dict, jd: str) -> dict:
    text = f"{jd or ''} {job.get('title', '')}".lower()
    dv = _hits(text, DEVOPS)
    cl = _hits(text, CLOUD)
    mon = _hits(text, MONITORING)
    s = len(dv)
    has_jd = bool(jd)

    score = min(100, s * 15 + len(cl) * 6)
    if s >= 3:
        tier = "🟢 Strong DevOps fit"
    elif s >= 1:
        tier = "🟡 Moderate fit"
    elif mon and has_jd:
        tier = "🔴 Monitoring-only — likely not worth it"
        score = min(score, 12)
    elif not has_jd:
        tier = "⚪ Unscored (no JD)"
    else:
        tier = "⚪ Unclear fit"

    return {
        "score": score,
        "tier": tier,
        "matched": _pretty(dv + cl),          # DevOps + cloud skills present
        "monitoring_only": (s == 0 and bool(mon) and has_jd),
        "has_jd": has_jd,
    }
