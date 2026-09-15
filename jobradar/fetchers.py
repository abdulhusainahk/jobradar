"""Fetch open roles from public job APIs.

Two families:
  * ATS boards — Greenhouse / Lever / Ashby (token-based).
  * Big-tech portals — Amazon, Microsoft, Workday (Salesforce/Adobe/...).

Every fetcher returns normalized dicts:
    {id, title, company, location, url, posted_ts}
`posted_ts` is epoch seconds (0.0 if unknown) — used to sort newest-first.

Page failures retain already fetched roles and are recorded in FETCH_ERRORS;
one bad company never breaks the run.
"""
from __future__ import annotations
import hashlib
import html as _htmllib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests

TIMEOUT = 25
HEADERS = {"User-Agent": "Mozilla/5.0 (JobRadar; +github.com/abdulhusainahk/jobradar)"}

# Main clears this list at the start of a polling run.
FETCH_ERRORS: list[str] = []

# Default role queries for portals that need a search term.
DEFAULT_QUERIES = [
    "devops", "site reliability", "platform engineer",
    "infrastructure engineer", "systems development engineer", "cloud engineer",
]


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _error(c: dict, context: str, error: object) -> None:
    detail = re.sub(r"https?://\S+", "<url>", str(error))
    message = f"{context}: {detail}"
    FETCH_ERRORS.append(f"{c['name']}: {message}")
    _log(f"[err]  {c['name']}: {message}")


def _locations(values) -> str:
    """Keep every supplied location, in source order, without duplicates."""
    return "; ".join(dict.fromkeys(v.strip() for v in values if v and v.strip()))


def _pages(c: dict, context: str, fetch, identity):
    """Walk an offset API; fetch returns (rows, total or None, next offset).

    Progress is local to this search, not the merged company results: overlapping
    queries must still reach their later pages. No page cap truncates coverage.
    """
    offset, seen = 0, set()
    while True:
        try:
            rows, total, next_offset = fetch(offset)
            if not isinstance(rows, list):
                raise ValueError("expected a list of jobs")
            if total is not None:
                total = int(total)
            if not rows:
                if total is not None and offset < total:
                    raise ValueError(f"empty page before advertised total {total}")
                return
            next_offset = int(next_offset)
            if next_offset <= offset:
                raise ValueError("pagination offset made no progress")
            keys = {str(identity(row)) for row in rows}
            if not keys - seen:
                raise ValueError("pagination made no progress (repeated jobs)")
            seen.update(keys)
        except Exception as e:  # keep earlier pages and continue other searches
            _error(c, f"{context}, offset {offset}", e)
            return
        yield rows
        offset = next_offset
        if total is not None and offset >= total:
            return


