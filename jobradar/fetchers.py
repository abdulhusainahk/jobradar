"""Fetch open roles from public job APIs.

Two families:
  * ATS boards — Greenhouse / Lever / Ashby (token-based).
  * Big-tech portals — Amazon, Microsoft, Workday (Salesforce/Adobe/...).

Every fetcher returns normalized dicts:
    {id, title, company, location, url, posted_ts}
`posted_ts` is epoch seconds (0.0 if unknown) — used to sort newest-first.

Each fetcher takes the whole company config dict and fails safe to [] on any
error (logged), so one bad company never breaks the run.
"""
from __future__ import annotations
import re
import sys
from datetime import datetime, timezone

import requests

TIMEOUT = 25
HEADERS = {"User-Agent": "Mozilla/5.0 (JobRadar; +github.com/abdulhusainahk/jobradar)"}

# Default role queries for portals that need a search term.
DEFAULT_QUERIES = [
    "devops", "site reliability", "platform engineer",
    "infrastructure engineer", "systems development engineer", "cloud engineer",
]


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _get(url: str):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _post(url: str, body: dict):
    r = requests.post(url, headers={**HEADERS, "Content-Type": "application/json"},
                      json=body, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# --------------------------- date helpers ---------------------------
def _iso_ts(s: str) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _amazon_ts(s: str) -> float:
    try:
        return (datetime.strptime(" ".join(s.split()), "%B %d, %Y")
                .replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return 0.0


def _atlassian_ts(s: str) -> float:
    try:
        return datetime.strptime(s, "%Y-%m-%d %I:%M %p").timestamp()
    except Exception:
        return 0.0


def _workday_ts(s: str) -> float:
    now = datetime.now(timezone.utc).timestamp()
    s = (s or "").lower()
    if "today" in s:
        return now
    if "yesterday" in s:
        return now - 86400
    m = re.search(r"(\d+)\+?\s*day", s)
    if m:
        return now - int(m.group(1)) * 86400
    m = re.search(r"(\d+)\+?\s*month", s)
    if m:
        return now - int(m.group(1)) * 2592000
    return 0.0


# =========================== ATS boards ===========================
def greenhouse(c: dict) -> list[dict]:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{c['token']}/jobs")
    out = []
    for j in data.get("jobs", []):
        out.append({
            "id": str(j.get("id")),
            "title": (j.get("title") or "").strip(),
            "company": c["name"],
            "location": ((j.get("location") or {}).get("name") or "").strip(),
            "url": j.get("absolute_url", ""),
            "posted_ts": _iso_ts(j.get("updated_at")),
        })
    return out


def lever(c: dict) -> list[dict]:
    data = _get(f"https://api.lever.co/v0/postings/{c['token']}?mode=json")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        out.append({
            "id": str(j.get("id")),
            "title": (j.get("text") or "").strip(),
            "company": c["name"],
            "location": (cats.get("location") or "").strip(),
            "url": j.get("hostedUrl", ""),
            "posted_ts": (j.get("createdAt") or 0) / 1000.0,
            "_jd": (j.get("descriptionPlain") or "")[:6000],  # JD is inline on Lever
        })
    return out


def ashby(c: dict) -> list[dict]:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{c['token']}")
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or "").strip()
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = (loc + " (Remote)").strip()
        out.append({
            "id": str(j.get("id")),
            "title": (j.get("title") or "").strip(),
            "company": c["name"],
            "location": loc,
            "url": j.get("jobUrl") or j.get("applyUrl", ""),
            "posted_ts": _iso_ts(j.get("publishedAt")),
            "_jd": (j.get("descriptionPlain") or "")[:6000],
        })
    return out


# =========================== Big-tech portals ===========================
def amazon(c: dict) -> list[dict]:
    """amazon.jobs public search.json — sorted by recency, merged across queries."""
    seen, out = set(), []
    for q in c.get("queries", DEFAULT_QUERIES):
        url = ("https://www.amazon.jobs/en/search.json?"
               f"result_limit=100&sort=recent&query={requests.utils.quote(q)}")
        try:
            data = _get(url)
        except Exception as e:  # noqa: BLE001
            _log(f"[amazon] query '{q}': {e}")
            continue
        for j in data.get("jobs", []):
            jid = str(j.get("id_icims") or j.get("id") or j.get("job_path"))
            if jid in seen:
                continue
            seen.add(jid)
            jd = " ".join(filter(None, [
                j.get("description", ""), j.get("basic_qualifications", ""),
                j.get("preferred_qualifications", "")]))
            out.append({
                "id": jid,
                "title": (j.get("title") or "").strip(),
                "company": c["name"],
                "location": (j.get("normalized_location") or j.get("location") or "").strip(),
                "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
                "posted_ts": _amazon_ts(j.get("posted_date", "")),
                "_jd": jd[:6000],  # Amazon JD is inline in search.json
            })
    return out


# PCSX careers-search API — same shape across Microsoft's Phenom portal and
# Eightfold-hosted portals (Morgan Stanley, etc.). Newest-first via sort_by.
PCSX_QUERIES = ["devops", "site reliability", "platform engineer"]
PCSX_LOCATIONS = ["India", "United Arab Emirates", "Ireland",
                  "Germany", "Netherlands", "United Kingdom"]


def _pcsx(c: dict, host: str, domain: str) -> list[dict]:
    seen, out = set(), []
    for q in c.get("queries", PCSX_QUERIES):
        for loc in c.get("locations", PCSX_LOCATIONS):
            url = (f"https://{host}/api/pcsx/search?domain={domain}"
                   f"&query={requests.utils.quote(q)}&location={requests.utils.quote(loc)}"
                   "&start=0&sort_by=timestamp")
            try:
                data = _get(url)
            except Exception as e:  # noqa: BLE001
                _log(f"[pcsx:{host}] '{q}'/{loc}: {e}")
                continue
            for j in (data.get("data") or {}).get("positions", []):
                jid = str(j.get("id"))
                if jid in seen:
                    continue
                seen.add(jid)
                locs = j.get("locations") or []
                out.append({
                    "id": jid,
                    "title": (j.get("name") or "").strip(),
                    "company": c["name"],
                    "location": "; ".join(locs[:2]),
                    "url": f"https://{host}" + (j.get("positionUrl") or ""),
                    "posted_ts": float(j.get("postedTs") or 0),
                })
    return out


def microsoft(c: dict) -> list[dict]:
    return _pcsx(c, "apply.careers.microsoft.com", "microsoft.com")


def pcsx(c: dict) -> list[dict]:
    """Generic PCSX portal (e.g. Eightfold-hosted). Config needs host + domain."""
    return _pcsx(c, c["host"], c["domain"])


def workday(c: dict) -> list[dict]:
    """Generic Workday CXS jobs endpoint. Config needs host + site.

    e.g. host: salesforce.wd12.myworkdayjobs.com, site: External_Career_Site
    """
    host, site = c["host"], c["site"]
    tenant = host.split(".")[0]
    endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    seen, out = set(), []
    for q in c.get("queries", DEFAULT_QUERIES):
        try:
            data = _post(endpoint, {"appliedFacets": {}, "limit": 20,
                                    "offset": 0, "searchText": q})
        except Exception as e:  # noqa: BLE001
            _log(f"[workday:{tenant}] query '{q}': {e}")
            continue
        for j in data.get("jobPostings", []):
            path = j.get("externalPath", "")
            if not path or path in seen:
                continue
            seen.add(path)
            bullets = j.get("bulletFields") or []
            out.append({
                "id": bullets[0] if bullets else path,
                "title": (j.get("title") or "").strip(),
                "company": c["name"],
                "location": (j.get("locationsText") or "").strip(),
                "url": f"https://{host}/en-US/{site}{path}",
                "posted_ts": _workday_ts(j.get("postedOn", "")),
                "_path": path,  # for per-job JD fetch
            })
    return out


def atlassian(c: dict) -> list[dict]:
    """Atlassian publishes all jobs as a single JSON array (iCIMS-backed)."""
    data = _get("https://www.atlassian.com/endpoint/careers/listings")
    out = []
    for j in data:
        pjp = j.get("portalJobPost") or {}
        out.append({
            "id": str(j.get("id")),
            "title": (j.get("title") or "").strip(),
            "company": c["name"],
            "location": "; ".join((j.get("locations") or [])[:2]),
            "url": pjp.get("portalUrl", ""),
            "posted_ts": _atlassian_ts(pjp.get("updatedDate", "")),
        })
    return out


# Google Careers is server-side rendered (no clean JSON API). Best-effort:
# parse job links from the results HTML; results are server-side location-
# filtered, so we trust the location we queried. Fails safe to [] if Google
# ever serves a JS shell / blocks the request.
import re as _re  # noqa: E402

# Google titles infra roles as "Site Reliability Engineer" almost exclusively,
# so 2 fuzzy queries cover it — keeps this heavy (SSR) fetcher's runtime down.
GOOGLE_QUERIES = ["site reliability", "platform engineer"]
GOOGLE_LOCATIONS = ["India", "United Arab Emirates", "Ireland",
                    "Germany", "Netherlands", "United Kingdom"]
_G_LINK = _re.compile(r'href="jobs/results/(\d+)-([a-z0-9-]+)\?')


def google(c: dict) -> list[dict]:
    base = "https://www.google.com/about/careers/applications/jobs/results"
    seen, out = set(), []
    for q in c.get("queries", GOOGLE_QUERIES):
        for loc in c.get("locations", GOOGLE_LOCATIONS):
            url = f"{base}?q={requests.utils.quote(q)}&location={requests.utils.quote(loc)}&sort_by=date"
            try:
                r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
                r.raise_for_status()
                html = r.text
            except Exception as e:  # noqa: BLE001
                _log(f"[google] '{q}'/{loc}: {e}")
                continue
            for m in _G_LINK.finditer(html):
                jid, slug = m.group(1), m.group(2)
                if jid in seen:
                    continue
                seen.add(jid)
                out.append({
                    "id": jid,
                    "title": slug.replace("-", " ").title(),
                    "company": c["name"],
                    "location": loc,  # results are server-filtered to this region
                    "url": f"{base}/{jid}-{slug}",
                    "posted_ts": 0.0,  # no per-job timestamp in HTML
                })
    return out


def oracle(c: dict) -> list[dict]:
    """Oracle Recruiting Cloud (fa.oraclecloud.com) — used by many finance GCCs
    (JP Morgan, etc.). Config needs host + site (siteNumber, default CX_1001).
    """
    host = c["host"]
    site = c.get("site", "CX_1001")
    base = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    seen, out = set(), []
    for q in c.get("queries", DEFAULT_QUERIES):
        kw = requests.utils.quote(f'"{q}"')
        url = (f"{base}?onlyData=true&expand=requisitionList.secondaryLocations"
               f"&finder=findReqs;siteNumber={site},keyword={kw},"
               f"sortBy=POSTING_DATES_DESC,limit=50")
        try:
            data = _get(url)
        except Exception as e:  # noqa: BLE001
            _log(f"[oracle:{host}] '{q}': {e}")
            continue
        items = data.get("items") or []
        reqs = items[0].get("requisitionList", []) if items else []
        for j in reqs:
            jid = str(j.get("Id"))
            if jid in seen:
                continue
            seen.add(jid)
            locs = [j.get("PrimaryLocation", "")] + \
                   [s.get("Name", "") for s in (j.get("secondaryLocations") or [])]
            out.append({
                "id": jid,
                "title": (j.get("Title") or "").strip(),
                "company": c["name"],
                "location": "; ".join([l for l in locs if l][:3]),
                "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}",
                "posted_ts": _iso_ts(j.get("PostedDate")),
            })
    return out


_FETCHERS = {
    "greenhouse": greenhouse, "lever": lever, "ashby": ashby,
    "amazon": amazon, "microsoft": microsoft, "workday": workday,
    "atlassian": atlassian, "google": google, "oracle": oracle, "pcsx": pcsx,
}


def fetch_company(c: dict) -> list[dict]:
    fn = _FETCHERS.get(str(c.get("ats", "")).lower())
    if not fn:
        _log(f"[skip] {c.get('name')}: unknown ats '{c.get('ats')}'")
        return []
    try:
        jobs = fn(c)
        _log(f"[ok]   {c['name']}: {len(jobs)} roles")
        return jobs
    except Exception as e:  # noqa: BLE001 — resilience by design
        _log(f"[err]  {c['name']} ({c.get('ats')}): {e}")
        return []
