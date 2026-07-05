"""Fetch open roles from public ATS JSON APIs (Greenhouse / Lever / Ashby).

Each fetcher returns a list of normalized dicts:
    {id, title, company, location, url, updated_at}
Network/parse errors are swallowed and logged so one bad company can't break
the whole run.
"""
from __future__ import annotations
import sys
import requests

TIMEOUT = 25
HEADERS = {"User-Agent": "JobRadar/0.1 (+github.com/abdulhusainahk/jobradar)"}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _get(url: str):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def greenhouse(company: str, token: str) -> list[dict]:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    out = []
    for j in data.get("jobs", []):
        out.append({
            "id": str(j.get("id")),
            "title": j.get("title", "").strip(),
            "company": company,
            "location": (j.get("location") or {}).get("name", "").strip(),
            "url": j.get("absolute_url", ""),
            "updated_at": j.get("updated_at", ""),
        })
    return out


def lever(company: str, token: str) -> list[dict]:
    data = _get(f"https://api.lever.co/v0/postings/{token}?mode=json")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        out.append({
            "id": str(j.get("id")),
            "title": j.get("text", "").strip(),
            "company": company,
            "location": (cats.get("location") or "").strip(),
            "url": j.get("hostedUrl", ""),
            "updated_at": str(j.get("createdAt", "")),
        })
    return out


def ashby(company: str, token: str) -> list[dict]:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or "").strip()
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = (loc + " (Remote)").strip()
        out.append({
            "id": str(j.get("id")),
            "title": j.get("title", "").strip(),
            "company": company,
            "location": loc,
            "url": j.get("jobUrl") or j.get("applyUrl", ""),
            "updated_at": j.get("publishedAt", ""),
        })
    return out


_FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}


def fetch_company(c: dict) -> list[dict]:
    """Dispatch on c['ats']; return [] on any error (logged)."""
    fn = _FETCHERS.get(c.get("ats", "").lower())
    if not fn:
        _log(f"[skip] {c.get('name')}: unknown ats '{c.get('ats')}'")
        return []
    try:
        jobs = fn(c["name"], c["token"])
        _log(f"[ok]   {c['name']}: {len(jobs)} roles")
        return jobs
    except Exception as e:  # noqa: BLE001 — resilience by design
        _log(f"[err]  {c['name']} ({c['ats']}:{c['token']}): {e}")
        return []
