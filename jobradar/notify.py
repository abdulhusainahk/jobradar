"""Alert delivery: Telegram (instant) + email digest (durable record).

All channels are best-effort and independently optional — configure whichever
you have secrets for. Nothing here raises; failures are logged.
"""
from __future__ import annotations
import html
import os
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText

import requests

from .state import key


def _age(ts: float) -> str:
    if not ts:
        return ""
    days = (datetime.now(timezone.utc).timestamp() - ts) / 86400
    if days < 1:
        return " · 🆕 posted today"
    if days < 2:
        return " · posted yesterday"
    return f" · posted {int(days)}d ago"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _job_line_html(job: dict) -> str:
    fit = job.get("fit") or {}
    ftier = fit.get("tier", "")
    fscore = fit.get("score")
    sc = f" · fit {fscore}/100" if fscore is not None else ""
    ai = f" · AI {job['ai_score']}/100" if job.get("ai_score") is not None else ""
    sen = f" · <i>{_esc(job['seniority'])}</i>" if job.get("seniority") else ""
    matched = fit.get("matched") or []
    skills = ("<br>🛠 " + _esc(", ".join(matched))) if matched else ""
    yrs = (f"<br>{_esc(job['exp_note'])}") if job.get("exp_note") else ""
    note = (f"<br>💬 <i>{_esc(job['ai_note'])}</i>") if job.get("ai_note") else ""
    return (
        (f"{_esc(ftier)}<br>" if ftier else "")
        + f"🎯 <b>{_esc(job['title'])}</b>{sc}{ai}{sen}<br>"
        f"🏢 {_esc(job['company'])} <i>({_esc(job.get('tier', ''))})</i>"
        f"{_esc(_age(job.get('posted_ts', 0)))}<br>"
        f"📍 {_esc(job['location'] or 'n/a')}<br>"
        f"🔗 <a href=\"{_esc(job['url'])}\">Apply / view posting</a>"
        + yrs + skills + note
    )


# ---------------- Telegram ----------------
def _tg_send_one(url: str, chat_id: str, text: str) -> bool:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    for attempt in range(4):
        try:
            response = requests.post(url, timeout=20, json=payload)
            if response.status_code == 429:
                if attempt == 3:
                    _log("[telegram] rate limit retries exhausted")
                    return False
                retry_after = (response.json().get("parameters") or {}).get("retry_after", 3)
                delay = max(0, min(60, float(retry_after)))
                _log(f"[telegram] 429; retrying in {delay + 1:g}s")
                time.sleep(delay + 1)
                continue
            if response.status_code != 200:
                _log(f"[telegram] HTTP {response.status_code}")
                return False
            if response.json().get("ok") is not True:
                _log("[telegram] API rejected message")
                return False
            return True
        except Exception as exc:
            # requests exceptions and response bodies may contain the bot URL.
            _log(f"[telegram] delivery failed ({type(exc).__name__})")
            return False
    return False


def _text_size(text: str) -> int:
    """Conservative Telegram bound: UTF-16 units, including markup/entities."""
    return len(text.encode("utf-16-le")) // 2


def _telegram_block(job: dict, limit: int) -> str:
    block = _job_line_html(job).replace("<br>", "\n")
    if _text_size(block) <= limit:
        return block

    # Never slice rendered HTML: that can split a tag, entity, or link. Optional
    # analysis yields to a compact alert built from independently escaped fields.
    def short(value, size=160):
        text = str(value or "")
        return _esc(text if len(text) <= size else text[:size] + "...")

    block = (f"<b>{short(job.get('title'))}</b>\n"
             f"{short(job.get('company'), 80)}\n"
             f"{short(job.get('location'), 80)}")
    link = f'\n<a href="{_esc(job.get("url"))}">Apply / view posting</a>'
    if _text_size(block + link) <= limit:
        block += link
    else:
        block += "\nPosting URL omitted (too long)."
    if _text_size(block) > limit:
        raise ValueError("Telegram header leaves insufficient room for an alert")
    return block


def _chunk(blocks: list[tuple[dict, str]], header: str,
           limit: int = 3800) -> list[tuple[str, list[dict]]]:
    """Pack whole, already bounded blocks, retaining each message's jobs."""
    pages = []
    text = header
    jobs = []
    for job, block in blocks:
        separator = "\n\n" if jobs else ""
        if _text_size(text + separator + block) > limit:
            if not jobs:
                raise ValueError("Telegram block exceeds message limit")
            pages.append((text, jobs))
            text, jobs, separator = header, [], ""
        if _text_size(text + separator + block) > limit:
            raise ValueError("Telegram block exceeds message limit")
        text += separator + block
        jobs.append(job)
    if jobs:
        pages.append((text, jobs))
    return pages


