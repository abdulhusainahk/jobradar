"""Parse the years-of-experience a role asks for, and judge it against the
candidate's band so we don't surface roles that are too junior or too senior.

Deliberately conservative: only drops a role when the JD *explicitly* asks for
more years than the ceiling, or gives a range that caps well below the
candidate. Bare mentions ("2+ years with Kubernetes") never trigger a junior
drop — only a stated range does.
"""
from __future__ import annotations
import re

# "3-5 years", "3 to 5 years"
_RANGE = re.compile(r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*years?", re.I)
# "at least 8 years", "minimum of 10 years", "8+ years"
_MIN = re.compile(
    r"(?:at least|minimum(?:\s+of)?|min\.?)\s+(\d{1,2})\s*\+?\s*years?"
    r"|(\d{1,2})\s*\+\s*years?", re.I)


def required_years(jd: str):
    """Return (low, high|None) years required, or None if not stated."""
    if not jd:
        return None
    t = jd.lower()
    m = _RANGE.search(t)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return (min(lo, hi), max(lo, hi))
    m = _MIN.search(t)
    if m:
        return (int(m.group(1) or m.group(2)), None)
    return None


def assess(jd: str, candidate_years: int, max_required: int) -> dict:
    """Judge the role's required experience vs the candidate's band."""
    req = required_years(jd)
    if not req:
        return {"note": "", "drop": False}
    lo, hi = req
    label = f"{lo}–{hi} yrs" if hi else f"{lo}+ yrs"
    if lo > max_required:
        return {"note": f"🎓 needs {label} — above your band", "drop": True}
    if hi is not None and hi <= candidate_years - 2:
        return {"note": f"🎓 {label} — junior for your band", "drop": True}
    return {"note": f"🎓 {label}", "drop": False}
