"""Deterministic matching: role + location + seniority tag + exclusions."""
from __future__ import annotations

import html
import re
import unicodedata


def _normalize(text: str) -> str:
    """Normalize words and separators without matching inside another word."""
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold()))


def _contains_phrase(text: str, phrase: str) -> bool:
    """Match a phrase in already-normalized text, including punctuation variants."""
    phrase = _normalize(phrase)
    return bool(phrase) and f" {phrase} " in f" {text} "


def role_matches(title: str, m: dict) -> bool:
    t = _normalize(title)
    return any(_contains_phrase(t, k) for k in m.get("role_keywords", []))


# These are disambiguators for the configured destinations, not a city geocoder.
# Explicit countries beat city names (notably Dublin, California and London, ON).
_COUNTRIES = {
    "india": ("india", "in", "ind"),
    "uae": ("united arab emirates", "uae", "u a e", "ae"),
    "europe": ("ireland", "ie", "irl", "germany", "de", "deu",
               "netherlands", "nl", "nld", "united kingdom", "uk", "u k", "gb", "gbr"),
}
_OUTSIDE = ("united states", "united states of america", "us", "u s", "usa", "u s a",
            "canada", "ca", "australia", "au", "new zealand", "nz", "singapore", "sg",
            "california", "indiana", "ohio", "ontario", "texas")
_LOCAL_QUALIFIERS = {
    "india": ("maharashtra", "karnataka", "telangana", "tamil nadu", "haryana",
              "uttar pradesh", "delhi", "ncr"),
    "uae": (),
    "europe": ("county dublin", "leinster", "england", "north holland", "bavaria"),
}
_REMOTE_REGIONS = {
    "india": {"india"}, "apac": {"india"}, "asia": {"india", "uae"},
    "europe": {"europe"}, "emea": {"europe", "uae"},
    "global": {"india", "uae", "europe"},
    "worldwide": {"india", "uae", "europe"},
    "anywhere": {"india", "uae", "europe"},
}
_LOCATION_SPLIT = re.compile(r"[;|/\n]+|\s+(?:or|and|&)\s+", re.I)


def _country_regions(country: str) -> set[str]:
    """Structured country values are authoritative, including unknown countries."""
    country = _normalize(country)
    return {region for region, names in _COUNTRIES.items()
            if country in names}


def _location_options(location: str, m: dict):
    """Yield known region sets, or None when an alternative needs JD review."""
    for part in _LOCATION_SPLIT.split(location or ""):
        loc = _normalize(part)
        if not loc:
            yield None
            continue
        fields = {_normalize(field) for field in part.split(",")}
        outside = any(_contains_phrase(loc, name) for name in _OUTSIDE)
        countries = {region for region, names in _COUNTRIES.items()
                     if any((name != "in" and not outside and name in fields
                             if len(name) <= 3 else _contains_phrase(loc, name))
                            for name in names)}
        if countries or outside:
            yield countries
            continue
        regions: set[str] = set()
        if _contains_phrase(loc, "remote"):
            for keyword in m.get("remote_region_keywords", []):
                if _contains_phrase(loc, keyword):
                    scope = _normalize(keyword)
                    if scope in {"global", "worldwide", "anywhere"}:
                        regions.update(m.get("regions_enabled", list(m.get("locations") or {})))
                    else:
                        regions.update(_REMOTE_REGIONS.get(scope, set()))
        for region, keywords in (m.get("locations") or {}).items():
            remainder = f" {loc} "
            city_hit = False
            for keyword in sorted(keywords, key=len, reverse=True):
                phrase = _normalize(keyword)
                if phrase and f" {phrase} " in remainder:
                    city_hit = True
                    remainder = remainder.replace(f" {phrase} ", " ")
            if not city_hit:
                continue
            for qualifier in (*_LOCAL_QUALIFIERS.get(region, ()), "remote", "hybrid", "on site", "onsite"):
                remainder = remainder.replace(f" {_normalize(qualifier)} ", " ")
            if not remainder.strip():
                regions.add(region)
        yield regions or None


def _location_regions(location: str, m: dict) -> set[str]:
    return {region for option in _location_options(location, m) if option for region in option}


def location_matches(location: str, m: dict) -> bool:
    locs = m.get("locations") or {}
    enabled = set(m.get("regions_enabled", list(locs)))
    return bool(_location_regions(location, m) & enabled)


def location_is_india(location: str, m: dict, country: str | None = None) -> bool:
    """True for a physical India location, not merely global/APAC remote scope."""
    if country:
        return "india" in _country_regions(country)
    physical = dict(m, remote_region_keywords=[])
    return "india" in _location_regions(location, physical)


def location_status(job: dict, m: dict) -> str:
    """Distinguish an explicit exclusion from an unresolved job location."""
    enabled = set(m.get("regions_enabled", list(m.get("locations") or {})))
    if not enabled:
        return "outside"
    entries = job.get("locations") or [job]
    if job.get("locations") and any(job.get(key) for key in ("country_code", "countryCode", "country")):
        entries = [job, *entries]
    unknown = False
    for entry in entries:
        if isinstance(entry, str):
            options = _location_options(entry, m)
        elif isinstance(entry, dict):
            country = entry.get("country_code") or entry.get("countryCode") or entry.get("country")
            options = ([_country_regions(country)] if country else
                       _location_options(entry.get("location") or entry.get("city") or "", m))
        else:
            unknown = True
            continue
        for regions in options:
            if regions is None:
                unknown = True
            elif regions & enabled:
                return "allowed"
    return "unknown" if unknown else "outside"


def is_excluded(job: dict, m: dict) -> bool:
    # Keep fields separate: a phrase must not straddle title/location boundaries.
    fields = [_normalize(job.get(key)) for key in ("title", "location")]
    if any(_contains_phrase(text, k) for text in fields for k in m.get("exclude_keywords", [])):
        return True
    # VP/Associate: excluded everywhere except finance companies (IC levels there).
    if not job.get("_finance"):
        if any(_contains_phrase(text, k) for text in fields for k in m.get("exclude_unless_finance", [])):
            return True
    comp = _normalize(job.get("company"))
    return any(_contains_phrase(comp, k) for k in m.get("exclude_companies", []))


def seniority_tag(title: str, m: dict) -> str:
    t = _normalize(title)
    hits = [k for k in m.get("seniority_keywords", []) if _contains_phrase(t, k)]
    return hits[0].title() if hits else ""


def passes(job: dict, m: dict) -> bool:
    return (
        not is_excluded(job, m)
        and role_matches(job.get("title", ""), m)
        and location_status(job, m) == "allowed"
    )


def is_candidate(job: dict, m: dict) -> bool:
    """Cheap hard constraints; uncertain locations remain eligible for AI review."""
    return (
        not is_excluded(job, m)
        and role_matches(job.get("title", ""), m)
        and location_status(job, m) != "outside"
    )