def _split_groups(jobs: list[dict]):
    """(India, International) — each preserving the incoming best-fit-first order."""
    india = [j for j in jobs if j.get("_india")]
    foreign = [j for j in jobs if not j.get("_india")]
    return india, foreign


def configured_channels() -> list[str]:
    channels = []
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        channels.append("telegram")
    if os.environ.get("EMAIL_USER") and os.environ.get("EMAIL_APP_PASSWORD"):
        channels.append("email")
    return channels


def send_telegram(jobs: list[dict]) -> dict[str, bool]:
    outcomes = {key(job): False for job in jobs}
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        _log("[telegram] skipped (no TELEGRAM_BOT_TOKEN/CHAT_ID)")
        return outcomes
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    india, foreign = _split_groups(jobs)
    for label, grp in (("🇮🇳 <b>India</b>", india),
                       ("🌍 <b>International</b>", foreign)):
        if not grp:
            continue
        header = f"{label} openings ({len(grp)}) — best fit first\n\n"
        blocks = []
        for job in grp:
            try:
                blocks.append((job, _telegram_block(job, 3800 - _text_size(header))))
            except Exception as exc:
                _log(f"[telegram] cannot render alert ({type(exc).__name__})")
        for msg, chunk_jobs in _chunk(blocks, header):
            if _tg_send_one(url, chat_id, msg):
                for job in chunk_jobs:
                    outcomes[key(job)] = True
            time.sleep(0.4)  # gentle pacing between chunks
    return outcomes


# ---------------- Email ----------------
def send_email(jobs: list[dict]) -> dict[str, bool]:
    outcomes = {key(job): False for job in jobs}
    user = os.environ.get("EMAIL_USER")
    pw = os.environ.get("EMAIL_APP_PASSWORD")
    to = (os.environ.get("EMAIL_TO") or "").strip() or user
    if not user or not pw:
        _log("[email] skipped (no EMAIL_USER/EMAIL_APP_PASSWORD)")
        return outcomes
    india, foreign = _split_groups(jobs)
    sections = ""
    for label, grp in (("🇮🇳 India openings", india),
                       ("🌍 International openings", foreign)):
        if not grp:
            continue
        rows = "<hr>".join(_job_line_html(j) for j in grp)
        sections += (f"<h3 style='margin-top:20px'>{label} ({len(grp)})"
                     f"</h3>{rows}")
    body = (
        f"<h2>JobRadar — {len(jobs)} new DevOps-fit role"
        f"{'s' if len(jobs) != 1 else ''} (best fit first)</h2>{sections}"
        "<hr><p style='color:#888;font-size:12px'>"
        "🟢 strong / 🟡 moderate / 🔴 monitoring-only. Apply fast + with a "
        "referral. Sent by your JobRadar GitHub Action.</p>"
    )
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = f"🎯 JobRadar: {len(jobs)} new DevOps/SRE role(s)"
    msg["From"] = user
    msg["To"] = to
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls()
            s.login(user, pw)
            refused = s.sendmail(user, [a.strip() for a in to.split(",") if a.strip()],
                                 msg.as_string())
            if refused:
                _log("[email] one or more recipients refused the digest")
                return outcomes
        _log("[email] sent digest")
        return {job_key: True for job_key in outcomes}
    except Exception as exc:
        _log(f"[email] delivery failed ({type(exc).__name__})")
        return outcomes


def dispatch(jobs: list[dict], channels_by_job: dict[str, list[str]] | None = None
             ) -> dict[str, dict[str, bool]]:
    """Return explicit outcomes only for attempted channel/job pairs.

    With no routing map, use all currently configured channels. An explicit map
    sends only the requested pairs, allowing durable retries to skip successes.
    Missing credentials for a requested channel produce False, never success.
    """
    if not jobs:
        return {}
    if channels_by_job is None:
        channels = configured_channels()
        channels_by_job = {key(job): channels for job in jobs}
    outcomes = {}
    requested = dict.fromkeys(channel for job in jobs
                              for channel in channels_by_job.get(key(job), []))
    senders = {"telegram": send_telegram, "email": send_email}
    for channel in requested:
        batch = [job for job in jobs if channel in channels_by_job.get(key(job), [])]
        result = {key(job): False for job in batch}
        try:
            sender = senders.get(channel)
            if sender is None:
                _log("[notify] unsupported delivery channel")
            else:
                sent = sender(batch)
                result = {job_key: sent.get(job_key) is True for job_key in result}
        except Exception as exc:
            _log(f"[notify] delivery failed ({type(exc).__name__})")
        outcomes[channel] = result
    return outcomes
