"""Alert delivery: Telegram (instant) + email digest (durable record).

All channels are best-effort and independently optional — configure whichever
you have secrets for. Nothing here raises; failures are logged.
"""
from __future__ import annotations
import html
import os
import smtplib
import sys
from email.mime.text import MIMEText

import requests


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _job_line_html(job: dict) -> str:
    tag = f" · <i>{html.escape(job['seniority'])}</i>" if job.get("seniority") else ""
    score = f" · fit {job['ai_score']}/100" if job.get("ai_score") is not None else ""
    return (
        f"🎯 <b>{html.escape(job['title'])}</b>{tag}{score}<br>"
        f"🏢 {html.escape(job['company'])} <i>({html.escape(job.get('tier',''))})</i><br>"
        f"📍 {html.escape(job['location'] or 'n/a')}<br>"
        f"🔗 <a href=\"{html.escape(job['url'])}\">Apply / view posting</a>"
        + (f"<br>💬 <i>{html.escape(job['ai_note'])}</i>" if job.get("ai_note") else "")
    )


# ---------------- Telegram ----------------
def send_telegram(jobs: list[dict]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        _log("[telegram] skipped (no TELEGRAM_BOT_TOKEN/CHAT_ID)")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for job in jobs:
        text = _job_line_html(job).replace("<br>", "\n")
        try:
            r = requests.post(url, timeout=20, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            })
            if r.status_code != 200:
                _log(f"[telegram] {r.status_code}: {r.text[:200]}")
        except Exception as e:  # noqa: BLE001
            _log(f"[telegram] error: {e}")


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
        f"<h2>JobRadar — {len(jobs)} new matching role"
        f"{'s' if len(jobs) != 1 else ''}</h2>{rows}"
        "<hr><p style='color:#888;font-size:12px'>"
        "Apply fast + with a referral. Sent by your JobRadar GitHub Action.</p>"
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
