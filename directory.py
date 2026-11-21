"""Pune company directory: every registered company in Pune, with size, industry,
marketing fit, public contact details and open marketing jobs.

Data sources
  1. Ministry of Corporate Affairs company master data (official registry, all sizes)
     via data.gov.in. Needs a free API key: sign up at https://data.gov.in, then
     "My Account -> Generate API key" and set DATA_GOV_API_KEY.
     Or download the Maharashtra CSV from data.gov.in yourself and run import-csv.
  2. Company websites (found from the company's registry email domain or added by
     hand): the homepage and contact page are read for public office phone numbers
     and generic emails (info@, hr@, careers@...). robots.txt is respected.
  3. Open jobs from crawler.py (web/jobs.json), matched by company name.

Usage
  python directory.py import-api              # pull Pune companies from the registry
  python directory.py import-csv file.csv     # same, from a downloaded CSV
  python directory.py enrich 300              # find websites/phones for 300 more companies
  python directory.py job-contacts            # find emails for companies with open marketing jobs
  python directory.py build                   # write web/companies.json for the dashboard
"""
import csv
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "data", "companies.sqlite")
OUT = os.path.join(ROOT, "web", "companies.json")
JOBS = os.path.join(ROOT, "web", "jobs.json")
UA = "PuneJobCrawler/1.0 (personal job search; polite, low volume)"
RESOURCE = "4dbe5667-7b6b-41d7-82af-211562424d9a"  # data.gov.in: RoC-wise Company Master Data

PUNE_ADDR = re.compile(r"\bpune\b|\bpimpri\b|\bchinchwad\b|\bhinj[ae]wadi\b|\b41[12]\s?\d{3}\b", re.I)
FREEMAIL = {"gmail.com", "yahoo.com", "yahoo.co.in", "hotmail.com", "rediffmail.com", "outlook.com",
            "live.com", "ymail.com", "icloud.com", "rediff.com", "aol.com", "protonmail.com", "in.com",
            "zoho.com", "msn.com", "yahoo.in", "gmai.com", "vsnl.net", "vsnl.com", "sify.com", "mail.com"}

# ---------- industry from the NIC code inside the CIN ----------
# CIN = L/U + 5-digit NIC activity code + state + year + type + number (e.g. U72200MH2010PTC123456)
NIC2008 = [((1, 3), "Agriculture"), ((5, 9), "Mining"), ((10, 12), "Food & beverages (FMCG)"),
           ((13, 15), "Textiles & apparel"), ((16, 18), "Paper & printing"), ((19, 20), "Chemicals"),
           ((21, 21), "Pharma"), ((22, 25), "Industrial manufacturing"), ((26, 27), "Electronics & electrical"),
           ((28, 28), "Machinery"), ((29, 30), "Automotive"), ((31, 33), "Other manufacturing"),
           ((35, 39), "Energy & utilities"), ((41, 43), "Construction"), ((45, 46), "Wholesale & distribution"),
           ((47, 47), "Retail"), ((49, 53), "Logistics"), ((55, 56), "Hospitality & food"),
           ((58, 58), "Publishing"), ((59, 60), "Media & entertainment"), ((61, 61), "Telecom"),
           ((62, 63), "IT & software"), ((64, 66), "Finance & insurance"), ((68, 68), "Real estate"),
           ((69, 69), "Legal & accounting"), ((70, 70), "Consulting"), ((71, 72), "Engineering & R&D"),
           ((73, 73), "Advertising & market research"), ((74, 75), "Professional services"),
           ((77, 82), "Business services & staffing"), ((84, 84), "Public sector"), ((85, 85), "Education"),
           ((86, 88), "Healthcare"), ((90, 93), "Arts, sports & events"), ((94, 96), "Other services")]
NIC2004 = [((1, 5), "Agriculture"), ((10, 14), "Mining"), ((15, 16), "Food & beverages (FMCG)"),
           ((17, 19), "Textiles & apparel"), ((20, 22), "Paper & printing"), ((23, 24), "Chemicals"),
           ((25, 28), "Industrial manufacturing"), ((29, 29), "Machinery"), ((30, 33), "Electronics & electrical"),
           ((34, 35), "Automotive"), ((36, 37), "Other manufacturing"), ((40, 41), "Energy & utilities"),
           ((45, 45), "Construction"), ((50, 51), "Wholesale & distribution"), ((52, 52), "Retail"),
           ((55, 55), "Hospitality & food"), ((60, 63), "Logistics"), ((64, 64), "Telecom"),
           ((65, 67), "Finance & insurance"), ((70, 70), "Real estate"), ((71, 71), "Business services & staffing"),
           ((72, 72), "IT & software"), ((73, 73), "Engineering & R&D"), ((74, 74), "Professional services"),
           ((75, 75), "Public sector"), ((80, 80), "Education"), ((85, 85), "Healthcare"),
           ((90, 91), "Other services"), ((92, 92), "Media & entertainment"), ((93, 99), "Other services")]