def _get(url: str, headers: dict | None = None):
    for attempt in range(3):
        response = requests.get(url, headers=headers or HEADERS, timeout=TIMEOUT)
        try:
            if response.status_code == 429 and attempt < 2:
                delay = 30.0 * (2 ** attempt)
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = float(retry_after)
                except (ValueError, TypeError):
                    try:
                        delay = (parsedate_to_datetime(retry_after)
                                 - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        pass
                delay = max(1.0, min(120.0, delay))
                _log(f"[rate-limit] {urlparse(url).hostname}: retrying in {delay:g}s")
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response.json()
        finally:
            response.close()


def _post(url: str, body: dict, headers: dict | None = None):
    r = requests.post(url, headers={**(headers or HEADERS), "Content-Type": "application/json"},
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
    for j in data["jobs"]:
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
        # Lever separates responsibilities/qualifications from the introductory
        # description. Scoring only descriptionPlain omits the actual requirements.
        sections = [j.get("descriptionPlain") or j.get("description") or ""]
        sections.extend(
            (section.get("text") or "") + ":\n" + (section.get("content") or "")
            for section in j.get("lists") or []
        )
        sections.append(j.get("additionalPlain") or j.get("additional") or "")
        jd = "\n".join(_clean_title(re.sub(r"<[^>]+>", " ", text)) for text in sections if text)
        out.append({
            "id": str(j.get("id")),
            "title": (j.get("text") or "").strip(),
            "company": c["name"],
            "location": _locations([cats.get("location")] + (cats.get("allLocations") or [])),
            "url": j.get("hostedUrl", ""),
            "posted_ts": (j.get("createdAt") or 0) / 1000.0,
            "_jd": jd,
        })
    return out


def ashby(c: dict) -> list[dict]:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{c['token']}")
    out = []
    for j in data["jobs"]:
        loc = _locations([j.get("location")] +
                         [s.get("location") for s in j.get("secondaryLocations") or []])
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = (loc + " (Remote)").strip()
        job_url = j.get("jobUrl") or j.get("applyUrl", "")
        jid = j.get("id") or urlparse(job_url).path.rstrip("/").removesuffix("/apply").split("/")[-1]
        out.append({
            "id": str(jid),
            "title": (j.get("title") or "").strip(),
            "company": c["name"],
            "location": loc,
            "url": job_url,
            "posted_ts": _iso_ts(j.get("publishedAt")),
            "_jd": j.get("descriptionPlain") or "",
        })
    return out


def smartrecruiters(c: dict) -> list[dict]:
    base = f"https://api.smartrecruiters.com/v1/companies/{c['token']}/postings"
    seen, out = set(), []

    def page(offset):
        data = _get(f"{base}?limit=100&offset={offset}")
        rows = data["content"]
        return rows, data.get("totalFound"), offset + len(rows)

    for rows in _pages(c, "smartrecruiters", page, lambda j: j["id"]):
        for j in rows:
            jid = str(j["id"])
            if jid in seen:
                continue
            seen.add(jid)
            loc = j.get("location") or {}
            location = loc.get("fullLocation") or ", ".join(
                value for value in (loc.get("city"), loc.get("region"), loc.get("country")) if value)
            if loc.get("remote"):
                location = _locations([location, "Remote"])
            out.append({
                "id": jid, "title": (j.get("name") or "").strip(),
                "company": c["name"], "location": location,
                "country_code": loc.get("country") or "",
                "url": f"https://jobs.smartrecruiters.com/{c['token']}/{jid}",
                "posted_ts": _iso_ts(j.get("releasedDate")),
            })
    return out


# This is the API host used by Navi's published TurboHire career-page bundle.
TURBOHIRE_API = "https://thapi-stage2.azurewebsites.net/api"


def _turbohire_headers(c: dict) -> dict:
    origin = f"https://{c['token']}.turbohire.co"
    headers = {**HEADERS, "Origin": origin, "Referer": origin + "/"}
    token = _get(f"{TURBOHIRE_API}/token/noauth", headers=headers)
    headers["Authorization"] = "Bearer " + token["access_token"]
    return headers


def turbohire(c: dict) -> list[dict]:
    """Public careerpagev2 returns the full Result array; paging is client-side."""
    headers = _turbohire_headers(c)
    org = _get(f"{TURBOHIRE_API}/publicorganizations?"
               + urlencode({"accountName": c["token"]}), headers=headers)
    body = {"SortByV2": {"Key": "PostedDate", "Order": 2},
            "Keyword": "", "Department": "", "CustomFields": {}}
    for field in ("BunitIds", "Experience", "JobTypes", "JobTypeV2", "Locations",
                  "CreatedDate", "Compensation", "Skills", "ClientIds"):
        body[field] = {"Value": None, "FilterType": 0}
    data = _post(f"{TURBOHIRE_API}/careerpagev2/filteredjobs?"
                 + urlencode({"orgId": org["OrgID"], "pageType": 0}), body, headers=headers)
    rows, out = data["Result"], []
    if len(rows) < int(data["Total"]):
        _error(c, "turbohire", "incomplete full-board response")
    for j in rows:
        try:
            locations = json.loads(j.get("Location") or "[]")
            out.append({
                "id": str(j["JobId"]), "title": (j.get("JobTitle") or "").strip(),
                "company": c["name"],
                "location": _locations([loc.get("Address") for loc in locations]),
                "url": f"https://{c['token']}.turbohire.co/job/publicjobs/{j['JobIdObfuscated']}",
                "posted_ts": _iso_ts(j.get("PublishedDate") or j.get("UpdatedDate")),
                "_public_id": j["JobIdObfuscated"],
            })
        except (KeyError, TypeError, ValueError) as e:
            _error(c, "turbohire job", e)
    return out


# =========================== Big-tech portals ===========================
def amazon(c: dict) -> list[dict]:
    """Amazon uses base_query (query is ignored), offset and hits."""
    seen, out = set(), []
    for q in c.get("queries", DEFAULT_QUERIES):
        def page(offset):
            params = urlencode({"result_limit": 100, "sort": "recent",
                                "base_query": q, "offset": offset})
            data = _get(f"https://www.amazon.jobs/en/search.json?{params}")
            rows = data["jobs"]
            return rows, data.get("hits"), offset + len(rows)

        identity = lambda j: j.get("id_icims") or j.get("id") or j["job_path"]
        for rows in _pages(c, f"amazon query {q!r}", page, identity):
            for j in rows:
                jid = str(identity(j))
                if jid in seen:
                    continue
                seen.add(jid)
                jd = " ".join(filter(None, [
                    j.get("description"), j.get("basic_qualifications"),
                    j.get("preferred_qualifications")]))
                out.append({
                    "id": jid, "title": (j.get("title") or "").strip(),
                    "company": c["name"],
                    "location": (j.get("normalized_location") or j.get("location") or "").strip(),
                    "url": urljoin("https://www.amazon.jobs", j.get("job_path") or ""),
                    "posted_ts": _amazon_ts(j.get("posted_date", "")),
                    "_jd": jd,
                })
    return out


# PCSX careers-search API — same shape across Microsoft's Phenom portal and
# Eightfold-hosted portals (Morgan Stanley, etc.). Newest-first via sort_by.


def _pcsx(c: dict, host: str, domain: str) -> list[dict]:
    seen, out = set(), []
    for q in c.get("queries", DEFAULT_QUERIES):
        # Unscoped by default: a country search can omit eligible remote roles.
        for loc in c.get("locations", [""]):
            def page(offset):
                params = urlencode({"domain": domain, "query": q, "location": loc,
                                    "start": offset, "sort_by": "timestamp"})
                data = _get(f"https://{host}/api/pcsx/search?{params}")
                if data.get("status", 200) != 200:
                    raise ValueError(data.get("error") or data["status"])
                payload = data["data"]
                rows = payload["positions"]
                return rows, payload.get("count"), offset + len(rows)

            for rows in _pages(c, f"pcsx {q!r}/{loc}", page, lambda j: j["id"]):
                for j in rows:
                    jid = str(j["id"])
                    if jid in seen:
                        continue
                    seen.add(jid)
                    out.append({
                        "id": jid, "title": (j.get("name") or "").strip(),
                        "company": c["name"],
                        "location": _locations(j.get("locations") or []),
                        "url": urljoin(f"https://{host}", j.get("positionUrl") or ""),
                        "posted_ts": float(j.get("postedTs") or 0),
                    })
    return out


def microsoft(c: dict) -> list[dict]:
    return _pcsx(c, "apply.careers.microsoft.com", "microsoft.com")


def pcsx(c: dict) -> list[dict]:
    """Generic PCSX portal (e.g. Eightfold-hosted). Config needs host + domain."""
    return _pcsx(c, c["host"], c["domain"])


def workday(c: dict) -> list[dict]:
    """Paginate CXS search and resolve multi-location summaries before filtering."""
    host, site = c["host"], c["site"]
    tenant = host.split(".")[0]
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    seen, out = set(), []
    for q in c.get("queries", DEFAULT_QUERIES):
        def page(offset):
            data = _post(f"{base}/jobs", {"appliedFacets": {}, "limit": 20,
                                        "offset": offset, "searchText": q})
            rows = data["jobPostings"]
            return rows, data.get("total"), offset + len(rows)

        for rows in _pages(c, f"workday query {q!r}", page, lambda j: j["externalPath"]):
            for j in rows:
                path = j["externalPath"]
                if path in seen:
                    continue
                seen.add(path)
                bullets = j.get("bulletFields") or []
                job = {
                    "id": str(bullets[0] if bullets else path),
                    "title": (j.get("title") or "").strip(),
                    "company": c["name"],
                    "location": (j.get("locationsText") or "").strip(),
                    "url": f"https://{host}/en-US/{site}{path}",
                    "posted_ts": _workday_ts(j.get("postedOn", "")),
                    "_path": path,
                }
                if re.fullmatch(r"\d+\s+Locations?", job["location"], re.I):
                    try:
                        info = _get(f"{base}{path}")["jobPostingInfo"]
                        location = _locations([info.get("location")] +
                                              (info.get("additionalLocations") or []))
                        if not location:
                            raise ValueError("detail has no concrete locations")
                        job["location"] = location
                        job["_jd"] = " ".join(_clean_title(
                            re.sub(r"<[^>]+>", " ", info.get("jobDescription", ""))).split())
                        job["url"] = info.get("externalUrl") or job["url"]
                        job["posted_ts"] = _iso_ts(info.get("startDate")) or job["posted_ts"]
                    except Exception as e:
                        _error(c, f"workday locations {path}", e)
                out.append(job)
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
                _error(c, f"google {q!r}/{loc}", e)
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
    """Oracle finder pagination is inside finder, not outer collection paging."""
    host, site = c["host"], c.get("site", "CX_1001")
    base = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    seen, out = set(), []
    # Semantic keyword searches are very broad on Oracle. One complete board
    # scan is cheaper and more inclusive than six overlapping near-global scans.
    for q in c.get("queries", [""]):
        def page(offset):
            finder = (f"findReqs;siteNumber={site},sortBy=POSTING_DATES_DESC,"
                      f"limit=100,offset={offset}")
            if q:
                finder += f',keyword="{q}"'
            data = _get(base + "?" + urlencode({
                "onlyData": "true", "expand": "requisitionList.secondaryLocations",
                "finder": finder}))
            items = data["items"]
            if not items:
                raise ValueError("missing Oracle search result envelope")
            result = items[0]
            rows = result["requisitionList"]
            # Oracle's offset window can include unpublished/hidden rows.
            # Advancing by len(rows) repeats the tail when TotalJobsCount is larger.
            next_offset = int(result.get("Offset", offset)) + int(result.get("Limit") or len(rows))
            return rows, result.get("TotalJobsCount"), next_offset

        for rows in _pages(c, f"oracle query {q!r}", page, lambda j: j["Id"]):
            for j in rows:
                jid = str(j["Id"])
                if jid in seen:
                    continue
                seen.add(jid)
                locs = [j.get("PrimaryLocation")] + [
                    s.get("Name") for s in j.get("secondaryLocations") or []]
                out.append({
                    "id": jid, "title": (j.get("Title") or "").strip(),
                    "company": c["name"], "location": _locations(locs),
                    "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}",
                    "posted_ts": _iso_ts(j.get("PostedDate")),
                })
    return out


_ATOM = "{http://www.w3.org/2005/Atom}"


def _rfc822_ts(s: str) -> float:
    try:
        return parsedate_to_datetime(s).timestamp()
    except Exception:
        return 0.0


def _clean_title(t: str) -> str:
    return _htmllib.unescape(re.sub(r"<[^>]+>", "", t or "")).strip()


def _real_url(link: str) -> str:
    # Google Alerts wraps links as .../url?...&url=<real>&...
    if "google.com/url" in (link or ""):
        q = parse_qs(urlparse(link).query)
        if q.get("url"):
            return q["url"][0]
    return link or ""


def rss(c: dict) -> list[dict]:
    """Consume a Google Alerts (or any careers) RSS/Atom feed. Lets custom
    career sites with no API flow into the SAME digest via native alerts.
    Config: {name, ats: rss, url: <feed>, location?: "India"}.
    """
    r = requests.get(c["url"], headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    loc = c.get("location", "India")  # the alert query is region-scoped
    out = []

    def item(gid, title, link, ts):
        return {
            "id": hashlib.md5((gid or link).encode("utf-8")).hexdigest()[:16],
            "title": _clean_title(title),
            "company": c["name"],
            "location": loc,
            "url": _real_url(link),
            "posted_ts": ts,
            "_rss": True,
        }

    entries = root.findall(f".//{_ATOM}entry")
    if entries:  # Atom (Google Alerts)
        for e in entries:
            le = e.find(f"{_ATOM}link")
            out.append(item(
                e.findtext(f"{_ATOM}id"),
                e.findtext(f"{_ATOM}title", default=""),
                le.get("href") if le is not None else "",
                _iso_ts(e.findtext(f"{_ATOM}updated") or e.findtext(f"{_ATOM}published") or ""),
            ))
    else:  # RSS 2.0
        for it in root.findall(".//item"):
            out.append(item(
                it.findtext("guid"),
                it.findtext("title") or "",
                it.findtext("link") or "",
                _rfc822_ts(it.findtext("pubDate") or ""),
            ))
    return out


_FETCHERS = {
    "greenhouse": greenhouse, "lever": lever, "ashby": ashby,
    "smartrecruiters": smartrecruiters, "turbohire": turbohire,
    "amazon": amazon, "microsoft": microsoft, "workday": workday,
    "atlassian": atlassian, "google": google, "oracle": oracle, "pcsx": pcsx,
    "rss": rss,
}


def fetch_company(c: dict) -> list[dict]:
    error_count = len(FETCH_ERRORS)
    fn = _FETCHERS.get(str(c.get("ats", "")).lower())
    if not fn:
        _error(c, "source", f"unknown ats {c.get('ats')!r}")
        return []
    try:
        jobs = fn(c)
    except Exception as e:  # noqa: BLE001 — resilience by design
        _error(c, str(c.get("ats")), e)
        return []
    if len(FETCH_ERRORS) > error_count:
        status = "partial" if jobs else "failed"
        _log(f"[{status}] {c['name']}: {len(jobs)} roles; fetch failures recorded")
    elif jobs:
        _log(f"[ok]   {c['name']}: {len(jobs)} roles")
    else:
        _log(f"[empty] {c['name']}: source returned no roles")
    return jobs
