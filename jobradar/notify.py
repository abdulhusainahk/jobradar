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
    note = (f"<br>💬 <i>{_esc(job['ai_note'])}</i>") if job.get("ai_note") else ""
    return (
        (f"{_esc(ftier)}<br>" if ftier else "")
        + f"🎯 <b>{_esc(job['title'])}</b>{sc}{ai}{sen}<br>"
        f"🏢 {_esc(job['company'])} <i>({_esc(job.get('tier', ''))})</i>"
        f"{_esc(_age(job.get('posted_ts', 0)))}<br>"
        f"📍 {_esc(job['location'] or 'n/a')}<br>"
        f"🔗 <a href=\"{_esc(job['url'])}\">Apply / view posting</a>"
        + skills + note
    )


# ---------------- Telegram ----------------
def _tg_send_one(url: str, chat_id: str, text: str) -> None:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    for _ in range(4):
        try:
            r = requests.post(url, timeout=20, json=payload)
        except Exception as e:  # noqa: BLE001
            _log(f"[telegram] error: {e}")
            return
        if r.status_code == 429:  # honor Telegram's retry_after
            wait = (r.json().get("parameters") or {}).get("retry_after", 3)
            _log(f"[telegram] 429; sleeping {wait}s")
            time.sleep(wait + 1)
            continue
        if r.status_code != 200:
            _log(f"[telegram] {r.status_code}: {r.text[:200]}")
        return


def _chunk(blocks: list[str], header: str, limit: int = 3800) -> list[str]:
    """Pack job blocks into as few Telegram messages as possible (<4096 chars)."""
    pages: list[list[str]] = [[]]
    size = len(header)
    for b in blocks:
        if pages[-1] and size + len(b) + 2 > limit:
            pages.append([])
            size = 0
        pages[-1].append(b)
        size += len(b) + 2
    out = []
    for i, p in enumerate(pages):
        h = header if i == 0 else f"🎯 <b>(continued {i + 1}/{len(pages)})</b>\n\n"
        out.append(h + "\n\n".join(p))
    return out


def send_telegram(jobs: list[dict]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        _log("[telegram] skipped (no TELEGRAM_BOT_TOKEN/CHAT_ID)")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    header = (f"🎯 <b>{len(jobs)} new DevOps-fit role"
              f"{'s' if len(jobs) != 1 else ''}</b> — best fit first\n\n")
    # One combined digest (chunked only if it exceeds Telegram's size limit).
    blocks = [_job_line_html(j).replace("<br>", "\n") for j in jobs]
    for msg in _chunk(blocks, header):
        _tg_send_one(url, chat_id, msg)
        time.sleep(0.4)  # gentle pacing between chunks


# ---------------- Email ----------------
def send_email(jobs: list[dict]) -> None:
    user = os.environ.get("EMAIL_USER")
    pw = os.environ.get("EMAIL_APP_PASSWORD")
    to = os.environ.get("EMAIL_TO", user)
    if not user or not pw:
        _log("[email] skipped (no EMAIL_USER/EMAIL_APP_PASSWORD)")
        return
    rows = "<hr>".join(_job_line_html(j) for j in jobs)
    body = (
        f"<h2>JobRadar — {len(jobs)} new DevOps-fit role"
        f"{'s' if len(jobs) != 1 else ''} (best fit first)</h2>{rows}"
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
            s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
        _log(f"[email] sent digest to {to}")
    except Exception as e:  # noqa: BLE001
        _log(f"[email] error: {e}")


def dispatch(jobs: list[dict]) -> None:
    if not jobs:
        return
    send_telegram(jobs)
    send_email(jobs)