# How likely a company in this sector employs marketing people (0-3). Company size adds to this.
MKT_SECTOR = {"Advertising & market research": 3, "Media & entertainment": 3, "Publishing": 2,
              "Food & beverages (FMCG)": 3, "Retail": 3, "Real estate": 3, "Hospitality & food": 3,
              "Education": 3, "Healthcare": 2, "Finance & insurance": 3, "IT & software": 2,
              "Telecom": 3, "Automotive": 2, "Pharma": 3, "Textiles & apparel": 2, "Consulting": 2,
              "Arts, sports & events": 2, "Wholesale & distribution": 2, "Electronics & electrical": 2,
              "Business services & staffing": 2, "Professional services": 2, "Logistics": 1,
              "Chemicals": 1, "Construction": 1, "Machinery": 1, "Industrial manufacturing": 1,
              "Engineering & R&D": 1, "Other manufacturing": 1, "Other services": 1}


# MCA used NIC-2004 codes in CINs until 2020 and a mix of NIC-2004 and NIC-2008 afterwards.
# These divisions only make sense as NIC-2008 codes (in NIC-2004 they are unused or rare).
ONLY_2008 = {10, 11, 12, 41, 42, 43, 46, 47, 49, 53, 56, 58, 59, 61, 62, 68, 69, 77, 78, 79, 81, 82, 86, 87, 88}
ADS_NAME = re.compile(r"advertis|media|marketing|\bbrand|creative|digital|communication|promotion|\bpr\b|outdoor", re.I)


def sector(cin, name=""):
    m = re.match(r"^[LU](\d{5})[A-Z]{2}(\d{4})", cin or "")
    if not m:
        return "Unknown"
    code, year = m.group(1), int(m.group(2))
    div = int(code[:2])
    if div == 73 and year >= 2021:  # R&D in NIC-2004, advertising in NIC-2008: let the name decide
        return "Advertising & market research" if ADS_NAME.search(name or "") else "Engineering & R&D"
    if year >= 2021 and div in ONLY_2008:
        table = NIC2008
    else:
        table = NIC2004
        if code.startswith("743") or code.startswith("7413"):
            return "Advertising & market research"   # NIC-2004 7430 advertising, 7413 market research
        if code.startswith("921") or code.startswith("922"):
            return "Media & entertainment"           # films, radio, TV
    for (a, b), label in table:
        if a <= div <= b:
            return label
    return "Other services"


NAME_SECTOR = [
    (r"advertis|marketing|\bbrand|creative|communications?\b|\bpr\b|promotions?", "Advertising & market research"),
    (r"media|films?\b|studios?|entertain|events?\b|production", "Media & entertainment"),
    (r"realt|infra|developers?\b|construct|builders?|propert|housing|homes\b|estates?\b", "Real estate"),
    (r"software|tech|digital|infotech|\bit\b|systems|data|cloud|web|cyber|\bai\b|analytics", "IT & software"),
    (r"resorts?|hotels?|hospitality|restaurants?|cafe|foods?\b|kitchen|cater", "Hospitality & food"),
    (r"pharma|health|clinic|hospital|medic|diagnos|wellness|dental", "Healthcare"),
    (r"educat|academy|school|learning|institute|training|\bedu", "Education"),
    (r"consult|advisor|associates|legal|accounta|\btax", "Consulting"),
    (r"logistic|transport|travels?\b|tours?\b|cargo|shipping|courier|movers", "Logistics"),
    (r"design|architect|interior", "Professional services"),
    (r"financ|capital|invest|wealth|fintech|insur|credit", "Finance & insurance"),
    (r"fashion|apparel|garment|textile|clothing", "Textiles & apparel"),
    (r"retail|mart\b|stores?\b|shop", "Retail"),
    (r"trad|exports?|imports?|distribut|enterprises?|suppl", "Wholesale & distribution"),
    (r"engineer|industr|manufactur|auto|machin|electric|steel|plast|metal|fabricat", "Industrial manufacturing"),
    (r"agro|agri|farm|dairy|organic", "Agriculture"),
    (r"solar|energy|power", "Energy & utilities"),
]


SHELL = re.compile(r"holding|investment|finvest|trust|properties|estates?", re.I)


def name_sector(name):
    for pat, label in NAME_SECTOR:
        if re.search(pat, name or "", re.I):
            return label
    return "Other services"


