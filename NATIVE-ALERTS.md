# Employers without a pollable ATS

> **Google Alerts RSS was retired on October 30, 2026.** All five feeds (Deloitte,
> Big 4, Mumbai brands, SaaS/infra, Enterprise) returned HTTP 200 with **zero
> entries** in every one of 40 consecutive monitor runs (October 23–30). Google
> Alerts indexes news and blog pages, not job-board listings, so `site:` career
> queries combined with long `OR` lists effectively never fire. Three feed URLs
> were also wired to the wrong label in `config.yaml`. Because an empty feed is a
> valid "healthy" result, the failure was invisible in source health.

## Moved to direct ATS polling

These were previously "covered" only by the dead feeds and now have verified
public boards in `config.yaml`:

| Employer | Source |
|---|---|
| Swiggy | SmartRecruiters `Swiggy` |
| Harness | Greenhouse `harnessinc` |
| Netskope | Greenhouse `netskope` |
| Wells Fargo | Workday `wf.wd1` / `WellsFargoJobs` |

## Still no machine-readable source (track manually)

Checked October 30, 2026. None exposes a public Greenhouse, Lever, Ashby,
SmartRecruiters, Workday or Eightfold board that JobRadar can poll without
bypassing access controls.

| Group | Employers | Why |
|---|---|---|
| Big 4 | Deloitte, PwC, EY, KPMG | Custom portals (Taleo/SuccessFactors-style) with session tokens |
| Big tech | Meta, Uber, LinkedIn, Intuit, Walmart, Visa | Meta uses GraphQL with page tokens; Uber's public search RPC was removed (`ERR_MISSING_HANDLER`); Visa's SmartRecruiters board is empty; Walmart/Intuit tenants are not public Workday sites |
| Infra/SaaS | Nutanix, Confluent, Razorpay, Flipkart, Juspay, BrowserStack, Sprinklr | Nutanix career site returns 403; Confluent's Ashby board is empty; Razorpay's Greenhouse board lists no infra roles in India; the rest run custom portals |
| Mumbai | CleverTap, Media.net, Fynd, Games24x7, Angel One, ACKO, smallcase, Quantiphi, Fractal | Custom or unlisted ATS |

Recommended coverage: create **LinkedIn job alerts** (daily, India + Dubai +
remote) for `"Production Engineer" Meta`, `SRE Uber`, `SRE LinkedIn`,
`Platform Engineer Nutanix` and `DevOps Confluent`. For Meta, Uber and LinkedIn,
a referral is worth more than any alert.

Re-check this list every quarter: employers move ATS providers (Postman moved
from Greenhouse to Workday in October 2026). Add a company only after verifying
its employer identity and that the board returns roles.
