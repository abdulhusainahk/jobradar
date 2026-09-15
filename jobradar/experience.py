"""Conservatively assess role-level experience, not individual tool tenure.

Explicit overall experience wins over engineering/relevant experience, which
wins over unqualified experience. Bare or tool-specific tenure is ambiguous and
cannot hard-drop a role. Equally authoritative alternatives form an envelope:
only a band wholly outside the candidate's band causes a drop.
"""
from __future__ import annotations

import html
import re

from .filter import _normalize


_NUMBERS = {word: value for value, word in enumerate((
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty"))}
_NUMBER = r"(?:\d{1,2}|" + "|".join(_NUMBERS) + r")"
_EXPRESSION = re.compile(
    rf"(?<![\w.])(?:(?:at\s+least|minimum(?:\s+of)?|min\.?)\s+)?"
    rf"(?P<low>{_NUMBER})\s*(?:\+|(?:[-–—]|to)\s*(?P<high>{_NUMBER})\s*\+?)?"
    r"\s*(?:(?:years?|yrs?)\b|(?=(?:overall|total|relevant|engineering)\b))", re.I)
_BREAK = re.compile(r"[;\n.!?]|\b(?:including|of which)\b", re.I)
_PREFIX_BREAK = re.compile(r"[;,\n.!?]|\b(?:including|of which|and|or)\b", re.I)
_ROLE = (r"(?:(?:software|systems?|platform|cloud|infrastructure|reliability|devops)\s+)?engineering"
         r"|software development|systems? administration|site reliability|devops|sre")
_MODIFIER = r"(?:relevant|professional|industry|commercial|hands on|work)"
_OPTIONAL = re.compile(r"\b(?:preferred|desirable|nice to have|bonus|optional)\b")


def _number(value: str) -> int:
    return int(value) if value.isdigit() else _NUMBERS[value.lower()]


def _scope(before: str, after: str) -> int:
    """Return the authority of this expression, or zero for ambiguous/tool tenure."""
    before, after = _normalize(before), _normalize(after)
    if _OPTIONAL.search(before) or _OPTIONAL.search(after):
        return 0
    qualifier = re.match(
        rf"^(?:of\s+)?(?:(?:overall|total|{_MODIFIER})\s+)*"
        r"(?:experience\s+)?(?:in|with|using|on)\s+(.*)", after)
    if qualifier and not re.match(rf"^(?:{_ROLE})\b", qualifier.group(1)):
        return 0
    # An explicit label before the number applies even to shorthand "12+ overall".
    if re.search(r"\b(?:overall|total)(?:\s+(?:work|professional))?(?:\s+experience)?$", before):
        return 3
    if re.match(r"^(?:of\s+)?(?:overall|total)(?:$|\s+(?:experience|work|professional|engineering)\b)", after):
        return 3
    if re.search(rf"\b(?:{_ROLE}|{_MODIFIER})(?:\s+experience)?$", before):
        return 2
    if re.match(rf"^(?:(?:of|in)\s+)?(?:{_MODIFIER}\s+)*(?:{_ROLE})\b", after):
        return 2
    # "experience in engineering" is role-level; "experience in Terraform" is not.
    generic = re.match(rf"^(?:of\s+)?(?P<modifier>(?:{_MODIFIER}\s+)*)experience\b(?P<rest>.*)", after)
    if generic:
        rest = generic.group("rest").strip()
        if re.match(r"^(?:in|with|using|on)\b", rest):
            if not re.match(rf"^(?:in|with)\s+(?:{_ROLE})\b", rest):
                return 0
            return 2
        # Arbitrary words after "experience" may still name a tool or discipline.
        if rest and not re.match(r"^(?:required|needed|is required|is needed|and|or|as)\b", rest):
            return 0
        if rest.startswith("as ") and not re.match(
                rf"^as\s+(?:an?\s+)?(?:(?:senior|staff|lead)\s+)?(?:{_ROLE}|"
                r"(?:software|platform|cloud|infrastructure) engineer)\b", rest):
            return 0
        return 2 if generic.group("modifier") else 1
    if before in {"experience", "required experience", "minimum experience", "experience required"}:
        # A label cannot turn the following tool qualifier into overall experience.
        return 1 if not after or after in {"required", "needed"} else 0
    return 0


def required_years(jd: str):
    """Return the conservative role-level (low, high|None) band, or None."""
    if not jd:
        return None
    text = html.unescape(jd)
    text = re.sub(r"</?(?:p|div|li|ul|ol|br|h[1-6])\b[^>]*>", ";", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text).replace("\u00a0", " ")
    expressions = list(_EXPRESSION.finditer(text))
    scoped = []
    for index, match in enumerate(expressions):
        start = expressions[index - 1].end() if index else 0
        end = expressions[index + 1].start() if index + 1 < len(expressions) else len(text)
        prefixes = _PREFIX_BREAK.split(text[start:match.start()])
        # Without a separator, this is the previous expression's suffix, not
        # an independent label for the next tenure.
        before = prefixes[-1] if not index or len(prefixes) > 1 else ""
        after = _BREAK.split(text[match.end():end])[0].strip(" ,:'’\"()")
        scope = _scope(before, after)
        if not scope:
            continue
        low = _number(match.group("low"))
        high = _number(match.group("high")) if match.group("high") else None
        if high is not None:
            low, high = min(low, high), max(low, high)
        scoped.append((scope, low, high))
    if not scoped:
        return None
    authority = max(scope for scope, _, _ in scoped)
    bands = [(low, high) for scope, low, high in scoped if scope == authority]
    low = min(low for low, _ in bands)
    high = None if any(high is None for _, high in bands) else max(high for _, high in bands)
    return low, high


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
