"""Pune job crawler.

Pulls openings from employers' public job-board APIs (Greenhouse, Lever, Ashby,
SmartRecruiters, Workday) plus, optionally, the Jooble and Adzuna aggregator APIs for broad
coverage of small and mid-size companies. Keeps only Pune-area roles (and,
optionally, remote-India roles), flags marketing roles, and writes web/jobs.json.

Usage:
  python crawler.py                 # one crawl
  python crawler.py watch 60        # crawl every 60 minutes, forever
  python crawler.py discover "Name" [--add]   # find a company's job board
  python crawler.py serve           # open dashboard at http://localhost:8765

Only standard-library Python is needed.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
COMPANIES = os.path.join(ROOT, "companies.json")
OUT = os.path.join(ROOT, "web", "jobs.json")
UA = "PuneJobCrawler/1.0 (personal job search; polite, low volume)"
KEEP_CLOSED_DAYS = 14
AGGREGATORS = {"adzuna", "jooble"}
AGGREGATOR_GRACE_DAYS = 3

PUNE_RE = re.compile(
    r"\b(pune|poona|hinjewadi|hinjawadi|kharadi|magarpatta|baner|hadapsar|viman ?nagar|"
    r"yerwada|yerawada|pimpri|chinchwad|chakan|talegaon|wakad|aundh|balewadi|"
    r"kalyani ?nagar|bavdhan|ranjangaon|kothrud|shivajinagar)\b", re.I)
INCLUDE_REMOTE_INDIA = os.environ.get("INCLUDE_REMOTE_INDIA", "1") == "1"
MARKETING_RE = re.compile(
    r"marketing|\bbrand|\bseo\b|\bsem\b|\bsmm\b|social media|content (writer|strateg|market|creator|manager)|"
    r"copywrit|growth|communications?\b|public relations|\bpr\b|campaign|demand gen|advertis|media (planner|buyer)|"
    r"market research|\bevents?\b|influencer|crm executive|e-?mail market|performance market|go-to-market|\bgtm\b|"
    r"\bppc\b|paid (media|ads|social)|creative writer|content (associate|executive|specialist)",
    re.I)


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def http_json(url, body=None, retries=2):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503) and attempt < retries:
                time.sleep(15 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries:
                time.sleep(5)
                continue
            raise


def classify(location):
    """Return 'pune', 'remote-india' or None."""
    loc = location or ""
    if PUNE_RE.search(loc):
        return "pune"
    if INCLUDE_REMOTE_INDIA and re.search(r"india", loc, re.I) and re.search(r"remote|anywhere", loc, re.I):
        return "remote-india"
    return None


def unmangle(s):
    """Fix text that was UTF-8 but got decoded as Windows-1252 upstream (e.g. 'â€“' for '–')."""
    if s and re.search(r"â€|Ã.", s):
        try:
            return s.encode("cp1252").decode("utf-8")
        except UnicodeError:
            pass
    return s


def job(company, source, jid, title, location, url, posted=None, dept=None, size=None):
    title, dept, company = unmangle((title or "").strip()), dept or "", unmangle(company)
    return {
        "id": f"{source}:{jid}", "company": company, "title": title,
        "location": (location or "").strip(), "url": url, "posted": posted,
        "dept": dept, "source": source, "size": size or "",
        "marketing": bool(MARKETING_RE.search(title + " " + dept))
                     and not re.search(r"engineer|developer|architect|\bsde\b|devops", title, re.I),
    }


# ---------- fetchers: each returns a list of job dicts (unfiltered) ----------

def f_greenhouse(c):
    d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs")
    return [job(c["name"], "greenhouse", j["id"], j["title"], (j.get("location") or {}).get("name"),
                j["absolute_url"], j.get("updated_at"), None, c.get("size")) for j in d["jobs"]]


def f_lever(c):
    d = http_json(f"https://api.lever.co/v0/postings/{c['slug']}?mode=json")
    out = []
    for j in d:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location") or ""]
        posted = datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc).isoformat() if j.get("createdAt") else None
        out.append(job(c["name"], "lever", j["id"], j["text"], " / ".join(l for l in locs if l),
                       j["hostedUrl"], posted, cat.get("team"), c.get("size")))
    return out


def f_ashby(c):
    d = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}")
    out = []
    for j in d.get("jobs", []):
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        loc = " / ".join(l for l in locs if l)
        if j.get("isRemote") and "remote" not in loc.lower():
            loc += " (Remote)"
        out.append(job(c["name"], "ashby", j["id"], j["title"], loc, j["jobUrl"],
                       j.get("publishedAt"), j.get("department"), c.get("size")))
    return out


def f_smartrecruiters(c):
    out, offset = [], 0
    while True:
        d = http_json(f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings"
                      f"?limit=100&offset={offset}&city=Pune")
        for j in d.get("content", []):
            l = j.get("location") or {}
            loc = ", ".join(x for x in [l.get("city"), l.get("region"), l.get("country", "").upper()] if x)
            out.append(job(c["name"], "smartrecruiters", j["id"], j["name"], loc,
                           f"https://jobs.smartrecruiters.com/{c['slug']}/{j['id']}",
                           j.get("releasedDate"), (j.get("department") or {}).get("label"), c.get("size")))
        offset += 100
        if offset >= d.get("totalFound", 0) or offset >= 500:
            return out


def f_workday(c):
    host, tenant, site = c["slug"].split("|")
    out, offset = [], 0
    while offset < 200:
        d = http_json(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                      {"searchText": "Pune", "limit": 20, "offset": offset, "appliedFacets": {}})
        posts = d.get("jobPostings", [])
        for j in posts:
            path = j.get("externalPath", "")
            out.append(job(c["name"], "workday", f"{tenant}{path}", j.get("title"), j.get("locationsText"),
                           f"https://{host}/en-US/{site}{path}", j.get("postedOn"), None, c.get("size")))
        offset += 20
        if offset >= d.get("total", 0) or not posts:
            break
        time.sleep(2)
    return out


ADZUNA_SKIPPED = "skipped"


def f_adzuna(last_run=None):
    """Aggregator: covers thousands of Pune employers incl. small companies. Needs a key.

    Adzuna counts every request, so this is rationed: at most ADZUNA_MAX_CALLS requests per run
    (default 4) and at most one run every ADZUNA_EVERY_HOURS hours (default 24).
    """
    app_id, key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and key):
        return None
    budget = int(os.environ.get("ADZUNA_MAX_CALLS") or 4)
    every = float(os.environ.get("ADZUNA_EVERY_HOURS") or 24)
    if budget <= 0:
        return None
    if last_run and datetime.now(timezone.utc) - datetime.fromisoformat(last_run) < timedelta(hours=every):
        return ADZUNA_SKIPPED
    out, seen = [], set()
    # marketing jobs first; general Pune jobs only if calls are left over
    for what in ("marketing", None):
        for page in range(1, 100):
            if budget <= 0:
                break
            budget -= 1
            params = {"app_id": app_id, "app_key": key, "where": "Pune",
                      "results_per_page": 50, "max_days_old": 30, "sort_by": "date"}
            if what:
                params["what"] = what
            d = http_json(f"https://api.adzuna.com/v1/api/jobs/in/search/{page}?{urllib.parse.urlencode(params)}")
            res = d.get("results", [])
            for j in res:
                if j["id"] in seen:
                    continue
                seen.add(j["id"])
                jb = job((j.get("company") or {}).get("display_name") or "Unknown", "adzuna", j["id"],
                         j.get("title"), (j.get("location") or {}).get("display_name") or "Pune",
                         j.get("redirect_url"), j.get("created"), (j.get("category") or {}).get("label"))
                if "marketing" in jb["dept"].lower():  # Adzuna category "PR, Advertising & Marketing Jobs"
                    jb["marketing"] = True
                out.append(jb)
            if len(res) < 50:
                break
            time.sleep(1)
    for j in out:  # Adzuna already filtered by where=Pune
        j["location"] = j["location"] if PUNE_RE.search(j["location"]) else j["location"] + " (Pune area)"
    return out


JOOBLE_EXTRA = ["digital marketing", "social media", "brand", "SEO", "content writer",
                "public relations", "marketing executive", "performance marketing", "event"]


def f_jooble(state):
    """Aggregator with a free key: 100 requests a day per key, reset at midnight UTC.

    `state` is {"day": "YYYY-MM-DD", "calls": n}, carried in jobs.json so the daily limit holds
    across runs. Each run spends at most JOOBLE_MAX_CALLS (default 7): the "marketing" search
    first, then one page each of a few related searches that rotate from run to run.
    """
    key = os.environ.get("JOOBLE_API_KEY")
    if not key:
        return None
    day = datetime.now(timezone.utc).date().isoformat()
    if state.get("day") != day:
        state.clear()
        state.update(day=day, calls=0)
    daily = int(os.environ.get("JOOBLE_DAILY") or 90)
    budget = min(int(os.environ.get("JOOBLE_MAX_CALLS") or 7), daily - state["calls"])
    if budget <= 0:
        return ADZUNA_SKIPPED
    host = os.environ.get("JOOBLE_HOST") or "in.jooble.org"
    turn = datetime.now(timezone.utc).hour // 2
    extras = JOOBLE_EXTRA[turn % len(JOOBLE_EXTRA):] + JOOBLE_EXTRA[:turn % len(JOOBLE_EXTRA)]
    plan = [("marketing", p) for p in (1, 2, 3)] + [(q, 1) for q in extras]
    out, seen, done = [], set(), set()
    for kw, page in plan:
        if budget <= 0:
            break
        if kw in done:
            continue  # the previous page of this search was the last one
        budget -= 1
        state["calls"] += 1
        body = {"keywords": kw, "location": "Pune", "radius": "40", "page": str(page), "ResultOnPage": 50}
        try:
            d = http_json(f"https://{host}/api/{key}", body, retries=0)
        except urllib.error.HTTPError as e:
            if e.code == 429:  # daily limit used up elsewhere: stop for today
                state["calls"] = daily
                break
            raise
        res = d.get("jobs", [])
        if len(res) < 20:  # fewer than any sensible page size: no more pages
            done.add(kw)
        for j in res:
            if j.get("id") in seen:
                continue
            seen.add(j.get("id"))
            loc = (j.get("location") or "Pune").strip()
            if not PUNE_RE.search(loc):
                loc += " (Pune area)"
            out.append(job((j.get("company") or "").strip() or "Unknown", "jooble", j.get("id"),
                           j.get("title"), loc, j.get("link"), j.get("updated"), j.get("type")))
        time.sleep(1)
    return out


FETCHERS = {"greenhouse": f_greenhouse, "lever": f_lever, "ashby": f_ashby,
            "smartrecruiters": f_smartrecruiters, "workday": f_workday}


# ---------- crawl ----------

def load_state():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"jobs": []}


def crawl():
    with open(COMPANIES, encoding="utf-8") as f:
        companies = json.load(f)["companies"]
    started = now()
    found, status = [], []

    def run(c):
        t = time.time()
        try:
            jobs = FETCHERS[c["ats"]](c)
            return c, jobs, None, time.time() - t
        except Exception as e:  # keep crawling other companies
            return c, [], f"{type(e).__name__}: {str(e)[:120]}", time.time() - t

    api = [c for c in companies if c["ats"] != "workday"]
    wd = [c for c in companies if c["ats"] == "workday"]
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(run, api))
    for c in wd:  # Workday rate-limits aggressively: one tenant at a time
        results.append(run(c))
        time.sleep(3)

    for c, jobs, err, secs in results:
        kept = []
        for j in jobs:
            kind = classify(j["location"]) or ("pune" if c["ats"] == "smartrecruiters" else None)
            if kind:
                j["where"] = kind
                kept.append(j)
        found += kept
        status.append({"key": f"{c['ats']}:{c['slug']}", "name": c["name"], "ats": c["ats"],
                       "ok": err is None, "error": err, "total": len(jobs), "pune": len(kept)})

    prev_state = load_state()
    adzuna_last = prev_state.get("adzuna_last")
    try:
        az = f_adzuna(adzuna_last)
        if az == ADZUNA_SKIPPED:  # not due yet: keep the Adzuna jobs we already have
            status.append({"key": "adzuna", "name": "Adzuna (aggregator)", "ats": "adzuna", "ok": False,
                           "error": "not due yet (rationed to save API calls)", "total": 0, "pune": 0})
            az = None
        elif az is not None:
            adzuna_last = now()
            for j in az:
                j["where"] = "pune"
            found += az
            status.append({"key": "adzuna", "name": "Adzuna (aggregator)", "ats": "adzuna", "ok": True,
                           "error": None, "total": len(az), "pune": len(az)})
    except Exception as e:
        status.append({"key": "adzuna", "name": "Adzuna (aggregator)", "ats": "adzuna", "ok": False,
                       "error": str(e)[:120], "total": 0, "pune": 0})

    jooble_state = dict(prev_state.get("jooble") or {})
    try:
        jb = f_jooble(jooble_state)
        if jb == ADZUNA_SKIPPED:
            status.append({"key": "jooble", "name": "Jooble (aggregator)", "ats": "jooble", "ok": False,
                           "error": "daily limit reached; resumes after midnight UTC", "total": 0, "pune": 0})
        elif jb is not None:
            # the same opening is often on Adzuna and Jooble: keep the first copy
            others = found + [j for j in prev_state.get("jobs", []) if j["source"] != "jooble" and not j.get("closed")]
            have = {(j["company"].lower(), j["title"].lower()) for j in others}
            fresh = [j for j in jb if (j["company"].lower(), j["title"].lower()) not in have]
            for j in fresh:
                j["where"] = "pune"
            found += fresh
            status.append({"key": "jooble", "name": "Jooble (aggregator)", "ats": "jooble", "ok": True,
                           "error": None, "total": len(jb), "pune": len(fresh)})
    except Exception as e:
        status.append({"key": "jooble", "name": "Jooble (aggregator)", "ats": "jooble", "ok": False,
                       "error": f"{type(e).__name__}: {str(e)[:120]}", "total": 0, "pune": 0})

    # merge with previous state: keep first_seen, mark vanished jobs closed
    prev = {j["id"]: j for j in prev_state.get("jobs", [])}
    # aggregators only return a slice of results per run, so a job missing from one run isn't closed
    grace = (datetime.now(timezone.utc) - timedelta(days=AGGREGATOR_GRACE_DAYS)).isoformat()
    failed = {s["key"] for s in status if not s["ok"]}
    ts = now()
    merged, new = {}, []
    for j in found:
        old = prev.get(j["id"])
        j["first_seen"] = old["first_seen"] if old else ts
        j["last_seen"] = ts
        j["closed"] = None
        if not old:
            new.append(j)
        merged[j["id"]] = j
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_CLOSED_DAYS)).isoformat()
    src_key = {c["name"]: f"{c['ats']}:{c['slug']}" for c in companies}
    for jid, old in prev.items():
        if jid in merged:
            continue
        key = old["source"] if old["source"] in AGGREGATORS else src_key.get(old["company"], "")
        if key in failed and not old.get("closed"):
            merged[jid] = old  # source was down this round; don't assume the job closed
            continue
        if key in AGGREGATORS and not old.get("closed") and old.get("last_seen", "") >= grace:
            merged[jid] = old
            continue
        old["closed"] = old.get("closed") or ts
        if old["closed"] >= cutoff:
            merged[jid] = old

    jobs = sorted(merged.values(), key=lambda j: j["first_seen"], reverse=True)
    jobs.sort(key=lambda j: j["closed"] is not None)  # stable: open jobs first, newest first
    out = {"updated": ts, "started": started, "sources": status, "adzuna_last": adzuna_last,
           "jooble": jooble_state,
           "counts": {"open": sum(1 for j in jobs if not j["closed"]), "new": len(new)}, "jobs": jobs}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)

    ok = sum(1 for s in status if s["ok"])
    print(f"[{ts}] {out['counts']['open']} open Pune jobs, {len(new)} new, sources ok {ok}/{len(status)}")
    for s in status:
        if not s["ok"]:
            print(f"   ! {s['name']}: {s['error']}")
    notify(new)
    return out


# ---------- alerts (optional) ----------

def notify(new):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat and new):
        return
    kws = [k.strip().lower() for k in os.environ.get("KEYWORDS", "").split(",") if k.strip()]
    hits = [j for j in new if not kws or any(k in (j["title"] + " " + j["dept"]).lower() for k in kws)]
    for j in hits[:20]:
        text = f"New in Pune: {j['title']}\n{j['company']} - {j['location']}\n{j['url']}"
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()), timeout=15)
        except Exception as e:
            print("telegram failed:", e)
            return


# ---------- discover a company's job board ----------

def discover(name, add=False):
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    slugs = list(dict.fromkeys([base.replace(" ", ""), base.replace(" ", "-"), base.split(" ")[0],
                                base.replace(" ", "") + "inc", base.replace(" ", "") + "1",
                                base.replace(" ", "") + "careers", base.replace(" ", "") + "technologies"]))
    hits = []
    for ats in ("greenhouse", "lever", "ashby", "smartrecruiters"):
        for s in slugs:
            c = {"name": name, "ats": ats, "slug": s}
            try:
                if ats == "smartrecruiters":
                    d = http_json(f"https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=1", retries=0)
                    total = d.get("totalFound", 0)
                    if not total:
                        continue
                    jobs = f_smartrecruiters(c)
                    pune = len(jobs)
                else:
                    jobs = FETCHERS[ats](c)
                    total, pune = len(jobs), sum(1 for j in jobs if classify(j["location"]))
            except Exception:
                continue
            print(f"  found {ats}:{s}  {total} jobs total, {pune} in Pune/remote-India")
            hits.append((pune, total, c))
            break
    if not hits:
        print(f"  no public Greenhouse/Lever/Ashby/SmartRecruiters board found for '{name}'."
              " If its careers URL contains myworkdayjobs.com, add it as ats=workday by hand.")
        return
    best = max(hits, key=lambda h: (h[0], h[1]))[2]
    if add:
        with open(COMPANIES, encoding="utf-8") as f:
            data = json.load(f)
        if any(c["ats"] == best["ats"] and c["slug"] == best["slug"] for c in data["companies"]):
            print("  already in companies.json")
            return
        data["companies"].append(best)
        with open(COMPANIES, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"  added {best['ats']}:{best['slug']} to companies.json")


def serve(port=8765):
    import functools, http.server, webbrowser
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=os.path.join(ROOT, "web"))
    print(f"Dashboard: http://localhost:{port}")
    webbrowser.open(f"http://localhost:{port}")
    http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "crawl"
    if cmd == "crawl":
        crawl()
    elif cmd == "watch":
        every = int(args[1]) if len(args) > 1 else 60
        while True:
            try:
                crawl()
            except Exception as e:
                print("crawl failed:", e)
            time.sleep(every * 60)
    elif cmd == "discover":
        names = [a for a in args[1:] if a != "--add"]
        for n in names:
            print(n)
            discover(n, add="--add" in args)
    elif cmd == "serve":
        serve()
    else:
        print(__doc__)