def size_band(paidup, cin):
    """Rough size from paid-up capital (registry has no headcount)."""
    if cin and re.match(r"^[A-Z]{3}-\d+$", cin):
        return "llp"        # LLPs file no share capital
    if cin and cin.startswith("L"):
        return "large"  # listed on a stock exchange
    if paidup is None:
        return "unknown"
    if paidup >= 50e7:
        return "large"      # >= 50 crore
    if paidup >= 1e7:
        return "mid"        # 1 - 50 crore
    if paidup >= 1e6:
        return "small"      # 10 lakh - 1 crore
    return "micro"          # < 10 lakh


def norm_name(s):
    s = re.sub(r"\b(private|pvt|limited|ltd|llp|india|technologies|technology|solutions|services|"
               r"the|inc|corporation|corp|co|company)\b", " ", (s or "").lower())
    return re.sub(r"[^a-z0-9]", "", s)


# ---------- storage ----------

def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    # keep the rollback journal in memory: no temp file created/deleted on every save,
    # which antivirus/indexers on Windows sometimes lock ("disk I/O error")
    con.execute("PRAGMA journal_mode=MEMORY")
    con.execute("""CREATE TABLE IF NOT EXISTS company (
        cin TEXT PRIMARY KEY, name TEXT, status TEXT, class TEXT, category TEXT,
        registered TEXT, paidup REAL, address TEXT, pincode TEXT, email TEXT,
        website TEXT, phones TEXT, emails TEXT, enriched TEXT, enrich_note TEXT)""")
    if "about" not in [r[1] for r in con.execute("PRAGMA table_info(company)")]:
        con.execute("ALTER TABLE company ADD COLUMN about TEXT")
    return con


def pick(row, *names):
    """Field lookup tolerant to the registry's changing column names."""
    keys = {re.sub(r"[^a-z0-9]", "", k.lower()): v for k, v in row.items()}
    for n in names:
        v = keys.get(re.sub(r"[^a-z0-9]", "", n.lower()))
        if v not in (None, "", "NA", "na", "-"):
            return str(v).strip()
    return ""


def to_num(s):
    try:
        return float(re.sub(r"[^\d.]", "", s)) if s else None
    except ValueError:
        return None


def upsert(con, rows):
    n = 0
    for r in rows:
        cin = pick(r, "CORPORATE_IDENTIFICATION_NUMBER", "CompanyCIN", "CIN", "cin")
        addr = pick(r, "REGISTERED_OFFICE_ADDRESS", "Registered_Office_Address", "CompanyRegisteredOfficeAddress",
                    "registeredofficeaddress", "address")
        roc = pick(r, "REGISTRAR_OF_COMPANIES", "CompanyROCcode", "ROC", "roc")
        if not cin or not (PUNE_ADDR.search(addr) or ("pune" in roc.lower() and not addr)):
            continue
        status = pick(r, "COMPANY_STATUS", "CompanyStatus", "status")
        pin = re.findall(r"\b4\d{2}\s?\d{3}\b", addr)
        con.execute("""INSERT INTO company (cin,name,status,class,category,registered,paidup,address,pincode,email)
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cin) DO UPDATE SET name=excluded.name,
            status=excluded.status, class=excluded.class, category=excluded.category, paidup=excluded.paidup,
            address=excluded.address, pincode=excluded.pincode, email=COALESCE(NULLIF(excluded.email,''),email)""",
                    (cin, pick(r, "COMPANY_NAME", "CompanyName", "name"), status,
                     pick(r, "COMPANY_CLASS", "CompanyClass"), pick(r, "COMPANY_CATEGORY", "CompanyCategory"),
                     pick(r, "DATE_OF_REGISTRATION", "CompanyRegistrationdate_date", "CompanyRegistrationDate"),
                     to_num(pick(r, "PAIDUP_CAPITAL", "PaidupCapital", "paidupcapital")),
                     addr, pin[-1].replace(" ", "") if pin else "",
                     pick(r, "EMAIL_ADDR", "EMAIL_ID", "CompanyEmail", "email").lower()))
        n += 1
    for attempt in range(6):
        try:
            con.commit()
            break
        except sqlite3.OperationalError:
            if attempt == 5:
                raise
            time.sleep(2 * (attempt + 1))
    return n


def http(url, data=None, timeout=25, accept="application/json"):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.geturl(), r.headers.get_content_charset() or "utf-8"


