"""Pakistan Job Pulse: daily market scanner.

Deliberately UNFILTERED: pulls every Pakistan job posting Indeed and LinkedIn
surface for the last 24h, across every sector and seniority, and appends it to
a local SQLite store so hiring trends can be measured over time.

The store stays local. Only aggregated statistics (pulse/stats.py) are
published, never the individual postings.

Usage:
    python -m pulse.scanner scan          # scrape + store + write daily report
    python -m pulse.scanner report        # rebuild the report from the store
    python -m pulse.scanner reclassify    # re-derive sector/seniority/city after rule edits

Set PULSE_DATA to keep the store somewhere other than ./data.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
import traceback
from collections import Counter
from datetime import date, datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("PULSE_DATA") or os.path.join(REPO, "data")
DB_PATH = os.path.join(DATA, "pk_market.db")

# --------------------------------------------------------------------------
# scrape plan
# --------------------------------------------------------------------------
# Indeed is fast (a 650-row national pull returns in ~6s) and effectively
# saturates the national 24h pool, so it is the backbone. The per-city calls
# exist to recover rows the national ranking truncates. LinkedIn caps around
# 100 rows per call and takes ~55s, so it gets few calls.

CITIES = ["Lahore", "Karachi", "Islamabad", "Rawalpindi", "Faisalabad", "Multan", "Peshawar"]

def build_plan():
    plan = [("indeed-national", dict(
        site_name=["indeed"], search_term="", location="Pakistan",
        country_indeed="Pakistan", results_wanted=1000, hours_old=24))]
    for c in CITIES:
        plan.append((f"indeed-{c.lower()}", dict(
            site_name=["indeed"], search_term="", location=c,
            country_indeed="Pakistan", results_wanted=200, hours_old=24)))
    plan.append(("linkedin-national", dict(
        site_name=["linkedin"], search_term="", location="Pakistan",
        results_wanted=200, hours_old=24, linkedin_fetch_description=False)))
    for c in ["Lahore", "Karachi"]:
        plan.append((f"linkedin-{c.lower()}", dict(
            site_name=["linkedin"], search_term="", location=c,
            results_wanted=100, hours_old=24, linkedin_fetch_description=False)))
    return plan

# --------------------------------------------------------------------------
# derived fields
# --------------------------------------------------------------------------
# Indeed PK returns job_level and company_industry almost entirely empty
# (0/657 and 10/657 on a representative pull), so sector and seniority are
# derived from the title instead. Salary is 0/657 - not tracked.

CITY_ALIASES = {
    "karachi": "Karachi", "lahore": "Lahore", "islamabad": "Islamabad",
    "rawalpindi": "Rawalpindi", "faisalabad": "Faisalabad", "multan": "Multan",
    "peshawar": "Peshawar", "quetta": "Quetta", "sialkot": "Sialkot",
    "gujranwala": "Gujranwala", "hyderabad": "Hyderabad", "abbottabad": "Abbottabad",
    "sargodha": "Sargodha", "bahawalpur": "Bahawalpur", "sukkur": "Sukkur",
    "gujrat": "Gujrat", "mardan": "Mardan", "wah": "Wah Cantt",
    "sahiwal": "Sahiwal", "okara": "Okara", "jhelum": "Jhelum",
    "rahim yar khan": "Rahim Yar Khan", "dera ghazi khan": "Dera Ghazi Khan",
}

# Districts/suburbs that arrive without their parent city attached.
DISTRICT_TO_CITY = {
    "korangi": "Karachi", "malir": "Karachi", "landhi": "Karachi",
    "gulshan": "Karachi", "nazimabad": "Karachi", "clifton": "Karachi",
    "saddar": "Karachi", "kotri": "Hyderabad",
    "johar town": "Lahore", "bahria town": "Lahore", "gulberg": "Lahore",
    "defence": "Lahore", "model town": "Lahore",
}

# Province names / postal codes that Indeed sometimes returns in the city slot.
NOT_A_CITY = {"pk", "pakistan", "punjab", "sindh", "balochistan", "kpk",
              "khyber pakhtunkhwa", "psd", "ppb", "pis", "pbl", "pkp", ""}

def _strip_accents(s: str) -> str:
    """LinkedIn returns 'Karāchi'/'Islāmābād'; fold to ASCII so they don't
    split into separate cities from the Indeed spellings."""
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(ch))

def normalize_city(location: str) -> str:
    """'Lahore Bahria Town, PPB, PK' -> 'Lahore'."""
    if not location:
        return "Unspecified"
    head = _strip_accents(location.split(",")[0]).strip().lower()
    head = re.sub(r"\b(gpo|cantt|cantonment|division|district|city|tehsil)\b", "", head).strip()
    head = re.sub(r"\s+", " ", head)
    if head in NOT_A_CITY:
        return "Unspecified"
    for alias, canon in CITY_ALIASES.items():
        if head == alias or head.startswith(alias + " ") or (" " + alias) in (" " + head):
            return canon
    for alias, canon in DISTRICT_TO_CITY.items():
        if head == alias or alias in head:
            return canon
    return head.title() if head else "Unspecified"

# ordered: first match wins, so the specific patterns sit above the generic ones
SECTOR_RULES = [
    ("Data & AI", r"data scien|machine learning|\bml engineer|\bai engineer|artificial intelligence|deep learning|\bnlp\b|computer vision|\bllm\b|data analyst|data engineer|analytics engineer|data model|data governance|data warehouse|business intelligence|power ?bi|tableau|\bmlops\b|data architect|big data|statistician|\betl\b"),
    ("Software & IT", r"software|developer|full[- ]?stack|front[- ]?end|back[- ]?end|devops|\bqa\b|\bsqa\b|test engineer|\.net|\bjava\b|python|\bphp\b|laravel|react|node|angular|android|\bios\b|mobile app|web dev|\bcloud\b|azure|\baws\b|\bsre\b|sysadmin|system admin|network engineer|it support|\bit\b (specialist|head|manager|executive|officer|infrastructure|admin)|information technology|cyber ?security|information security|database admin|\bdba\b|wordpress|shopify|salesforce|\berp\b|odoo|\bsap\b|sharepoint|programmer|tech(nical)? lead|solution architect|\biam\b|sailpoint|technical (consultant|architect|product owner)|scrum|automation engineer"),
    ("Design & Creative", r"graphic design|ui/?ux|\bui\b design|\bux\b|video edit|animator|animation|illustrat|motion graphic|3d artist|creative design|photographer|visual design|fashion design|product design|content creator|\bdesigner\b"),
    ("Healthcare & Pharma", r"\bdoctor\b|\bnurse\b|medical|pharmac|physiotherap|dental|dentist|clinical|lab technician|radiolog|sonolog|ultrasound|surgeon|physician|healthcare|technologist|\bregistrar\b|patient|\bward\b"),
    ("Education", r"teacher|lecturer|professor|\btutor\b|instructor|academic|principal|educat|curriculum|montessori|coaching|counsel(l)?or|admission|school|campus"),
    ("Finance & Accounting", r"account(ant|s|ing)|finance|financial|audit|taxation|\btax\b|bookkeep|treasury|cashier|billing|payroll|invoic|credit analyst|budget|\bsox\b|recovery (officer|manager)|collection officer"),
    ("HR & Admin", r"\bhr\b|human resource|recruit|talent acquisition|\badmin\b|administrative|office assistant|receptionist|secretary|front desk|personal assistant|office boy|data entry|\bcoordinator\b|protocol officer|protocal officer"),
    ("Sales & Business Development", r"\bsales?\b|business development|\bbdm\b|\bbde\b|account executive|account manager|upwork|bidder|lead gen|telesale|appointment setter|cold call|closers?\b|verifiers?\b|relationship manager|territory|retail|\bmedia buyer\b|client success|customer success"),
    ("Marketing", r"marketing|marketer|\bseo\b|search engine optimi|\bppc\b|social media|content writer|copywriter|\bbrand\b|advertis|campaign|\bpr\b executive|community manager|influencer|\bmedia\b"),
    ("Customer Support & BPO", r"customer (support|service|care)|\bcsrs?\b|call cent|\bbpo\b|help ?desk|live chat|virtual assistant|technical support|inbound|outbound|calls? agent|\bagent\b"),
    ("Logistics & Supply Chain", r"supply chain|logistic|procure|warehouse|inventory|\bdriver\b|dispatch|import|export|freight|shipping|\brider\b|delivery|store ?keeper|\bsupply man\b"),
    ("Engineering & Manufacturing", r"mechanical|electrical|civil engineer|production|quality control|\bqc\b|\bqa/qc\b|maintenance|technician|industrial|chemical|textile|garment|embroider|draftsman|instrumentation|\bhvac\b|\bmep\b|\bcnc\b|lathe|milling|machine operator|welder|fabricat|site (engineer|supervisor)|surveyor|solar|electrician|polymer|planning engineer|proposal engineer|\bdae\b|pattern master|\br&d\b|research (and|&) development|\bips\b"),
    ("Construction & Real Estate", r"real estate|property|\barchitect\b|construction|interior design|\bbuilder\b|civil works"),
    ("Legal", r"\blegal\b|lawyer|advocate|paralegal|compliance officer|litigation|\bcounsel\b"),
    ("Hospitality & Food", r"chef|\bcook\b|barista|waiter|restaurant|\bhotel\b|kitchen|catering|housekeep|\bbaker\b|caf[eé]|fitness trainer|\bgym\b"),
    ("Security & Facilities", r"security (guard|supervisor|officer)|\bguards?\b|janitor|cleaner|\bcustodian\b|facilities"),
    ("Management & Operations", r"operations|project (manager|coordinator|consultant)|program manager|general manager|\bceo\b|\bcoo\b|\bcto\b|managing director|branch manager|\bdirector\b|business analyst|process (improvement|and controls)|strategy|product (manager|owner)|management trainee|\bmto\b|\bsupervisor\b|\bmanager\b"),
]
SECTOR_COMPILED = [(name, re.compile(pat, re.I)) for name, pat in SECTOR_RULES]

def classify_sector(title: str) -> str:
    t = title or ""
    for name, rx in SECTOR_COMPILED:
        if rx.search(t):
            return name
    return "Other / Unclassified"

SENIORITY_RULES = [
    ("Intern / Trainee", r"\bintern\b|internship|trainee|fresher|apprentice|\bgraduate program"),
    ("Head / Exec", r"head of|\bvp\b|vice president|\bchief\b|\bcto\b|\bceo\b|\bcoo\b|\bcfo\b|managing director|\bdirector\b|principal\b"),
    ("Manager", r"\bmanager\b|\bsupervisor\b|\blead\b|\bteam lead\b|\bforeman\b"),
    ("Senior", r"\bsenior\b|\bsr\.?\b|\bstaff\b|\bexpert\b|\bspecialist iii\b"),
    ("Junior / Associate", r"\bjunior\b|\bjr\.?\b|\bassociate\b|entry.level|\bassistant\b|\btrainee\b"),
]
SENIORITY_COMPILED = [(name, re.compile(pat, re.I)) for name, pat in SENIORITY_RULES]

def classify_seniority(title: str) -> str:
    t = title or ""
    for name, rx in SENIORITY_COMPILED:
        if rx.search(t):
            return name
    return "Mid / Unspecified"

def dedup_key(company: str, title: str, city: str) -> str:
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())
    return f"{norm(company)}|{norm(title)}|{norm(city)}"

# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    job_url        TEXT PRIMARY KEY,
    site           TEXT,
    title          TEXT,
    company        TEXT,
    location_raw   TEXT,
    city           TEXT,
    sector         TEXT,
    seniority      TEXT,
    job_type       TEXT,
    is_remote      INTEGER,
    date_posted    TEXT,
    company_size   TEXT,
    company_url    TEXT,
    dedup_key      TEXT,
    first_seen     TEXT,
    last_seen      TEXT,
    times_seen     INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_first_seen ON postings(first_seen);
CREATE INDEX IF NOT EXISTS idx_company    ON postings(company);
CREATE INDEX IF NOT EXISTS idx_sector     ON postings(sector);
CREATE INDEX IF NOT EXISTS idx_dedup      ON postings(dedup_key);

CREATE TABLE IF NOT EXISTS scans (
    scan_date   TEXT PRIMARY KEY,
    ran_at      TEXT,
    rows_seen   INTEGER,
    rows_new    INTEGER,
    calls_ok    INTEGER,
    calls_fail  INTEGER,
    duration_s  REAL,
    notes       TEXT
);
"""

