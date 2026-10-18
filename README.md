# Pakistan Job Pulse

Daily statistics on who is hiring in Pakistan: by sector, seniority, city, remote share and employer. Built from every new Pakistan job post on Indeed and LinkedIn, scanned once a day.

**Live dashboard: https://cognitionsync.com/pulse/** (updated daily)

This is a market statistics tool, not a job board. The live site publishes counts only: how many posts by day, company, sector, level and city. Job links and descriptions are never published, and a job title is named only when at least 3 posts from at least 2 companies share it.

**Who it is for:** fresh graduates picking a first direction, experienced people weighing a career switch, and anyone who wants numbers instead of hunches about the Pakistani job market.

## What it measures

| Question | Where on the site |
|---|---|
| How many jobs are posted per day, and is the market growing? | Posts per day, latest week against the week before |
| Which sectors are hiring, and which are open to juniors, seniors or remote work? | Sectors tab |
| Which companies hired in a sector, and on which days? | Click any sector: company list with a posting day strip |
| Who are the biggest, steadiest and newest employers? | Companies tab, with rankings for most posts, most consistent, new this week, most remote and open to juniors |
| What does one company hire for, and how often? | Company profile: timeline, sectors, levels, cities, common titles, similar employers |
| Where are the jobs? | Cities |
| How often does "AI" show up in job titles, and in which sectors? | AI in the job title |

Every figure responds to the filters at the top: any date range, sector, city, level, remote or on site, direct employers or recruiters, and company name. All of it is counted in the browser from one table of counts inside `stats.json`, so the parts always add up.

## How it works

```
Indeed + LinkedIn ──> pulse/scanner.py ──> data/pk_market.db (local, never published)
                                                  │
                                                  └──> pulse/stats.py ──> site/data/stats.json ──> site/index.html
```

1. **Scan.** `python -m pulse.scanner scan` pulls every Pakistan posting from the last 24 hours (one national Indeed pull, seven city pulls, three LinkedIn pulls) through the open source [JobSpy](https://github.com/speedyapply/JobSpy) library. It takes about three minutes and usually finds 600 to 900 postings.
2. **Store.** Each posting is stored once in SQLite, keyed on its URL, and counted on the day it first appeared.
3. **Classify.** Sector, seniority and city come from rules on the job title, because the job sites leave those fields empty. The rules live in `pulse/scanner.py`. After editing them, run `python -m pulse.scanner reclassify` so the history stays comparable.
4. **Aggregate.** `python -m pulse.stats` turns the store into one JSON file, deduplicating openings posted on both sites. It holds a table of counts by day, company, sector, level, city and remote flag, which the page filters in the browser.
5. **Publish.** `site/` is a static page with no build step. Serve it from any web server.

## Run it yourself

```bash
pip install -r requirements.txt
python -m pulse.scanner scan
python -m pulse.stats
python -m http.server 8000 --directory site
```

Then open http://localhost:8000. Set `PULSE_DATA` to keep the database somewhere other than `./data`.

To run it daily on Windows, point Task Scheduler at `scripts/daily.ps1`. On Linux, a cron line running `scripts/daily.sh` does the same.

## Limits, stated plainly

* **Two sources.** Companies that never post on Indeed or LinkedIn are invisible. Rozee.pk and company career pages are not covered.
* **Title based classification.** About 17% of titles are too vague to place in a sector ("Officer", "Executive"). Seniority is read from words like senior, junior and manager, so a senior role without the word in its title counts as mid level.
* **Remote is a floor.** It relies on the site's remote flag, which many remote roles leave unset.
* **No salary data.** Almost no Pakistani posting publishes pay, so pay trends are not possible from this data.
* **Skipped days.** If a scan does not run, that day is empty. Week over week change compares the same weekdays and only days with a full scan in both weeks, so a skipped day does not look like a crash, and the chart marks missing and partial days.
* **Recruiters.** Staffing agencies post on behalf of many employers. They are detected by name and listed separately so they do not top the employer ranking. The detection is rule based and can miss some.

## Responsible use

Scraping is done at a low rate, once a day, through JobSpy. Check the terms of each site before running this yourself, and publish aggregates rather than copies of other people's listings. That is the design here.

## License

MIT. See `LICENSE`.
