"""Fetch the full job description for a matched role, so we can score how well
it aligns with a DevOps engineer's toolset. Inline JD (Lever/Ashby/Amazon) is
reused; Greenhouse/Microsoft/Workday are fetched per-job (matched roles only,
so volume is tiny). Fails safe to '' — fit scoring falls back to the title.
"""
from __future__ import annotations
import html as _html
import re

import requests

from .fetchers import HEADERS, TIMEOUT, _log


def _strip(htmltext: str) -> str:
    t = _html.unescape(htmltext or "")     # Greenhouse double-encodes HTML
    t = re.sub(r"<[^>]+>", " ", t)
    t = _html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()[:6000]


def enrich_jd(job: dict) -> str:
    """Return JD text for a job, fetching per-source if not already inline."""
    if job.get("_jd"):
        return job["_jd"][:6000]
    c = job.get("_c") or {}
    ats = str(c.get("ats", "")).lower()
    jid = job.get("id")
    try:
        if ats == "greenhouse":
            d = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{c['token']}/jobs/{jid}",
                headers=HEADERS, timeout=TIMEOUT).json()
            return _strip(d.get("content", ""))
        if ats == "microsoft":
            d = requests.get(
                f"https://apply.careers.microsoft.com/api/pcsx/position_details"
                f"?position_id={jid}&domain=microsoft.com&hl=en",
                headers=HEADERS, timeout=TIMEOUT).json()
            return _strip((d.get("data") or {}).get("jobDescription", ""))
        if ats == "workday":
            host, site = c["host"], c["site"]
            tenant = host.split(".")[0]
            d = requests.get(
                f"https://{host}/wday/cxs/{tenant}/{site}{job.get('_path', '')}",
                headers=HEADERS, timeout=TIMEOUT).json()
            return _strip((d.get("jobPostingInfo") or {}).get("jobDescription", ""))
    except Exception as e:  # noqa: BLE001
        _log(f"[jd] {job.get('company')} {jid}: {e}")
    return ""
