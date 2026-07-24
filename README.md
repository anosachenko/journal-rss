# MR PORTER Journal → Telegram

Scrapes https://www.mrporter.com/en-gb/journal (and its 5 category pages)
twice a day via GitHub Actions, and posts any article not seen before
straight to a Telegram channel — with its teaser text and image included.
`seen_articles.json` is just a dedup log; there's no RSS feed and nothing
runs on your own computer.

**Why a proxy service:** mrporter.com's CDN (Akamai) blocks GitHub Actions'
IP ranges directly at the edge ("Access Denied"), regardless of headers or
browser fingerprint. Routing the request through
[ScraperAPI](https://www.scraperapi.com/) (or a similar service — ScrapingBee
and Zyte work the same way) uses their pool of rotating/residential IPs
instead, which isn't blocked.

## One-time setup

1. **Sign up for ScraperAPI** (free trial covers ~1,000 requests/month —
   two runs a day across 6 pages is ~360/month, comfortably within that).
   Copy your API key from the dashboard.
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
4. **Enable workflow write access**: repo → Settings → Actions → General →
   Workflow permissions → "Read and write permissions" → Save (needed so the
   workflow can commit the updated `seen_articles.json`).
5. **Run it once manually**: Actions tab → "Post new MR PORTER Journal
   articles to Telegram" → Run workflow.

Heads up on the first run: since `seen_articles.json` starts empty,
*everything* found on the Journal pages (~40+ articles) counts as "new" and
gets posted in one burst. After that, only genuinely new articles get
posted on each run.

## Schedule

Runs automatically at 09:00 and 21:00 Moscow time (`0 6 * * *` and
`0 18 * * *` UTC in `.github/workflows/update-feed.yml` — MSK is UTC+3
year-round, no DST). Edit those cron lines to change the times.

## What gets posted

Each new article is sent as its own Telegram message: image (if found) as
the photo, with a caption of bold title, teaser text (trimmed to fit
Telegram's 1024-character caption limit), category hashtag, and link.
Articles are posted oldest-first so the channel reads chronologically.
Sending respects Telegram's rate limits (waits on 429 and retries).

`seen_articles.json` keeps a 30-day rolling window (`RETENTION_DAYS` in
`update_feed.py`) so old entries get pruned automatically and the file
doesn't grow forever. This window is based on when the script first saw
each article, not its real publish date (the category pages don't expose one).

## If it stops working

Check the Actions tab for the failed run's log. Common causes:
- **ScraperAPI trial ran out / key invalid** — the script will print a
  clear message about `SCRAPERAPI_KEY` or the HTTP status it got back.
- **mrporter.com changed its page markup** — the script logs the raw HTML
  it received when it can't find any articles, which helps diagnose this.
- **Telegram posting failures** — logged per-article without stopping the
  run; check for `Telegram send failed` lines.
