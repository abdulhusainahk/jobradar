# JobRadar

A GitHub Actions monitor for DevOps, SRE and platform roles across India, UAE and
selected European locations. It reads configured employer feeds and career
portals, filters jobs against your preferences, ranks matches, and delivers
Telegram and/or email digests. Google Alerts RSS supplements employers without a
supported direct integration.

Keyword matching needs no AI subscription. Optional Gemini scoring uses the
Google AI Studio API; keep its project on the **Free Tier** if you want free API
usage. A free-tier-compatible model does not prevent charges on a paid project.

## How it works

```text
Employer feeds -> all result pages -> normalize + dedup
  -> hard exclusions + broad title candidates -> full job descriptions
  -> Gemini relevance / experience / location assessment + fit score
       success: keep, reject, or retain as uncertain for review
       failure: existing keyword/location + experience/DevOps heuristic filters
  -> best fit first -> durable per-channel delivery -> saved acknowledgements
```

- The monitor is scheduled at minutes **17 and 47** of each UTC hour. GitHub can
  delay or skip scheduled runs; this is not an instant-alert or 30-minute SLA.
- The first run with a new state file records a **silent baseline**. It does not
  send all existing matches. An empty successful baseline is still initialized.
- `seen_jobs.json` preserves finalized history and a pending delivery outbox.
  Existing legacy history migrates automatically without clearing prior keys.
- A failed channel stays pending. A successful Telegram delivery is not repeated
  just because email failed. Pending job snapshots survive a listing disappearing
  from its feed. Required channels are retained until they acknowledge delivery.
- State is atomically replaced locally and committed back by Actions, including
  when a source or notification failure makes the monitor exit unsuccessfully.
- Delivery is **at least once**, not exactly once: an interruption after a remote
  service accepts a message but before its acknowledgement is persisted can
  cause a duplicate. `resend_all` intentionally sends duplicates.
- Source errors are distinguished from valid empty boards. Partial results are
  retained, but a degraded source scan exits unsuccessfully rather than claiming
  complete coverage.

## Repository setup

Enable GitHub Actions and allow the workflow's `contents: write` permission so it
can persist state. Standard hosted runners for public repositories are free;
private-repository usage follows your GitHub plan's quota.

Add settings under **Settings -> Secrets and variables -> Actions**. Configure
whichever notification channels you want; both are independently optional.

| Repository secret | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Bot token from Telegram's `@BotFather` |
| `TELEGRAM_CHAT_ID` | Chat the bot should notify; message the bot first |
| `EMAIL_USER` | Gmail account used to send digests |
| `EMAIL_APP_PASSWORD` | Gmail App Password, not the normal account password |
| `EMAIL_TO` | Recipient address(es), comma-separated; empty defaults to `EMAIL_USER` |
| `GEMINI_API_KEY` | Optional Google AI Studio API key for Gemini scoring |

To obtain a Telegram chat ID, message your bot, then inspect its `getUpdates`
response using Telegram's API. Keep the token-bearing URL private. Gmail App
Passwords require two-step verification and an account that supports App Passwords.

Notification secrets are not required for fetching or ranking. With no channels
configured, new alerts remain pending rather than being marked delivered.

## Gemini free-tier scoring

1. Create a key at <https://aistudio.google.com/apikey>.
2. Check the associated project's billing tier. **Do not enable billing** if you
   want free-tier usage only.
3. Add the key as repository secret **`GEMINI_API_KEY`**. Do not commit it.
4. The Actions workflow enables scoring by default when that key is available.
   Set repository variable `AI_SCORING=off` to force keyword-only operation.

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `AI_SCORING` | `on` in Actions | `on` enables Gemini; `off` forces keyword matching |
| `JOBRADAR_MODEL` | `gemini-3.5-flash-lite` | Gemini model ID; check current free-tier availability |
| `AI_MIN_SCORE` | `0` | Minimum score for confident `keep` decisions; uncertain roles bypass this threshold |
| `AI_MAX_ATTEMPTS` | `3` | Total attempts per failed Gemini request, from 2 to 5 |

The client uses Gemini's REST API with validated structured JSON. One request per
new candidate assesses actual role relevance, overall experience (not individual
tool tenure), geographic eligibility, fit score, and a short reason. No additional
AI SDK is required.

| Decision | Selection behavior |
| --- | --- |
| `keep` | Retain when its score meets `AI_MIN_SCORE`; rank by AI score |
| `reject` | Drop a clear mismatch, regardless of its numeric score |
| `uncertain` | Retain even below `AI_MIN_SCORE`; show **Review required: AI uncertain** |

The response also includes `relevance`, `experience_fit`, and `location_fit`.
Missing descriptions or unresolved evidence cannot create a definitive rejection
without a clear mismatch. AI-selected alerts use AI assessments as their primary
labels; the numeric heuristic score remains available as a secondary signal.

### Failure behavior

- A missing key immediately keeps keyword scoring; there is no useful request to
  retry without credentials.
- A configured key gets **three attempts by default** for request or response
  failures, including authentication errors, quotas, timeouts, unavailable
  models, blocked responses, invalid JSON and invalid scores.
- Retries use exponential backoff and the `Retry-After` header, with each delay
  bounded at 30 seconds.
- If attempts are exhausted, AI stops for the rest of the new batch. **All partial
  AI scores and decisions are discarded** before applying the existing deterministic
  filters and ranking. `AI_MIN_SCORE` never filters this fallback path.