def import_api():
    key = os.environ.get("DATA_GOV_API_KEY")
    if not key:
        sys.exit("Set DATA_GOV_API_KEY (free from https://data.gov.in -> My Account -> API key).")
    con, offset, kept, limit = db(), int(os.environ.get("IMPORT_OFFSET", "0")), 0, 1000
    while True:
        q = urllib.parse.urlencode({"api-key": key, "format": "json", "limit": limit, "offset": offset,
                                    "filters[CompanyStateCode]": "maharashtra"})
        for attempt in range(4):
            try:
                body, _, _ = http(f"https://api.data.gov.in/resource/{RESOURCE}?{q}", timeout=90)
                d = json.loads(body)
                break
            except Exception as e:
                if attempt == 3:
                    sys.exit(f"Stopped at offset {offset}: {e}. Resume with IMPORT_OFFSET={offset}")
                time.sleep(10 * (attempt + 1))
        recs = d.get("records", [])
        try:
            kept += upsert(con, recs)
        except sqlite3.OperationalError as e:
            sys.exit(f"Saving failed at offset {offset} ({e}). Run again with IMPORT_OFFSET={offset} to resume.")
        total = int(d.get("total", 0) or 0)
        offset += len(recs)
        print(f"\r  {offset}/{total} Maharashtra records read, {kept} in Pune", end="", flush=True)
        if not recs or offset >= total:
            break
        time.sleep(0.5)
    print()


def import_csv(path):
    con = db()
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        n = upsert(con, csv.DictReader(f))
    print(f"  {n} Pune companies imported from {path}")


# ---------- enrichment: website + public contact details ----------

PHONE_RE = re.compile(r"(?:\+?91[\s\-.]?|0)?(?:\(?0?20\)?[\s\-.]?\d{4}[\s\-.]?\d{4}|[6-9]\d{4}[\s\-.]?\d{5}|"
                      r"1800[\s\-.]?\d{3}[\s\-.]?\d{4})")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
GENERIC = re.compile(r"^(info|contact|hello|hr|careers?|jobs|recruit\w*|talent|marketing|sales|enquir\w*|"
                     r"inquir\w*|support|admin|office|mail|connect|business|team|people|hiring)\b", re.I)
_robots = {}


def allowed(url):
    p = urllib.parse.urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            body, _, cs = http(base + "/robots.txt", timeout=10, accept="text/plain")
            rp.parse(body.decode(cs, "replace").splitlines())
        except Exception:
            rp.parse([])
        _robots[base] = rp
    return _robots[base].can_fetch(UA, url)


def page(url):
    if not allowed(url):
        return None, url
    body, final, cs = http(url, timeout=15, accept="text/html")
    return body[:600_000].decode(cs, "replace"), final


def clean_phone(p):
    d = re.sub(r"\D", "", p)
    if d.startswith("91") and len(d) == 12:
        d = d[2:]
    d = d.lstrip("0") if len(d) == 11 else d
    if len(d) == 10 and d.startswith("20"):
        return f"+91 20 {d[2:6]} {d[6:]}"          # Pune landline
    if len(d) == 10 and d[0] in "6789":
        return f"+91 {d[:5]} {d[5:]}"
    if d.startswith("1800") and len(d) == 11:
        return f"{d[:4]} {d[4:7]} {d[7:]}"
    return None


def plausible(p):
    """Reject digit runs that are not real numbers, e.g. 66666 66667 or 83333 33333."""
    d = re.sub(r"\D", "", p or "")[-10:]
    return bool(p) and len(set(d)) >= 4 and not re.search(r"(\d)\1{4}", d)


CONTEXT = re.compile(r"(phone|tel|call|mob|contact|helpline|toll|whatsapp|ph\b|ph\.|\+91|☎|📞)", re.I)


def contacts(text):
    text = html.unescape(re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I))
    tel = [clean_phone(p) for p in re.findall(r'href=["\']tel:([^"\']+)', text, re.I)]
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))
    near = []
    for m in PHONE_RE.finditer(plain):  # plain-text numbers only count next to words like "phone"/"call"
        before = plain[max(0, m.start() - 50):m.start()]
        if CONTEXT.search(before + m.group(0)) and not re.search(r"fax", before[-20:], re.I):
            near.append(clean_phone(m.group(0)))
    phones = [p for p in dict.fromkeys(tel + near) if p and plausible(p)]
    emails = {e.lower().rstrip(".") for e in EMAIL_RE.findall(text) if GENERIC.match(e)}
    junk = re.compile(r"@(example\d*|domain|yourdomain|mydomain|email|company|sentry|wixpress|test)\.|\.(png|jpe?g|gif|svg|webp)$", re.I)
    return phones, sorted(e for e in emails if not junk.search(e))


def shows_name(text, words):
    flat = re.sub(r"[^a-z0-9]", "", html.unescape(text or "").lower())
    return "".join(words) in flat or (len(words) >= 2 and "".join(words[:2]) in flat and len("".join(words[:2])) >= 6)


