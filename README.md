# Pune Job Radar

A job-search tool for marketing roles in Pune. Two parts:

- **Jobs**: a crawler checks employers' public job boards (and, with a free key, the
  Adzuna aggregator) around the clock and flags marketing openings.
- **Companies**: a directory of every active company registered in Pune (from the
  official Ministry of Corporate Affairs registry), with size, industry, a
  "marketing fit" ranking, public office phone numbers and emails from each
  company's own website, and any open jobs. Track each company as To contact /
  Contacted / Applied with notes, and download the list as a CSV.

## Setup (one time)

1. **Company registry key (free):** sign up at https://data.gov.in, then
   My Account → Generate API key. Then in PowerShell:
   ```
   $env:DATA_GOV_API_KEY="your-key"
   python directory.py import-api       # reads all Maharashtra companies, keeps Pune ones
   ```
   If it stops partway, it prints an offset; resume with `$env:IMPORT_OFFSET="<offset>"`.
2. **Adzuna key (free, for marketing jobs at small and mid-size firms):**
   https://developer.adzuna.com → set `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`.

## Daily use

```
python crawler.py               # crawl jobs
python directory.py enrich 300  # find phones/emails for 300 more companies (largest first)
python directory.py build       # refresh the Companies view
python crawler.py serve         # open the dashboard at http://localhost:8765
```

If a company's website wasn't found, set it by hand and re-enrich:
`python directory.py site <CIN> https://example.com`

## How to read the data

- **Size** comes from paid-up capital in the registry (the registry has no headcount):
  micro < ₹10 L, small < ₹1 Cr, mid < ₹50 Cr, large ≥ ₹50 Cr or stock-listed.
- **Industry** comes from the activity code inside each company's CIN.
- **Marketing fit** ranks companies by how likely they are to employ marketing
  staff (industry + size, and a big boost if they have a marketing opening now).
  No public source lists which companies have a marketing department.
- **Contacts** are only the company's own published office numbers and generic
  addresses (info@, hr@, careers@…). Personal mailboxes from the registry are hidden.
  Websites' robots.txt rules are respected.
- The registry data currently runs to November 2023.

## Run 24/7 on GitHub

`.github/workflows/crawl.yml` crawls jobs every 2 hours, finds contacts for 300 more
companies each run, refreshes the registry monthly, and publishes the dashboard to
GitHub Pages. Add `DATA_GOV_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` as repo secrets.

Or only on this PC: `powershell -ExecutionPolicy Bypass -File schedule_windows.ps1`.

Naukri, LinkedIn and Indeed forbid scraping in their terms, so they are not crawled.
