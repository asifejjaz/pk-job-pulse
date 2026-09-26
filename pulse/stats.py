"""Build the public statistics file from the local posting store.

Only aggregates leave this script: counts, shares and rankings. No job URLs,
no individual postings. That keeps the live site a market statistics tool and
not a job board.

Usage:
    python -m pulse.stats                         # reads $PULSE_DATA/pk_market.db
    python -m pulse.stats --db path/to/pk_market.db --out site/data/stats.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from pulse.scanner import DB_PATH, REPO

TECH_SECTORS = {"Software & IT", "Data & AI"}
SENIOR_LEVELS = {"Senior", "Manager", "Head / Exec"}
ENTRY_LEVELS = {"Junior / Associate", "Intern / Trainee"}
AI_TITLE = re.compile(r"\bai\b|artificial intelligence|gen ?ai|\bllm\b|machine learning|agentic", re.I)

# Accounts that post on behalf of other employers. Their volume says little about
# who is actually hiring, so they get their own list instead of topping the employer
# ranking. Name rules plus accounts seen posting templated listings with no employer.
RECRUITER_NAME = re.compile(
    r"recruit|staffing|\bhr\b|hiring|talent|placement|manpower|headhunt|\bjobs?\b"
    r"|resourcing|workforce|outsourc|employment|\bhire\b", re.I)
RECRUITER_KNOWN = {"hired", "hire feed", "jobs ai", "quik hire staffing", "recruit ai",
                   "ai job fever", "jobright ai", "crossing hurdles", "jack jill", "jack & jill",
                   "three percent", "bebee professionals", "hr pod careers"}

COMPANY_SUFFIX = re.compile(
    r"\b(pvt|private|ltd|limited|llc|inc|smc|co|company|corp|corporation|plc|gmbh|pakistan|pk)\b")

WINDOWS = {"7d": 7, "30d": 30, "all": None}


def company_key(name: str) -> str:
    k = re.sub(r"[^a-z0-9& ]+", " ", (name or "").lower())
    k = COMPANY_SUFFIX.sub(" ", k)
    return re.sub(r"\s+", " ", k).strip()


def is_recruiter(name: str) -> bool:
    k = company_key(name)
    return k in RECRUITER_KNOWN or bool(RECRUITER_NAME.search(name or ""))


def load(db: str):
    con = sqlite3.connect(db)
    rows = con.execute("""SELECT title, company, city, sector, seniority, is_remote,
                                 first_seen, dedup_key, job_type
                          FROM postings ORDER BY first_seen, rowid""").fetchall()
    scans = {r[0]: dict(rows_seen=r[1], calls_ok=r[2], calls_fail=r[3])
             for r in con.execute("SELECT scan_date, rows_seen, calls_ok, calls_fail FROM scans")}
    # The same opening often appears on Indeed and LinkedIn: keep the first sighting only.
    seen, out = set(), []
    for t, co, city, sec, sen, rem, fs, dk, jt in rows:
        k = dk or f"{co}|{t}|{city}"
        if k in seen:
            continue
        seen.add(k)
        out.append(dict(title=t or "", company=(co or "").strip(), city=city or "Unspecified",
                        sector=sec, seniority=sen, remote=bool(rem), day=fs, job_type=jt or ""))
    return out, scans


def pct(n, d):
    return round(100.0 * n / d, 1) if d else 0.0


def per_day_change(rows, days, end: date):
    """Per scan day volume, last 7 calendar days vs the 7 before.

    Normalizing by scan days matters: skipped scan days otherwise read as a crash."""
    def window(a, b):
        sel = [r for r in rows if a <= r["day"] <= b]
        nd = len({d for d in days if a <= d <= b})
        return (len(sel) / nd if nd else None), nd
    cur, n1 = window((end - timedelta(days=6)).isoformat(), end.isoformat())
    prev, n2 = window((end - timedelta(days=13)).isoformat(), (end - timedelta(days=7)).isoformat())
    if not cur or not prev:
        return None
    return round(100.0 * (cur - prev) / prev, 1)


def top_titles(rows, n=6):
    c = Counter(re.sub(r"\s+", " ", r["title"].strip().lower()) for r in rows if r["title"])
    return [[t, k] for t, k in c.most_common(n)]


def company_table(rows, limit, key="posts", min_remote=0):
    by = defaultdict(list)
    for r in rows:
        if r["company"]:
            by[company_key(r["company"])].append(r)
    out = []
    for k, rs in by.items():
        name = Counter(r["company"] for r in rs).most_common(1)[0][0]
        if not k or is_recruiter(name):
            continue
        remote = sum(r["remote"] for r in rs)
        if remote < min_remote:
            continue
        sec = Counter(r["sector"] for r in rs).most_common(1)[0][0]
        out.append(dict(name=name, posts=len(rs), remote=remote,
                        tech=sum(r["sector"] in TECH_SECTORS for r in rs),
                        sector=sec, days=len({r["day"] for r in rs}),
                        cities=[c for c, _ in Counter(r["city"] for r in rs).most_common(2)]))
    out.sort(key=lambda d: (-d[key], -d["posts"], d["name"]))
    return out[:limit]


def recruiter_table(rows, limit=15):
    c = Counter()
    names = {}
    for r in rows:
        if r["company"] and is_recruiter(r["company"]):
            k = company_key(r["company"])
            c[k] += 1
            names.setdefault(k, r["company"])
    return [dict(name=names[k], posts=n) for k, n in c.most_common(limit)]


def sector_block(rows, days, end, all_rows, all_days):
    by = defaultdict(list)
    for r in rows:
        by[r["sector"]].append(r)
    total = len(rows)
    out = []
    for sec, rs in by.items():
        n = len(rs)
        out.append(dict(
            sector=sec, posts=n, share=pct(n, total),
            per_day=round(n / max(1, len({r['day'] for r in rows})), 1),
            entry=pct(sum(r["seniority"] in ENTRY_LEVELS for r in rs), n),
            senior=pct(sum(r["seniority"] in SENIOR_LEVELS for r in rs), n),
            remote=pct(sum(r["remote"] for r in rs), n),
            ai=pct(sum(bool(AI_TITLE.search(r["title"])) for r in rs), n),
            change=per_day_change([r for r in all_rows if r["sector"] == sec], all_days, end),
            titles=top_titles(rs, 6),
            companies=[[d["name"], d["posts"]] for d in company_table(rs, 6)],
            cities=[[c, k] for c, k in Counter(r["city"] for r in rs).most_common(5)],
            seniority=[[s, k] for s, k in Counter(r["seniority"] for r in rs).most_common()],
        ))
    out.sort(key=lambda d: -d["posts"])
    return out


def window_stats(rows, days, end, n_days):
    # Week over week change always compares the latest 7 days with the 7 before,
    # so it needs the full history even when the window itself is shorter.
    all_rows, all_days = rows, days
    if n_days:
        start = (end - timedelta(days=n_days - 1)).isoformat()
        rows = [r for r in rows if r["day"] >= start]
        days = [d for d in days if d >= start]
    total = len(rows)
    first_seen_company = {}
    for r in rows:
        first_seen_company.setdefault(company_key(r["company"]), r["day"])
    cities = Counter(r["city"] for r in rows)
    city_rows = []
    for c, n in cities.most_common(12):
        rs = [r for r in rows if r["city"] == c]
        city_rows.append(dict(city=c, posts=n, share=pct(n, total),
                              tech=pct(sum(r["sector"] in TECH_SECTORS for r in rs), n),
                              remote=pct(sum(r["remote"] for r in rs), n)))
    ai_rows = [r for r in rows if AI_TITLE.search(r["title"])]
    return dict(
        posts=total,
        companies=len({company_key(r["company"]) for r in rows if r["company"]}),
        scan_days=len(set(days)),
        per_day=round(total / max(1, len(set(days))), 1),
        remote=pct(sum(r["remote"] for r in rows), total),
        tech=pct(sum(r["sector"] in TECH_SECTORS for r in rows), total),
        entry=pct(sum(r["seniority"] in ENTRY_LEVELS for r in rows), total),
        change=per_day_change(all_rows, all_days, end),
        sectors=sector_block(rows, days, end, all_rows, all_days),
        seniority=[[s, n] for s, n in Counter(r["seniority"] for r in rows).most_common()],
        cities=city_rows,
        employers=company_table(rows, 25),
        tech_employers=company_table([r for r in rows if r["sector"] in TECH_SECTORS], 15),
        remote_employers=company_table(rows, 15, key="remote", min_remote=2),
        recruiters=recruiter_table(rows),
        titles=top_titles(rows, 15),
        ai=dict(posts=len(ai_rows), share=pct(len(ai_rows), total),
                sectors=[[s, n] for s, n in Counter(r["sector"] for r in ai_rows).most_common(8)]),
    )


def build(db: str):
    rows, scans = load(db)
    days = sorted({r["day"] for r in rows})
    end = date.fromisoformat(days[-1])
    median = statistics.median([sum(1 for r in rows if r["day"] == d) for d in days]) if days else 0

    daily = []
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    d = date.fromisoformat(days[0])
    while d <= end:
        k = d.isoformat()
        rs = by_day.get(k, [])
        sc = scans.get(k)
        status = "missing" if not rs else "ok"
        if rs and ((sc and sc["calls_fail"] >= 3) or len(rs) < 0.5 * median):
            status = "partial"
        # companies posting for the first time ever on this day
        daily.append(dict(day=k, posts=len(rs), remote=sum(r["remote"] for r in rs),
                          tech=sum(r["sector"] in TECH_SECTORS for r in rs),
                          sales=sum(r["sector"] == "Sales & Business Development" for r in rs),
                          companies=len({company_key(r["company"]) for r in rs}),
                          status=status, weekday=d.strftime("%a")))
        d += timedelta(days=1)

    first_day = {}
    for r in rows:
        first_day.setdefault(company_key(r["company"]), r["day"])
    new_cut = (end - timedelta(days=6)).isoformat()
    newcomers = defaultdict(list)
    for r in rows:
        k = company_key(r["company"])
        if k and first_day[k] >= new_cut and not is_recruiter(r["company"]):
            newcomers[k].append(r)
    new_rows = sorted(({"name": Counter(x["company"] for x in rs).most_common(1)[0][0],
                        "posts": len(rs), "first": first_day[k],
                        "sector": Counter(x["sector"] for x in rs).most_common(1)[0][0]}
                       for k, rs in newcomers.items() if len(rs) >= 2),
                      key=lambda x: (-x["posts"], x["name"]))[:15]

    return dict(
        generated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        first_day=days[0], last_day=days[-1],
        total_posts=len(rows),
        total_companies=len({company_key(r["company"]) for r in rows if r["company"]}),
        scan_days=len(days),
        daily=daily,
        windows={**{k: window_stats(rows, days, end, n) for k, n in WINDOWS.items()},
                 **{m: window_stats([r for r in rows if r["day"].startswith(m)],
                                    [x for x in days if x.startswith(m)],
                                    date.fromisoformat(max(x for x in days if x.startswith(m))), None)
                    for m in sorted({x[:7] for x in days})}},
        months=sorted({x[:7] for x in days}),
        newcomers=new_rows,
    )


def main():
    ap = argparse.ArgumentParser(description="Build public stats JSON")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--out", default=os.path.join(REPO, "site", "data", "stats.json"))
    a = ap.parse_args()
    s = build(a.db)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, separators=(",", ":"))
    print(f"stats -> {a.out}  ({s['total_posts']} posts, {s['scan_days']} scan days, "
          f"{os.path.getsize(a.out)//1024} KB)")


if __name__ == "__main__":
    main()