def connect():
    os.makedirs(DATA, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con

def upsert(con, rows, today: str):
    """Insert new postings; bump last_seen/times_seen for ones already known."""
    new = 0
    for r in rows:
        cur = con.execute("SELECT 1 FROM postings WHERE job_url = ?", (r["job_url"],))
        if cur.fetchone():
            con.execute(
                "UPDATE postings SET last_seen = ?, times_seen = times_seen + 1 WHERE job_url = ?",
                (today, r["job_url"]))
        else:
            con.execute("""INSERT INTO postings
                (job_url, site, title, company, location_raw, city, sector, seniority,
                 job_type, is_remote, date_posted, company_size, company_url, dedup_key,
                 first_seen, last_seen, times_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (r["job_url"], r["site"], r["title"], r["company"], r["location_raw"],
                 r["city"], r["sector"], r["seniority"], r["job_type"], r["is_remote"],
                 r["date_posted"], r["company_size"], r["company_url"], r["dedup_key"],
                 today, today))
            new += 1
    con.commit()
    return new

# --------------------------------------------------------------------------
# scrape
# --------------------------------------------------------------------------
def scrape(log):
    from jobspy import scrape_jobs
    import pandas as pd

    frames, ok, fail = [], 0, 0
    plan = build_plan()
    for i, (name, kw) in enumerate(plan, 1):
        log(f"[{i}/{len(plan)}] {name}")
        try:
            df = scrape_jobs(**kw)
            n = 0 if df is None else len(df)
            log(f"    -> {n} rows")
            if n:
                frames.append(df)
            ok += 1
        except Exception as e:
            log(f"    !! FAILED {type(e).__name__}: {str(e)[:200]}")
            fail += 1
        if i < len(plan):
            time.sleep(6)

    if not frames:
        return [], ok, fail

    allc = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["job_url"])
    log(f"unique after dedupe: {len(allc)}")

    def g(row, col):
        v = row.get(col)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        return str(v)

    rows = []
    for _, row in allc.iterrows():
        title = g(row, "title")
        loc = g(row, "location")
        city = normalize_city(loc)
        company = g(row, "company")
        rem = row.get("is_remote")
        rows.append(dict(
            job_url=g(row, "job_url"), site=g(row, "site"), title=title, company=company,
            location_raw=loc, city=city, sector=classify_sector(title),
            seniority=classify_seniority(title), job_type=g(row, "job_type"),
            is_remote=1 if (rem is True or str(rem).lower() == "true") else 0,
            date_posted=g(row, "date_posted"), company_size=g(row, "company_num_employees"),
            company_url=g(row, "company_url"), dedup_key=dedup_key(company, title, city),
        ))
    return rows, ok, fail

# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def _rows(con, sql, args=()):
    return con.execute(sql, args).fetchall()

def _tbl(headers, rows):
    if not rows:
        return "_No data._\n"
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x).replace("|", "\\|") for x in r) + " |")
    return "\n".join(out) + "\n"

def _pct(now, prev):
    if prev == 0:
        return "new" if now else "-"
    return f"{(now - prev) / prev * 100:+.0f}%"

def _share(n, total):
    if not total:
        return "-"
    return f"{n / total * 100:.1f}%"

def build_report(con, today: str) -> str:
    d0 = datetime.strptime(today, "%Y-%m-%d").date()
    w1_start = (d0 - timedelta(days=6)).isoformat()      # last 7d incl today
    w2_start = (d0 - timedelta(days=13)).isoformat()     # prior 7d
    w2_end = (d0 - timedelta(days=7)).isoformat()

    total = _rows(con, "SELECT COUNT(*) FROM postings")[0][0]
    new_today = _rows(con, "SELECT COUNT(*) FROM postings WHERE first_seen = ?", (today,))[0][0]
    companies_today = _rows(con, "SELECT COUNT(DISTINCT company) FROM postings WHERE first_seen = ? AND company != ''", (today,))[0][0]
    hist_days = _rows(con, "SELECT COUNT(*) FROM scans")[0][0]

    w1 = _rows(con, "SELECT COUNT(*) FROM postings WHERE first_seen >= ?", (w1_start,))[0][0]
    w2 = _rows(con, "SELECT COUNT(*) FROM postings WHERE first_seen >= ? AND first_seen <= ?", (w2_start, w2_end))[0][0]

    o = []
    o.append(f"# Pakistan job market scan - {today}\n")
    o.append(f"\n_Unfiltered daily sweep of Indeed + LinkedIn Pakistan postings, all sectors. "
             f"Tracking dataset for hiring trends, not a shortlist. Generated "
             f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}._\n")
    o.append(f"\n**{new_today} new postings today** from **{companies_today} companies**. "
             f"Store now holds **{total}** unique postings across **{hist_days}** scan day(s).\n")

    if hist_days < 2:
        o.append("\n> First scan day. Week-over-week and company-growth comparisons need at "
                 "least 2 scan days, and become meaningful at ~2 weeks. Today establishes the baseline.\n")
    else:
        o.append(f"\n**Last 7 days: {w1} postings** vs **{w2}** the previous 7 days ({_pct(w1, w2)}).\n")

    # --- bulk-posting / trending call-out, surfaced up top so it's not buried in tables ---
    o.append("\n## 🔥 Bulk-posting & trending signals (today)\n\n")
    bulk = _rows(con, """SELECT company, COUNT(*) n FROM postings
        WHERE first_seen = ? AND company != '' GROUP BY company
        HAVING n >= 5 ORDER BY n DESC LIMIT 10""", (today,))
    if bulk:
        o.append("**Posting in bulk today** (5+ new listings from one company - often a recruiter/agency "
                  "account rather than a single hiring team; worth a look before assuming 5 separate roles):\n\n")
        for c, n in bulk:
            o.append(f"- **{c}** - {n} new postings today\n")
    else:
        o.append("_No single company posted 5+ new listings today._\n")

    if hist_days >= 2:
        accel = _rows(con, """SELECT company,
            SUM(first_seen >= ?) a, SUM(first_seen >= ? AND first_seen <= ?) b
            FROM postings WHERE company != '' GROUP BY company
            HAVING a > b AND a >= 3 ORDER BY (a - b) DESC LIMIT 5""",
            (w1_start, w2_start, w2_end))
        if accel:
            o.append("\n**Accelerating this week** (7d vs prior 7d, top 5 - see full table further down):\n\n")
            for c, a, b in accel:
                o.append(f"- **{c}** - {a} last 7d vs {b} prior 7d ({_pct(a, b)})\n")

    top_titles = _rows(con, """SELECT title, COUNT(*) n FROM postings
        WHERE first_seen = ? AND title != '' GROUP BY title
        HAVING n >= 2 ORDER BY n DESC LIMIT 8""", (today,))
    if top_titles:
        o.append("\n**Same exact title posted more than once today** (repeat postings can mean either "
                  "real bulk hiring or a templated/lead-gen listing - worth a quick sanity check on the company):\n\n")
        for t, n in top_titles:
            o.append(f"- \"{t}\" x{n}\n")

    o.append("\n---\n\n## Sector mix\n\n")
    cur = _rows(con, """SELECT sector,
        SUM(first_seen = ?) AS today,
        SUM(first_seen >= ?) AS w1,
        SUM(first_seen >= ? AND first_seen <= ?) AS w2
        FROM postings GROUP BY sector ORDER BY today DESC, w1 DESC""",
        (today, w1_start, w2_start, w2_end))
    rows = [(s, t, _share(t, new_today), a, b, _pct(a, b)) for s, t, a, b in cur]
    o.append(_tbl(["Sector", "New today", "% of today", "Last 7d", "Prior 7d", "WoW"], rows))

    o.append("\n## Job categories — local vs remote (today)\n\n")
    o.append("_Same sector breakdown as above, split by whether the posting is remote-flagged, "
              "so you can see which categories are locally-tied vs open remotely._\n\n")
    cur = _rows(con, """SELECT sector,
        SUM(CASE WHEN is_remote THEN 1 ELSE 0 END) AS remote_n,
        SUM(CASE WHEN is_remote THEN 0 ELSE 1 END) AS local_n,
        COUNT(*) AS total_n
        FROM postings WHERE first_seen = ? GROUP BY sector
        HAVING total_n > 0 ORDER BY total_n DESC""", (today,))
    rows = [(s, loc, rem, tot, _share(rem, tot)) for s, rem, loc, tot in cur]
    o.append(_tbl(["Sector", "Local (today)", "Remote (today)", "Total (today)", "% remote"], rows))

    o.append("\n## Cities\n\n")
    cur = _rows(con, """SELECT city,
        SUM(first_seen = ?) AS today,
        SUM(first_seen >= ?) AS w1,
        SUM(first_seen >= ? AND first_seen <= ?) AS w2
        FROM postings GROUP BY city ORDER BY today DESC, w1 DESC LIMIT 15""",
        (today, w1_start, w2_start, w2_end))
    rows = [(c, t, a, b, _pct(a, b)) for c, t, a, b in cur]
    o.append(_tbl(["City", "New today", "Last 7d", "Prior 7d", "WoW"], rows))

    o.append("\n## Top hiring companies today\n\n")
    cur = _rows(con, """SELECT company, COUNT(*) n,
        (SELECT COUNT(*) FROM postings p2 WHERE p2.company = p1.company AND p2.first_seen >= ?) w1
        FROM postings p1 WHERE first_seen = ? AND company != ''
        GROUP BY company ORDER BY n DESC, company LIMIT 20""", (w1_start, today))
    rows = [(c, n, _share(n, new_today), w1n) for c, n, w1n in cur]
    o.append(_tbl(["Company", "New today", "% of today", "Last 7d"], rows))

    o.append("\n## Top job titles today\n\n")
    o.append("_Exact-title matches, so this understates true demand for a role (companies phrase "
              "the same opening differently) - use it to spot templated postings, not as a full count._\n\n")
    cur = _rows(con, """SELECT title, COUNT(*) n FROM postings
        WHERE first_seen = ? AND title != '' GROUP BY title
        ORDER BY n DESC, title LIMIT 15""", (today,))
    rows = [(t, n, _share(n, new_today)) for t, n in cur]
    o.append(_tbl(["Title", "New today", "% of today"], rows))

    o.append("\n## Companies posting for the first time\n")
    if hist_days < 2:
        o.append("\n_Not meaningful yet: on the first scan day every company is new to the "
                 "dataset, so this would just restate the table above. It becomes a real "
                 "signal from the second scan onward._\n")
    else:
        o.append("\nCompanies whose first-ever posting in this dataset appeared today. "
                 "Over time this is the clearest signal of new or newly-scaling employers. "
                 "Sector and city show each company's dominant value, not a single listing.\n\n")
        cur = _rows(con, """SELECT p.company, COUNT(*) n,
            (SELECT sector FROM postings s WHERE s.company = p.company
             GROUP BY sector ORDER BY COUNT(*) DESC LIMIT 1),
            (SELECT city FROM postings s WHERE s.company = p.company
             GROUP BY city ORDER BY COUNT(*) DESC LIMIT 1)
            FROM postings p
            WHERE p.company != '' AND p.company NOT IN (
                SELECT DISTINCT company FROM postings WHERE first_seen < ?)
            AND p.first_seen = ?
            GROUP BY p.company ORDER BY n DESC, p.company LIMIT 25""", (today, today))
        o.append(_tbl(["Company", "Postings today", "Dominant sector", "Main city"], cur))

    if hist_days >= 2:
        o.append("\n## Accelerating employers (7d vs prior 7d)\n\n")
        cur = _rows(con, """SELECT company,
            SUM(first_seen >= ?) a, SUM(first_seen >= ? AND first_seen <= ?) b
            FROM postings WHERE company != '' GROUP BY company
            HAVING a > b AND a >= 3 ORDER BY (a - b) DESC LIMIT 20""",
            (w1_start, w2_start, w2_end))
        rows = [(c, a, b, _pct(a, b)) for c, a, b in cur]
        o.append(_tbl(["Company", "Last 7d", "Prior 7d", "Change"], rows))

    o.append("\n## Seniority mix (new today)\n\n")
    cur = _rows(con, """SELECT seniority, COUNT(*) FROM postings WHERE first_seen = ?
        GROUP BY seniority ORDER BY COUNT(*) DESC""", (today,))
    o.append(_tbl(["Seniority", "New today"], cur))

    o.append("\n## Work arrangement (new today)\n\n")
    rem = _rows(con, "SELECT SUM(is_remote), COUNT(*) FROM postings WHERE first_seen = ?", (today,))[0]
    r_n, r_t = (rem[0] or 0), (rem[1] or 0)
    share = f"{r_n / r_t * 100:.1f}%" if r_t else "-"
    o.append(f"Remote-flagged: **{r_n} / {r_t}** ({share}) of today's new postings.\n\n")
    cur = _rows(con, """SELECT CASE WHEN job_type='' THEN 'unspecified' ELSE job_type END, COUNT(*)
        FROM postings WHERE first_seen = ? GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 10""", (today,))
    o.append(_tbl(["Job type", "New today"], cur))

    o.append("\n## Data & AI subset\n")
    o.append("\nToday's data and AI postings, listed for context.\n\n")
    cur = _rows(con, """SELECT company, title, city, site FROM postings
        WHERE first_seen = ? AND sector = 'Data & AI' ORDER BY company LIMIT 40""", (today,))
    o.append(_tbl(["Company", "Title", "City", "Source"], cur))

    o.append("\n---\n")
    o.append(f"\n_Store: `data/pk_market.db`. Salary is not tracked - Indeed PK returns it "
             f"empty on essentially every row. Sector and seniority are derived from job titles, "
             f"since the native `job_level` and `company_industry` fields are also empty._\n")
    return "".join(o)

# --------------------------------------------------------------------------
def reclassify(con, log):
    """Re-derive city/sector/seniority for every stored row.

    The classifiers are keyword rules that get sharpened over time; without
    this, old rows keep their old labels and the trend lines break at the
    point the rules changed. Run after any edit to the rule tables.
    """
    rows = con.execute("SELECT job_url, title, company, location_raw, city, sector, seniority "
                       "FROM postings").fetchall()
    changed = 0
    for url, title, company, loc, old_city, old_sec, old_sen in rows:
        city = normalize_city(loc)
        sec = classify_sector(title)
        sen = classify_seniority(title)
        if (city, sec, sen) == (old_city, old_sec, old_sen):
            continue
        con.execute("UPDATE postings SET city=?, sector=?, seniority=?, dedup_key=? WHERE job_url=?",
                    (city, sec, sen, dedup_key(company, title, city), url))
        changed += 1
    con.commit()
    log(f"reclassified {changed} of {len(rows)} rows")
    return changed


def main():
    ap = argparse.ArgumentParser(description="Pakistan-wide job market scan")
    ap.add_argument("cmd", choices=["scan", "report", "reclassify"], nargs="?", default="scan")
    ap.add_argument("--date", default=None, help="override scan date (YYYY-MM-DD)")
    args = ap.parse_args()

    today = args.date or date.today().isoformat()
    logpath = os.path.join(DATA, "pk_market_scan.log")
    lf = open(logpath, "w", encoding="utf-8")
    def log(m):
        lf.write(str(m) + "\n"); lf.flush()

    con = connect()
    t0 = time.time()

    if args.cmd == "scan":
        log(f"=== pk market scan {today} ===")
        rows, ok, fail = scrape(log)
        new = upsert(con, rows, today) if rows else 0
        dur = time.time() - t0
        con.execute("""INSERT INTO scans (scan_date, ran_at, rows_seen, rows_new, calls_ok,
            calls_fail, duration_s, notes) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(scan_date) DO UPDATE SET ran_at=excluded.ran_at,
            rows_seen=excluded.rows_seen, rows_new=excluded.rows_new,
            calls_ok=excluded.calls_ok, calls_fail=excluded.calls_fail,
            duration_s=excluded.duration_s""",
            (today, datetime.now(timezone.utc).isoformat(), len(rows), new, ok, fail, dur, ""))
        con.commit()
        log(f"seen={len(rows)} new={new} ok={ok} fail={fail} dur={dur:.1f}s")

    elif args.cmd == "reclassify":
        reclassify(con, log)

    md = build_report(con, today)
    out = os.path.join(DATA, f"pk_market_{today}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    log(f"report -> {out}")

    sys.stdout = __import__("io").TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print(f"pk_market {args.cmd} done: {out}")
    lf.close()

if __name__ == "__main__":
    main()
