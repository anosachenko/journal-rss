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
3. **Set up a Telegram bot to post new articles to your channel:**
   - Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` →
     follow the prompts. It gives you a bot token
     (looks like `123456789:AAF...`).
   - Add the bot as an **admin** to your channel (Channel → Administrators →
     Add Admin → search for your bot). It needs admin rights to post.
   - Get your channel's chat ID:
     - Public channel: use `@your_channel_username` directly as the chat ID.
     - Private channel: forward any message from the channel to
       [@userinfobot](https://t.me/userinfobot) or use the Bot API's
       `getUpdates` endpoint after posting something in the channel to find
       the numeric ID (looks like `-1001234567890`).
   - Add two more repo secrets: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. **Enable GitHub Pages** (if not already): repo → Settings → Pages →
   Source: "Deploy from a branch" → Branch: `main` / `(root)` → Save.
5. **Enable workflow write access**: repo → Settings → Actions → General →
   Workflow permissions → "Read and write permissions" → Save.
6. **Run it once manually**: Actions tab → "Update MR PORTER Journal RSS
   feed" → Run workflow.

Feed URL once the first run succeeds:
```
https://anosachenko.github.io/mrporter-rss/mrporter_journal_feed.xml
```

## Schedule

Runs automatically at 09:00 and 21:00 Moscow time (`0 6 * * *` and
`0 18 * * *` UTC in `.github/workflows/update-feed.yml` — MSK is UTC+3
year-round, no DST). Edit those cron lines to change the times.

## Telegram posting

Every run, any article not seen before gets posted as a separate message to
your Telegram channel (title, category hashtag, link), oldest-first. If
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` aren't set, this step is skipped
without failing the run — the feed still updates normally.

## If it stops working

Check the Actions tab for the failed run's log. Common causes:
- **ScraperAPI trial ran out / key invalid** — the script will print a
  clear message about `SCRAPERAPI_KEY` or the HTTP status it got back.
- **mrporter.com changed its page markup** — the script logs the raw HTML
  it received when it can't find any articles, which helps diagnose this.
