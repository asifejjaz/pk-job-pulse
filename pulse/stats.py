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
    # \w keeps letters of every script, so a name written in Urdu or Arabic still gets a key
    k = re.sub(r"[^\w& ]+|_", " ", (name or "").lower())
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


def per_day_change(rows, status, end: date):
    """Latest 7 days against the 7 before, same weekdays only.

    A day is compared with the day one week earlier, and only when both had a full
    scan. Skipped or partial scan days therefore drop out instead of reading as a
    crash, and a week that lost its busy weekdays is not set against one that kept
    them. At least 3 matching weekdays are needed, otherwise there is no figure."""
    per = Counter(r["day"] for r in rows)
    cur = prev = pairs = 0
    for back in range(7):
        a = (end - timedelta(days=back)).isoformat()
        b = (end - timedelta(days=back + 7)).isoformat()
        if status.get(a) == "ok" and status.get(b) == "ok":
            pairs += 1
            cur += per[a]
            prev += per[b]
    if pairs < 3 or not cur or not prev:
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


def sector_block(rows, days, end, all_rows, status):
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
            change=per_day_change([r for r in all_rows if r["sector"] == sec], status, end),
            titles=top_titles(rs, 6),
            companies=[[d["name"], d["posts"]] for d in company_table(rs, 6)],
            cities=[[c, k] for c, k in Counter(r["city"] for r in rs).most_common(5)],
            seniority=[[s, k] for s, k in Counter(r["seniority"] for r in rs).most_common()],
        ))
    out.sort(key=lambda d: -d["posts"])
    return out


def window_stats(rows, days, end, n_days, status):
    # Week over week change always compares the latest 7 days with the 7 before,
    # so it needs the full history even when the window itself is shorter.
    all_rows = rows
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
        change=per_day_change(all_rows, status, end),
        sectors=sector_block(rows, days, end, all_rows, status),
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


def norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def build_cube(rows, daily):
    """Counts by day, company, sector, level, city, remote flag and common title.

    The site filters and drills down on this in the browser, so every figure on the
    page comes from one table and always adds up. It holds counts only. A job title is
    kept only when at least 3 posts from at least 2 companies share it, so a single
    posting can never be read back out of the file; every other title becomes "other"."""
    day_ix = {d["day"]: i for i, d in enumerate(daily)}

    names = defaultdict(Counter)
    for r in rows:
        names[company_key(r["company"])][r["company"]] += 1
    names.pop("", None)
    volume = Counter(company_key(r["company"]) for r in rows)
    keys = sorted(names, key=lambda k: (-volume[k], k))
    companies = [["", 0]]  # index 0: posts that name no company
    for k in keys:
        name = names[k].most_common(1)[0][0]
        companies.append([name, 1 if is_recruiter(name) else 0])
    co_ix = {k: i + 1 for i, k in enumerate(keys)}
    co_ix[""] = 0

    def index(values):
        order = [v for v, _ in Counter(values).most_common()]
        return order, {v: i for i, v in enumerate(order)}

    sectors, sec_ix = index(r["sector"] for r in rows)
    levels, lvl_ix = index(r["seniority"] for r in rows)
    cities, city_ix = index(r["city"] for r in rows)

    t_posts, t_cos = Counter(), defaultdict(set)
    for r in rows:
        t = norm_title(r["title"])
        if t:
            t_posts[t] += 1
            t_cos[t].add(company_key(r["company"]))
    common = [t for t, n in t_posts.most_common() if n >= 3 and len(t_cos[t]) >= 2]
    titles = [""] + common  # index 0: any title too rare to publish
    title_ix = {t: i + 1 for i, t in enumerate(common)}

    cells = Counter()
    for r in rows:
        flags = (1 if r["remote"] else 0) | (2 if AI_TITLE.search(r["title"]) else 0)
        cells[(day_ix[r["day"]], co_ix[company_key(r["company"])], sec_ix[r["sector"]],
               lvl_ix[r["seniority"]], city_ix[r["city"]], flags,
               title_ix.get(norm_title(r["title"]), 0))] += 1
    ordered = sorted(cells.items())
    cols = list(zip(*[k + (n,) for k, n in ordered])) if ordered else [[]] * 8
    return dict(
        days=[[d["day"], d["status"]] for d in daily],
        companies=companies, sectors=sectors, levels=levels, cities=cities, titles=titles,
        tech=[sec_ix[s] for s in TECH_SECTORS if s in sec_ix],
        entry=[lvl_ix[s] for s in ENTRY_LEVELS if s in lvl_ix],
        senior=[lvl_ix[s] for s in SENIOR_LEVELS if s in lvl_ix],
        rows=dict(zip(["d", "c", "s", "l", "y", "f", "t", "n"], [list(c) for c in cols])),
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
                          companies=len({company_key(r["company"]) for r in rs} - {""}),
                          status=status, weekday=d.strftime("%a")))
        d += timedelta(days=1)

    status = {x["day"]: x["status"] for x in daily}

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
        windows={**{k: window_stats(rows, days, end, n, status) for k, n in WINDOWS.items()},
                 **{m: window_stats([r for r in rows if r["day"].startswith(m)],
                                    [x for x in days if x.startswith(m)],
                                    date.fromisoformat(max(x for x in days if x.startswith(m))), None,
                                    status)
                    for m in sorted({x[:7] for x in days})}},
        months=sorted({x[:7] for x in days}),
        newcomers=new_rows,
        cube=build_cube(rows, daily),
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
