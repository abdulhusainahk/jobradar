"""Fetch the full job description for a matched role, so we can score how well
it aligns with a DevOps engineer's toolset. Inline JD (Lever/Ashby/Amazon) is
reused; Greenhouse/Microsoft/Workday are fetched per-job (matched roles only,
so volume is tiny). Fails safe to '' — fit scoring falls back to the title.
"""
from __future__ import annotations
import html as _html
import re

from .fetchers import TURBOHIRE_API, _get, _log, _turbohire_headers


def _strip(htmltext: str) -> str:
    t = _html.unescape(htmltext or "")     # Greenhouse double-encodes HTML
    t = re.sub(r"<[^>]+>", " ", t)
    t = _html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def enrich_jd(job: dict) -> str:
    """Return JD text for a job, fetching per-source if not already inline."""
    if job.get("_jd"):
        return job["_jd"]
    c = job.get("_c") or {}
    ats = str(c.get("ats", "")).lower()
    jid = job.get("id")
    try:
        if ats == "greenhouse":
            d = _get(
                f"https://boards-api.greenhouse.io/v1/boards/{c['token']}/jobs/{jid}")
            return _strip(d.get("content", ""))
        if ats in ("microsoft", "pcsx"):
            host = "apply.careers.microsoft.com" if ats == "microsoft" else c["host"]
            domain = "microsoft.com" if ats == "microsoft" else c["domain"]
            d = _get(
                f"https://{host}/api/pcsx/position_details"
                f"?position_id={jid}&domain={domain}&hl=en")
            return _strip((d.get("data") or {}).get("jobDescription", ""))
        if ats == "oracle":
            host, site = c["host"], c.get("site", "CX_1001")
            d = _get(
                f"https://{host}/hcmRestApi/resources/latest/"
                f"recruitingCEJobRequisitionDetails?onlyData=true&expand=all"
                f'&finder=ById;Id="{jid}",siteNumber={site}')
            it = (d.get("items") or [{}])[0]
            return _strip((it.get("ExternalDescriptionStr") or "") + " "
                          + (it.get("ExternalQualificationsStr") or ""))
        if ats == "workday":
            host, site = c["host"], c["site"]
            tenant = host.split(".")[0]
            d = _get(
                f"https://{host}/wday/cxs/{tenant}/{site}{job.get('_path', '')}")
            return _strip((d.get("jobPostingInfo") or {}).get("jobDescription", ""))
        if ats == "smartrecruiters":
            d = _get(
                f"https://api.smartrecruiters.com/v1/companies/{c['token']}/postings/{jid}")
            sections = (d.get("jobAd") or {}).get("sections") or {}
            return _strip(" ".join(
                (sections.get(key) or {}).get("text") or ""
                for key in ("jobDescription", "qualifications", "additionalInformation")))
        if ats == "turbohire":
            public_id = job.get("_public_id") or job["url"].rstrip("/").split("/")[-1]
            d = _get(f"{TURBOHIRE_API}/publicjobs?jobId={public_id}&fieldVisibility=0",
                     headers=_turbohire_headers(c))
            return _strip(" ".join(
                d.get(key + "V2") or d.get(key) or ""
                for key in ("JobDescription", "RolesAndResponsibilities", "Eligibility")))
    except Exception as e:  # noqa: BLE001
        _log(f"[jd] {job.get('company')} {jid}: {e}")
    return ""