- Successful pending alerts reuse their saved assessment during delivery retries;
  they do not incur another AI request.

Only unseen, plausible candidates reach Gemini. Experience and monitoring-only
heuristics do not reject candidates before AI runs. Explicit employer/seniority
exclusions and clearly outside locations still fail before enrichment or API use;
unresolved locations can reach AI, while fallback requires a known allowed location.
`resend_all` assesses all current candidates, so it can exhaust the free quota and trigger
fallback. No code can guarantee that a particular account/model always has free
quota. Google may use free-tier content to improve its products; do not put
confidential information in the profile or prompts.

Official references: [API keys](https://ai.google.dev/gemini-api/docs/api-key),
[pricing and free-tier availability](https://ai.google.dev/gemini-api/docs/pricing),
[structured output](https://ai.google.dev/gemini-api/docs/structured-output).

## Candidate preferences and search scope

Edit `config.yaml`:

- `profile`: role, skills and preferences used by Gemini.
- `match.candidate_years` and `max_required_years`: shared experience constraints.
- `match.regions_enabled`, `locations` and `preferred_locations`: shared regional
  eligibility and AI preferences. A preference does not exclude other enabled regions.
- `role_keywords`: broad candidate title phrases, including production engineering
  and developer productivity/experience. `intern` does not match `internal`.
- `exclude_keywords`, `exclude_unless_finance`, `exclude_companies`: explicit
  exclusions, including the current employer.
- `drop_monitoring_below`: fallback-only threshold for monitoring-only descriptions.
- `drop_out_of_band`: enables experience-band rejection; Gemini evaluates semantic
  requirements when available, and the regex-based filter is used only on fallback.

Candidate titles and provider queries remain deterministic. Broader titles are not
automatically suitable: Gemini can reject, for example, a manufacturing production
role while retaining an infrastructure production engineer. The fallback is less
precise and can retain low-scoring, unclear matches.

Existing finalized history is not reset by this cutover, and queued alerts reuse
their saved assessments. Use `resend_all` deliberately if you want older finalized
roles reconsidered; it also resends notifications.

**AI assessment is not web discovery.** It cannot recover jobs absent from feeds
or outside the broad title/hard-constraint gate. It now runs before uncertain
description-based rejections rather than only scoring their survivors.

## Supported sources

| ATS / source | Configuration |
| --- | --- |
| Greenhouse, Lever, Ashby | `ats` and employer `token` |
| SmartRecruiters | `ats: smartrecruiters`, company token; PhonePe is `PHONEPELIMITED`, Freshworks is `Freshworks` |
| TurboHire | `ats: turbohire`, career subdomain token; Navi is `navi` |
| Amazon | `ats: amazon`; optional `queries`; uses `base_query` and pagination |
| Microsoft | `ats: microsoft`; PCSX search with pagination |
| Other PCSX portals | `ats: pcsx`, `host`, `domain`; optional `queries` and `locations` |
| Workday | `ats: workday`, `host`, `site`; resolves multi-location summaries |
| Oracle Recruiting | `ats: oracle`, `host`, `site`; complete board scan by default |
| Atlassian | `ats: atlassian` |
| Google Careers | `ats: google`; best-effort HTML results, not a complete search index |
| RSS / Atom | `ats: rss`, feed `url`, optional query-scoped `location` |

Verify the **employer identity**, not just that a token returns jobs. Fintech Zeta
uses Lever `zeta`; Greenhouse `zetaglobal` is a different company. Navi fintech
uses TurboHire, not Ashby `navi`. Plivo's official careers page still uses its
Lever board, which can legitimately be empty.

APIs can change or rate-limit access. Pagination stops on exhausted results or a
visible source failure, not an arbitrary page cap. Oracle's broad search uses one
complete board scan rather than six overlapping global keyword scans. RSS cannot
guarantee complete coverage or accurate per-job locations; see `NATIVE-ALERTS.md`.
Public accessibility does not remove the need to follow each source's terms.
GET-based sources retry HTTP 429 up to three total attempts, honoring
`Retry-After` with bounded waits (30/60 seconds when the header is absent).
Oracle advances by its reported page window, including hidden rows, rather than
reusing the last page when the returned count is smaller than its advertised total.

## Running and checking locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# No notification credentials: scan and persist only an isolated local state.
AI_SCORING=off JOBRADAR_STATE=/tmp/jobradar-state.json python -m jobradar

# Regression checks (no live credentials or network requests needed).
python -m unittest discover -s tests -v
```

Local AI scoring is off unless `AI_SCORING=on` is explicitly set alongside
`GEMINI_API_KEY`. Use `JOBRADAR_CONFIG` to point at a separate configuration.
Do not delete production state to test a change: use another `JOBRADAR_STATE` path.

From **Actions -> JobRadar monitor -> Run workflow**:

- `test_alert`: sends one synthetic delivery check and leaves state untouched.
  It does **not** test Gemini. Failed/unconfigured delivery exits unsuccessfully.
- `resend_all`: sends all current matches without changing production state. Use
  it deliberately after matching-rule changes if you want to reconsider older
  jobs already finalized by the previous rules. Expect duplicate notifications.

The separate **JobRadar checks** workflow runs regression tests for code pushes
and pull requests; state-only commits do not trigger it.
