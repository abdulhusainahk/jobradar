# JobRadar 🎯

A free, always-on **cloud monitor** for senior DevOps / SRE / Platform openings at
high-end product companies (Mumbai → Bangalore → remote-India). It polls each
company's **public ATS API** (Greenhouse / Lever / Ashby) on a schedule via
**GitHub Actions**, filters for roles that fit your profile, de-dupes against
what it has already seen, and pings you on **Telegram + email** the moment a new
one appears.

- **₹0/month** — runs on GitHub Actions' free public-repo minutes. No server, no PC.
- **No scraping, no ToS risk** — reads official ATS JSON feeds only.
- **Notify-first** — you apply fast, with a referral (the highest-conversion path
  at top product companies). Optional device-side *assisted* apply via Simplify.
- **Keyword-only by default (free).** Optional AI fit-scoring is a feature flag.

Verified live on build day: **19 companies, ~3,250 roles scanned, correct matches.**

---

## How it works

```
GitHub Actions cron (every 30 min)
        │
        ▼
  fetch each company's ATS feed  →  filter (role + location + seniority)
        │                                   │
        ▼                                   ▼
  dedupe vs seen_jobs.json  ───────►  new matches only
        │                                   │
        ▼                                   ▼
  commit updated state              Telegram + email alert
```

The dedup store `seen_jobs.json` is committed back to the repo each run, so state
survives with zero infrastructure. The **first run is a silent baseline** — it
records everything currently open and alerts on nothing, so you only get pinged
for roles posted *after* JobRadar goes live.

---

## Setup (one-time, ~15 min)

### 1. Create the repo
Create a **public** repo `jobradar` under your GitHub account and push this folder:

```bash
cd C:/Users/Administrator/jobradar
git init && git add -A && git commit -m "JobRadar v0.1"
git branch -M main
git remote add origin https://github.com/abdulhusainahk/jobradar.git
git push -u origin main
```
> Public repo = unlimited free Actions minutes. (Private works too but bills against a monthly quota.)

### 2. Create a Telegram bot (2 min)
1. In Telegram, message **@BotFather** → `/newbot` → follow prompts → copy the **bot token**.
2. Message your new bot once (say "hi") so it can DM you.
3. Get your **chat id**: open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and copy
   `result[0].message.chat.id` (a number).

### 3. Get a Gmail App Password (for email copies)
Google Account → **Security** → 2-Step Verification (must be on) →
**App passwords** → generate one for "Mail". Copy the 16-char password.
(Use `abdulhusainahk@gmail.com`.)

### 4. Add repo secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from BotFather |
| `TELEGRAM_CHAT_ID` | your chat id |
| `EMAIL_USER` | `abdulhusainahk@gmail.com` |
| `EMAIL_APP_PASSWORD` | the 16-char Gmail app password |
| `EMAIL_TO` | `abdulhusainahk@gmail.com` (comma-separate for multiple) |

That's it. The workflow runs every 30 min automatically. Trigger it now from
**Actions → JobRadar monitor → Run workflow** to record the baseline.

---

## Enabling AI scoring later (optional, ~pennies/month)

Keyword-only is the default and fully free. To add a Claude-Haiku **fit score
(0-100) + tailored note** on each new role:

1. Create a **dedicated** Anthropic API key at console.anthropic.com (this is
   separate from your Claude subscription — the subscription can't power headless
   cloud calls). Set a low monthly spend cap.
2. Add secret `ANTHROPIC_API_KEY`.
3. Add a repo **variable** (not secret): `AI_SCORING = on`
   (Settings → Secrets and variables → Actions → **Variables** tab).

Cost is trivial — it scores only *new* postings (a handful/day) on Haiku
(`$1/$5` per 1M tokens). Edit your profile text in `jobradar/score.py`. Set
`AI_MIN_SCORE` (repo variable) to auto-drop weak matches.

---

## Running / testing locally

```bash
pip install -r requirements.txt
# dry run (no secrets set = it just scans & prints, sends nothing):
JOBRADAR_STATE=/tmp/state.json python -m jobradar
```

Delete `seen_jobs.json` (or point `JOBRADAR_STATE` elsewhere) to re-baseline.

---

## Adding companies

Edit `config.yaml`. Each entry needs an `ats` and a `token`:

| ATS | How to find the token | Test URL |
|---|---|---|
| `greenhouse` | careers page URL `boards.greenhouse.io/<token>` | `https://boards-api.greenhouse.io/v1/boards/<token>/jobs` |
| `lever` | `jobs.lever.co/<token>` | `https://api.lever.co/v0/postings/<token>?mode=json` |
| `ashby` | `jobs.ashbyhq.com/<token>` | `https://api.ashbyhq.com/posting-api/job-board/<token>` |

If the test URL returns JSON with jobs, the token is valid. Tune matching in the
`match:` block (role/location/seniority/exclude keywords).

---

## Roadmap (v2)

- **Big-tech fetchers.** Amazon, Google, Microsoft, Apple, Atlassian, Flipkart,
  Walmart, Salesforce, Adobe, Razorpay use their own career sites (custom JSON
  APIs), not Greenhouse/Lever/Ashby. Each needs a bespoke fetcher. Until then,
  set a **saved search + native email alert** on each of those career pages.
- Assisted auto-apply hook (Simplify/FastApply, device-side).
- Cloudflare Workers Cron variant for sub-minute latency.
- Weekly digest + "roles closed" tracking.

## Note
Built for personal job-radar use. Reads only public ATS endpoints. Excludes your
current employer (Dream Sports / FanCode) by config.
