# Native-alert companies → one common digest

Some companies (Big 4, Indian consumer startups, a few enterprises) run **custom
career sites with no machine-readable API** — JobRadar can't poll them directly.

The fix: **Google Alerts → RSS → JobRadar**. You create one Google Alert per
company (region + role scoped), set its delivery to **RSS**, and paste the feed
URL into `config.yaml`. JobRadar's `rss` fetcher then polls those feeds and folds
the results into the **same India/International digest** — so you still get
**one common alert**, deduped and fit-ranked, not 25 separate company emails.

```
Google Alert (per company)  ──RSS feed──►  JobRadar rss fetcher  ──►  same digest
   site:careers.X.com devops india                                    (Telegram + email)
```

---

## One-time setup (~10 min for all)

For each company below:

1. Go to **https://www.google.com/alerts**
2. Paste the **query** from the table into the search box.
3. Click **Show options** →
   - **How often:** As-it-happens
   - **Sources:** Automatic
   - **Deliver to:** **RSS feed**  ← important
4. Click **Create Alert**.
5. On the alerts page, click the **RSS icon** next to the new alert → copy the
   feed URL (looks like `https://www.google.com/alerts/feeds/1234567890/9876543210`).
6. In `config.yaml`, find that company's commented line under the NATIVE-ALERT
   section, **uncomment it**, and paste the URL as `url:`. Example:
   ```yaml
   - { name: Deloitte, ats: rss, url: "https://www.google.com/alerts/feeds/12.../98...", tier: "Big4 · Native" }
   ```
7. Commit + push. Done — those roles now appear in your normal digest.

> You don't have to do all of them. Start with the ones you care about most
> (e.g. Mumbai brands), add more later.

---

## Queries (copy-paste)

Each query is scoped to the company's careers site + DevOps/SRE/Platform roles +
India. Adjust the role terms or drop `india` if you want broader coverage.

### Big 4
| Company | Google Alert query |
|---|---|
| Deloitte | `site:apply.deloitte.com ("devops" OR "site reliability" OR "platform engineer" OR "cloud engineer") india` |
| PwC | `site:pwc.com/careers ("devops" OR "site reliability" OR "platform engineer") india` |
| EY | `site:careers.ey.com ("devops" OR "site reliability" OR "platform engineer") india` |
| KPMG | `site:kpmg.com ("devops" OR "site reliability engineer" OR "platform engineer") india careers` |

### MANGO / GCC gap
| Company | Query |
|---|---|
| Uber | `site:uber.com/careers ("devops" OR "site reliability" OR "platform engineer") india` |
| Intuit | `site:intuit.com/careers ("devops" OR "site reliability" OR "platform engineer") india` |
| Walmart | `site:careers.walmart.com ("devops" OR "site reliability" OR "platform engineer") india` |
| Visa | `"Visa" ("devops" OR "site reliability engineer" OR "platform engineer") india careers` |

### Mumbai brands
| Company | Query |
|---|---|
| CleverTap | `site:clevertap.com ("devops" OR "site reliability" OR "platform engineer" OR "infrastructure") careers` |
| Media.net | `site:media.net ("devops" OR "site reliability" OR "platform engineer") careers` |
| Fynd | `site:fynd.com ("devops" OR "sre" OR "platform engineer" OR "infrastructure") jobs` |
| Games24x7 | `"Games24x7" ("devops" OR "site reliability" OR "platform engineer") careers` |
| Angel One | `site:angelone.in ("devops" OR "sre" OR "platform engineer" OR "cloud") careers` |
| ACKO | `site:acko.com ("devops" OR "site reliability" OR "platform engineer") careers` |
| smallcase | `site:smallcase.com ("devops" OR "sre" OR "platform engineer" OR "infrastructure") careers` |
| Quantiphi | `site:quantiphi.com ("devops" OR "mlops" OR "platform engineer" OR "cloud engineer") careers` |
| Fractal | `site:fractal.ai ("devops" OR "mlops" OR "platform engineer" OR "cloud") careers` |

### SaaS / infra
| Company | Query |
|---|---|
| Razorpay | `site:razorpay.com ("devops" OR "site reliability" OR "platform engineer") jobs` |
| Swiggy | `site:careers.swiggy.com ("devops" OR "site reliability" OR "platform engineer")` |
| Flipkart | `site:flipkartcareers.com ("devops" OR "site reliability" OR "platform engineer")` |
| Juspay | `site:juspay.io ("devops" OR "sre" OR "platform engineer" OR "infrastructure") careers` |
| Nutanix | `site:nutanix.com ("devops" OR "site reliability" OR "platform engineer") india careers` |
| Harness | `site:harness.io ("devops" OR "site reliability" OR "platform engineer") india` |
| HashiCorp | `site:hashicorp.com ("devops" OR "site reliability" OR "platform engineer") india` |
| Confluent | `site:confluent.io ("devops" OR "site reliability" OR "platform engineer") india` |
| BrowserStack | `site:browserstack.com ("devops" OR "site reliability" OR "platform engineer") careers` |
| Sprinklr | `site:sprinklr.com ("devops" OR "site reliability" OR "platform engineer") india careers` |

---

## Notes

- RSS items have **no full job description**, so they're scored on the **title**
  (title/seniority boost) rather than deep DevOps-fit. They still respect the
  role filter, dedupe, and grouping.
- They default to the **India** group. If a company's alert is region-specific,
  add `location: "Dubai"` (etc.) to its config line to group it correctly.
- Google Alerts can lag a few hours and occasionally miss postings — it's a
  best-effort net for the sites automation can't reach, not a guarantee.
