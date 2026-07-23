# MR PORTER Journal — self-updating RSS feed

Scrapes https://www.mrporter.com/en-ru/journal twice a day via GitHub
Actions, tracks which articles have already been seen, and commits a
regenerated `mrporter_journal_feed.xml` back to this repo. GitHub Pages
serves that file at a stable URL you can subscribe to in any RSS reader.
Nothing needs to run on your own computer.

**Why a proxy service:** mrporter.com's CDN (Akamai) blocks GitHub Actions'
IP ranges directly at the edge ("Access Denied"), regardless of headers or
browser fingerprint. Routing the request through
[ScraperAPI](https://www.scraperapi.com/) (or a similar service — ScrapingBee
and Zyte work the same way) uses their pool of rotating/residential IPs
instead, which isn't blocked.

## One-time setup

1. **Sign up for ScraperAPI** (free trial covers ~1,000 requests/month —
   two runs a day is ~60/month, comfortably within that). Copy your API key
   from the dashboard.
2. **Add it as a GitHub secret**: repo → Settings → Secrets and variables →
   Actions → New repository secret → name `SCRAPERAPI_KEY`, value = your key.
3. **Enable GitHub Pages** (if not already): repo → Settings → Pages →
   Source: "Deploy from a branch" → Branch: `main` / `(root)` → Save.
4. **Enable workflow write access**: repo → Settings → Actions → General →
   Workflow permissions → "Read and write permissions" → Save.
5. **Run it once manually**: Actions tab → "Update MR PORTER Journal RSS
   feed" → Run workflow.

Feed URL once the first run succeeds:
```
https://anosachenko.github.io/mrporter-rss/mrporter_journal_feed.xml
```

## Schedule

Runs automatically at 09:00 and 21:00 Moscow time (`0 6 * * *` and
`0 18 * * *` UTC in `.github/workflows/update-feed.yml` — MSK is UTC+3
year-round, no DST). Edit those cron lines to change the times.

## If it stops working

Check the Actions tab for the failed run's log. Common causes:
- **ScraperAPI trial ran out / key invalid** — the script will print a
  clear message about `SCRAPERAPI_KEY` or the HTTP status it got back.
- **mrporter.com changed its page markup** — the script logs the raw HTML
  it received when it can't find any articles, which helps diagnose this.