def clearbit_site(words):
    """Free company-name -> domain lookup; accept only a suggestion whose name matches ours."""
    try:
        q = urllib.parse.quote(" ".join(words))
        body, _, _ = http(f"https://autocomplete.clearbit.com/v1/companies/suggest?query={q}", timeout=12)
        for s in json.loads(body)[:5]:
            sw = core_words(s.get("name"))
            if sw and sw[-1] == "india":
                sw = sw[:-1]
            if s.get("domain") and (sw == words or (len(words) >= 2 and sw[:2] == words[:2])):
                return "https://" + s["domain"]
    except Exception:
        pass
    return None


def core_words(name):
    s = re.sub(r"\(.*?\)", " ", (name or "").lower())
    s = re.sub(r"\b(private|pvt|limited|ltd|llp|opc|the|m/s)\b", " ", s)
    return re.sub(r"[^a-z0-9 ]", " ", s.replace("&", " and ")).split()


def guess_site(name):
    """Try likely domains for a company and accept one only if its homepage shows the company's name."""
    words = core_words(name)
    if words and words[-1] == "india":
        words = words[:-1]
    key = "".join(words)
    if len(key) < 6:  # too short to verify safely
        return None
    slugs = ["".join(words), "-".join(words), "".join(words) + "india"]
    if len(words) >= 3:
        slugs.append("".join(words[:2]))
    tried = 0
    for slug in dict.fromkeys(slugs):
        for tld in (".com", ".in", ".co.in"):
            if tried >= 8:
                return None
            tried += 1
            url = f"https://www.{slug}{tld}"
            try:
                text, final = page(url)
            except Exception:
                continue
            if text and key in re.sub(r"[^a-z0-9]", "", html.unescape(text).lower()):
                return url, text, final
    return None


def enrich_one(row, deep=False):
    """deep=True (for companies hiring now) also reads careers/jobs pages and common contact URLs."""
    cin, name, email, website = row
    site, home, final = website, None, None
    if not site and email and "@" in email:
        dom = email.split("@")[1].strip()
        if dom not in FREEMAIL and "." in dom:
            site = "https://" + dom
    words = core_words(name)
    if words and words[-1] == "india":
        words = words[:-1]
    looked_up = False
    if not site:
        site = clearbit_site(words) if words else None
        looked_up = bool(site)
    if not site:
        g = guess_site(name)
        if not g:
            return cin, None, [], [], "no website found"
        site, home, final = g
        looked_up = True  # found by name, so it needs the same checks
    try:
        if home is None:
            home, final = page(site)
        if home is None:
            return cin, site, [], [], "robots.txt disallows"
        base = lambda u: ".".join(urllib.parse.urlparse(u).netloc.lower().split(".")[-2:])
        # a redirect to a parent-group site is fine if it names the company; bot-check pages don't
        if base(final) != base(site) and not shows_name(home, words):
            return cin, site, [], [], f"redirected to {urllib.parse.urlparse(final).netloc}"
        host = urllib.parse.urlparse(final).netloc.lower()
        if looked_up and not shows_name(home, words) and words[0] not in host:
            return cin, None, [], [], f"site unverified: {host}"
        # a same-named company abroad (e.g. studiomars.si) is not ours: want an Indian domain or an India mention
        if looked_up and not host.endswith(".in") and \
                not re.search(r"\bindia\b|\bpune\b|maharashtra|\+91|\b0?20[\s-]?\d{4}[\s-]?\d{4}", home, re.I):
            return cin, None, [], [], f"site not Indian: {host}"
        phones, emails = contacts(home)
        pat = r"contact|reach|locat|office" + (r"|career|jobs|join|hiring|work-with" if deep else "")
        links = re.findall(r'href=["\']([^"\'#]*(?:' + pat + r')[^"\'#]*)', home, re.I)
        links = list(dict.fromkeys(links))[:4 if deep else 2]
        if deep and not links:
            links = ["/contact", "/contact-us", "/careers"]
        for link in links:
            url = urllib.parse.urljoin(final, link)
            if urllib.parse.urlparse(url).netloc != urllib.parse.urlparse(final).netloc:
                continue
            try:
                text, _ = page(url)
                if text:
                    p2, e2 = contacts(text)
                    phones += p2
                    emails += e2
            except Exception:
                pass
        root = urllib.parse.urlparse(final)
        ABOUT[cin] = describe(home)
        return cin, f"{root.scheme}://{root.netloc}", list(dict.fromkeys(phones))[:4], \
            sorted(set(emails))[:4], "ok"
    except Exception as e:
        return cin, site, [], [], f"site error: {type(e).__name__}"


