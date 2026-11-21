# Pune Job Radar

A job-search tool for marketing roles in Pune. Two parts:

- **Jobs**: a crawler checks employers' public job boards (and, with a free key, the
  Jooble and Adzuna aggregators) around the clock and flags marketing openings.
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
2. **Jooble key (free, 100 searches a day; marketing jobs at small and mid-size firms):**
   register at https://in.jooble.org/api/about (the India site: keys only work for their own
   country) and the key arrives by email. Set `JOOBLE_API_KEY`. The crawler uses at most 7
   searches per run and 90 a day.
3. **Adzuna key (optional; the free test plan is only 25 calls, then it's paid):**
   https://developer.adzuna.com → set `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`. Rationed to 4 calls
   a day; change with `ADZUNA_MAX_CALLS` and `ADZUNA_EVERY_HOURS`.

## Daily use

```
python crawler.py               # crawl jobs
python directory.py enrich 300  # find phones/emails for 300 more companies (largest first)
python directory.py build       # refresh the Companies view
python crawler.py serve         # open the dashboard at http://localhost:8765
```

If a company's website wasn't found, set it by hand and re-enrich:
`python directory.py site <CIN> https://example.com`

## Applying to jobs and emailing companies in bulk (CV + cover letter)

On the website:
1. **Jobs** tab: press **Save** on the jobs you want, then **✉ Apply to N saved jobs**.
   **Companies** tab: tap ☆ to save companies you'd like to approach even without an opening.
2. **✉ Email** tab: fill in your details once. There are two templates: one for saved jobs (it names
   the job, `{job_title}`) and one for companies (`{company}`, `{about}`, …). Add a personal line per row if you like.
3. Each saved job gets an email address from the job posting when it lists one, otherwise from the
   company's published address in the directory. Pick or type one where it's missing, or use **Job page**
   to apply on the website.
4. Click **Open in Gmail** for single emails (attach your CV with 📎), or **Download selected…** for all of
   them at once, and on the laptop run:
   ```
   python mailer.py emails-to-draft.csv --cv "My_CV.pdf" --cover-template cover_letter.txt --dry-run
   python mailer.py emails-to-draft.csv --cv "My_CV.pdf" --cover-template cover_letter.txt --send
   ```
   The first makes sample emails in `outbox/` to open and check. The second shows the list, asks you to
   type `SEND`, then sends every email from your Gmail with the CV and a personalised cover-letter PDF
   attached (one every 45 seconds, at most 40 per run, so Gmail doesn't flag the account). Sent emails
   appear in Gmail → Sent and in `outbox/sent-log.csv`. Leave out `--send` to put them in
   **Gmail → Drafts** instead and send them yourself.
5. Back on the website, press **Mark selected as applied**.

Gmail needs an App Password (Google Account → Security → 2-Step Verification → App passwords).
Edit `cover_letter.txt` first: `{company}`, `{position}` (e.g. "the Brand Manager position") and `{date}` are filled in.

Saved jobs and companies, notes and your details stay in your browser only. CVs, cover letters and email
lists are never uploaded to GitHub (they're in `.gitignore`).

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
GitHub Pages. Add `DATA_GOV_API_KEY`, `JOOBLE_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` as repo secrets.

Or only on this PC: `powershell -ExecutionPolicy Bypass -File schedule_windows.ps1`.

Naukri, LinkedIn and Indeed forbid scraping in their terms, so they are not crawled.