ABOUT = {}  # cin -> short description found on the company's homepage
ABOUT_PATTERNS = [
    r"""<meta[^>]+name=["']description["'][^>]*content=["']([^"']{20,600})""",
    r"""<meta[^>]+content=["']([^"']{20,600})["'][^>]*name=["']description""",
    r"""<meta[^>]+property=["']og:description["'][^>]*content=["']([^"']{20,600})""",
    r"""<title[^>]*>([^<]{15,200})</title>""",
]
ABOUT_JUNK = re.compile(r"just another wordpress|coming soon|under construction|index of /|\b40[34]\b|page not found", re.I)


def describe(home):
    """The site's own one-line summary: meta description, og:description, else the page title."""
    for pat in ABOUT_PATTERNS:
        m = re.search(pat, home or "", re.I | re.S)
        if m:
            text = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()
            if not ABOUT_JUNK.search(text):
                return text[:300]
    return ""


def about_one(row):
    cin, site = row
    try:
        home, _ = page(site)
        return cin, describe(home) if home else ""
    except Exception:
        return cin, ""


def backfill_about(limit=2000):
    """Fetch the homepage summary for companies already checked that have a website."""
    con = db()
    rows = con.execute("""SELECT cin, website FROM company WHERE website <> '' AND website IS NOT NULL
        AND about IS NULL LIMIT ?""", (limit,)).fetchall()
    print(f"  reading 'about' text for {len(rows)} company websites…")
    n = 0
    with ThreadPoolExecutor(8) as ex:
        for cin, text in ex.map(about_one, rows):
            con.execute("UPDATE company SET about=? WHERE cin=?", (text, cin))
            n += bool(text)
    con.commit()
    print(f"  done: {n} descriptions found")


def company_sector(cin, name):
    sec = sector(cin, name)
    return name_sector(name) if sec == "Unknown" else sec


def commit(con):
    for attempt in range(6):
        try:
            con.commit()
            return
        except sqlite3.OperationalError:
            if attempt == 5:
                raise
            time.sleep(2 * (attempt + 1))


def enrich(limit=300, industry=None, size_filter=None, max_minutes=None):
    con = db()
    # best marketing prospects first: industry likelihood + size, skipping holding/investment shells
    sizes = {"large": 3, "mid": 2, "small": 1, "micro": 0, "llp": 0, "unknown": 0}
    cand = con.execute("""SELECT cin,name,email,website,paidup FROM company
        WHERE enriched IS NULL AND status LIKE 'activ%'""").fetchall()
    if industry:  # e.g. enrich 700 --industry "Advertising & market research"
        cand = [r for r in cand if company_sector(r[0], r[1]).lower() == industry.lower()]
    if size_filter:  # e.g. enrich 9800 --sizes large,mid,small
        want = {s.strip().lower() for s in size_filter.split(",") if s.strip()}
        cand = [r for r in cand if size_band(r[4], r[0]) in want]
    shell = re.compile(r"holding|investment|finvest|\btrust\b|properties|estates?\b", re.I)
    cand.sort(key=lambda r: (-(MKT_SECTOR.get(sector(r[0], r[1]), 1) + sizes[size_band(r[4], r[0])]
                               - (3 if SHELL.search(r[1] or "") else 0)), -(r[4] or 0)))
    hiring = set(hiring_companies())  # companies with an open marketing job jump the queue
    if hiring:
        cand.sort(key=lambda r: norm_name(r[1]) not in hiring)
    first = sum(1 for r in cand[:limit] if norm_name(r[1]) in hiring)
    rows = [r[:4] for r in cand[:limit]]
    print(f"  enriching {len(rows)} companies ({first} hiring for marketing now, then best marketing prospects)…")
    ts = datetime.now(timezone.utc).isoformat()
    done = found = 0
    t0 = time.time()
    with ThreadPoolExecutor(8) as ex:
        for start in range(0, len(rows), 400):  # save every 400, so a long run never loses its work
            if max_minutes and time.time() - t0 > max_minutes * 60:
                print(f"  time limit of {max_minutes} min reached; the rest will be checked in later runs")
                break
            for cin, site, phones, emails, note in ex.map(enrich_one, rows[start:start + 400]):
                con.execute("UPDATE company SET website=?, phones=?, emails=?, enriched=?, enrich_note=?, about=? "
                            "WHERE cin=?", (site, json.dumps(phones), json.dumps(emails), ts, note,
                                            ABOUT.pop(cin, "") if site else None, cin))
                done += 1
                found += bool(phones or emails)
            commit(con)
            if start and start % 4000 == 0:
                print(f"  … {done} checked, {found} with a phone or email ({(time.time() - t0) / 60:.0f} min)", flush=True)
    print(f"  done: {done} checked, {found} with a phone or email")


def job_key(name):
    """The matching key for a company name from a job feed ("Aarna Systems | Agency" -> "aarnasystems")."""
    return norm_name((name or "").split("|")[0])


def hiring_companies(marketing_only=True):
    """norm_name -> the company name as the job feed spells it, for open (marketing) jobs."""
    try:
        jobs = json.load(open(JOBS, encoding="utf-8"))["jobs"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return {}
    out = {}
    for j in jobs:
        name = (j.get("company") or "").strip()
        if (j.get("marketing") or not marketing_only) and not j.get("closed") and name and name.lower() != "unknown":
            k = job_key(name)
            if len(k) > 3:
                out.setdefault(k, name)
    return out


def job_contacts(recheck_days=14):
    """Find emails for every company with an open job in Pune, registry or not -> web/job-contacts.json.

    Registry companies use their saved contacts; anyone without an email gets a deeper look at
    their website (contact and careers pages). Results are cached for `recheck_days`.
    """
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS job_contact (
        key TEXT PRIMARY KEY, name TEXT, website TEXT, phones TEXT, emails TEXT, note TEXT, checked TEXT)""")
    if "about" not in [r[1] for r in con.execute("PRAGMA table_info(job_contact)")]:
        con.execute("ALTER TABLE job_contact ADD COLUMN about TEXT")
    hiring = hiring_companies(marketing_only=False)
    reg = {}
    for cin, name, site, phones, emails, email in con.execute(
            "SELECT cin, name, website, phones, emails, email FROM company WHERE status LIKE 'activ%'"):
        k = norm_name(name)
        if k in hiring and (k not in reg or (emails or "[]") != "[]"):
            reg[k] = (cin, name, site, json.loads(phones or "[]"), json.loads(emails or "[]"), email)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=recheck_days)).isoformat()
    cached = dict(con.execute("SELECT key, checked FROM job_contact"))
    todo = []
    for k, name in hiring.items():
        r = reg.get(k)
        if r and r[4]:
            continue  # the registry company already has an email
        if cached.get(k, "") >= cutoff:
            continue
        clean = re.sub(r"\s+", " ", name.split("|")[0]).strip()
        todo.append((k, clean, r[5] if r else None, r[2] if r else None))
    print(f"  {len(hiring)} companies hiring in Pune; looking up {len(todo)} of them…")
    ts = datetime.now(timezone.utc).isoformat()
    found = 0
    with ThreadPoolExecutor(8) as ex:
        for k, site, phones, emails, note in ex.map(lambda r: enrich_one(r, deep=True), todo):
            con.execute("INSERT OR REPLACE INTO job_contact (key, name, website, phones, emails, note, checked, about) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (k, hiring[k], site, json.dumps(phones), json.dumps(emails), note, ts, ABOUT.pop(k, "") if site else ""))
            found += bool(emails)
    con.commit()
    print(f"  found an email for {found} of {len(todo)} looked up")
    lookups = {k: (site, json.loads(p or "[]"), json.loads(e or "[]"))
               for k, site, p, e in con.execute("SELECT key, website, phones, emails FROM job_contact")}
    out = {}
    for k, name in hiring.items():
        r = reg.get(k)
        if r and r[4]:
            site, phones, emails = r[2], r[3], r[4]
        else:
            site, phones, emails = lookups.get(k, (None, [], []))
            site, phones = site or (r[2] if r else ""), phones or (r[3] if r else [])
        if emails or phones:
            out[name] = {"emails": emails, "phones": phones, "website": site or ""}
    with open(os.path.join(ROOT, "web", "job-contacts.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    print(f"  wrote web/job-contacts.json: {sum(1 for v in out.values() if v['emails'])} hiring companies with an email")


def set_site(cin, url):
    con = db()
    con.execute("UPDATE company SET website=?, enriched=NULL WHERE cin=?", (url, cin))
    con.commit()


# ---------- export for the dashboard ----------

def build():
    con = db()
    try:
        jobs = json.load(open(JOBS, encoding="utf-8"))["jobs"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        jobs = []
    open_jobs = {}
    for j in jobs:
        if not j.get("closed"):
            open_jobs.setdefault(job_key(j["company"]), []).append(j)
    sizes = {"large": 3, "mid": 2, "small": 1, "micro": 0, "llp": 0, "unknown": 0}
    out, registry_keys = [], set()
    for (cin, name, reg, paidup, addr, pin, email, site, phones, emails, about) in con.execute(
            """SELECT cin, name, registered, paidup, address, pincode, email, website, phones, emails, about
               FROM company WHERE status LIKE 'activ%'"""):
        sec, size = sector(cin, name), size_band(paidup, cin)
        if sec == "Unknown":
            sec = name_sector(name)  # LLPs and foreign companies carry no activity code
        registry_keys.add(norm_name(name))
        js = open_jobs.get(norm_name(name), [])
        mkt_jobs = [j for j in js if j.get("marketing")]
        score = MKT_SECTOR.get(sec, 1) + sizes.get(size, 0) + (4 if mkt_jobs else 0) + (1 if phones not in (None, "", "[]") else 0)
        if SHELL.search(name or ""):
            score -= 3  # holding / investment companies rarely have marketing teams
        if email and "@" in email and email.split("@")[1] in FREEMAIL:
            email = ""  # personal mailboxes from the registry are not shown
        em = json.loads(emails or "[]") or ([email] if email else [])
        out.append([cin, name, size, sec, pin, (reg or "")[:10], paidup or 0, site or "",
                    json.loads(phones or "[]"), em, len(js), len(mkt_jobs), min(score, 10),
                    re.sub(r"\s+", " ", addr or "")[:160], about or ""])
    # companies hiring in Pune whose registered office is elsewhere (or not a company at all, e.g. a firm
    # or startup): they come from the job feeds, with whatever their website told us
    looked = {}
    try:
        for k, site, phones, emails, about in con.execute("SELECT key, website, phones, emails, about FROM job_contact"):
            looked[k] = (site or "", json.loads(phones or "[]"), json.loads(emails or "[]"), about or "")
    except sqlite3.OperationalError:
        pass  # job-contacts hasn't run yet
    extra = 0
    for k, js in open_jobs.items():
        name = re.sub(r"\s+", " ", (js[0].get("company") or "").split("|")[0]).strip()
        if len(k) <= 3 or k in registry_keys or not name or name.lower() in ("unknown", "confidential"):
            continue
        site, phones, emails, about = looked.get(k, ("", [], [], ""))
        mkt_jobs = [j for j in js if j.get("marketing")]
        places = sorted({j.get("location", "") for j in js if j.get("location")})
        out.append([f"job:{k}", name, "unknown", "Hiring now (job listings)", "", "", 0, site, phones, emails,
                    len(js), len(mkt_jobs), min(10, 6 + (3 if mkt_jobs else 0) + (1 if emails else 0)),
                    " / ".join(places)[:160], about])
        extra += 1
    print(f"  added {extra} companies hiring in Pune that aren't registered here")
    out.sort(key=lambda r: (-r[12], -r[6]))
    data = {"updated": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "fields": ["cin", "name", "size", "sector", "pincode", "registered", "paidup", "website",
                       "phones", "emails", "jobs", "mkt_jobs", "fit", "address", "about"],
            "companies": out}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"  wrote {len(out)} active Pune companies to {OUT}")
    # ready-to-use spreadsheet of every company we have a phone number or email for
    rows = [r for r in out if r[8] or r[9]]
    with open(os.path.join(ROOT, "web", "pune-companies-with-contacts.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Company", "What they do", "Industry", "Size", "Phones", "Emails", "Website",
                    "Open marketing jobs", "Pincode", "Registered", "Address", "CIN"])
        for r in rows:
            w.writerow([r[1], r[14], r[3], r[2], " / ".join(r[8]), " / ".join(r[9]), r[7], r[11], r[4],
                        r[5][:4], r[13], r[0]])
    print(f"  wrote {len(rows)} companies with contacts to web/pune-companies-with-contacts.csv")


if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0] if a else ""
    if cmd == "import-api":
        import_api()
    elif cmd == "import-csv" and len(a) > 1:
        import_csv(a[1])
    elif cmd == "enrich":
        ind = a[a.index("--industry") + 1] if "--industry" in a and a.index("--industry") + 1 < len(a) else None
        sz = a[a.index("--sizes") + 1] if "--sizes" in a and a.index("--sizes") + 1 < len(a) else None
        mins = float(a[a.index("--minutes") + 1]) if "--minutes" in a and a.index("--minutes") + 1 < len(a) else None
        nums = [x for x in a[1:] if x.isdigit() and not (mins and x == a[a.index("--minutes") + 1])]
        enrich(int(nums[0]) if nums else 300, ind, sz, mins)
    elif cmd == "site" and len(a) > 2:
        set_site(a[1], a[2])
    elif cmd == "about":
        nums = [x for x in a[1:] if x.isdigit()]
        backfill_about(int(nums[0]) if nums else 2000)
    elif cmd == "job-contacts":
        job_contacts()
    elif cmd == "build":
        build()
    else:
        print(__doc__)
