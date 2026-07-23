import json
import re
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from xml.sax.saxutils import escape

import requests
from feedgen.feed import FeedGenerator

BASE = "https://www.mrporter.com"
JOURNAL_URL = f"{BASE}/en-gb/journal"
CATEGORIES = ["fashion", "grooming", "watches", "travel", "lifestyle"]
PAGES_TO_FETCH = [JOURNAL_URL] + [f"{JOURNAL_URL}/{cat}" for cat in CATEGORIES]

STATE_FILE = os.path.join(os.path.dirname(__file__), "seen_articles.json")
FEED_FILE = os.path.join(os.path.dirname(__file__), "mrporter_journal_feed.xml")
MAX_ITEMS = 50
MAX_ATTEMPTS = 4

SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY")
SCRAPERAPI_ENDPOINT = "https://api.scraperapi.com/"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Matches links like /en-gb/journal/fashion/logo-trend-25526080, capturing
# everything inside the <a>...</a> so we can pull the image and blurb out of
# it too (article cards on this site wrap image+title+byline in one link).
ARTICLE_LINK_RE = re.compile(
    r'href="(/en-gb/journal/(?:fashion|grooming|watches|travel|lifestyle)/[a-z0-9-]+)(?:\?[^"]*)?"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
TAG_STRIP_RE = re.compile(r"<[^>]+>")
READ_TIME_RE = re.compile(r"\d+\s*MINUTE\s*READ", re.IGNORECASE)
BYLINE_RE = re.compile(r"\s*Words by .+$", re.IGNORECASE)

IMG_DATA_SRC_RE = re.compile(r'<img[^>]+data-src="([^"]+)"', re.IGNORECASE)
IMG_SRCSET_RE = re.compile(r'<img[^>]+srcset="([^"]+)"', re.IGNORECASE)
IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.IGNORECASE)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def normalize_url(url):
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE + url
    return url


def extract_image(inner_html):
    """Pull an image URL out of an article card. Prefers data-src/srcset
    (usually the real lazy-loaded image) over a plain src (often a blank
    placeholder used before JS lazy-loading kicks in)."""
    m = IMG_DATA_SRC_RE.search(inner_html)
    if not m:
        m = IMG_SRCSET_RE.search(inner_html)
        if m:
            first_candidate = m.group(1).split(",")[0].strip().split(" ")[0]
            return normalize_url(first_candidate)
    if not m:
        m = IMG_SRC_RE.search(inner_html)
    return normalize_url(m.group(1)) if m else None


def fetch_html(url):
    """Fetch a page through ScraperAPI's proxy (residential/rotating IPs),
    which avoids the Akamai edge block that hits GitHub Actions' own IPs."""
    if not SCRAPERAPI_KEY:
        raise RuntimeError(
            "SCRAPERAPI_KEY is not set. Add it as a GitHub repo secret "
            "(Settings → Secrets and variables → Actions) and reference it "
            "in the workflow's env."
        )

    params = {"api_key": SCRAPERAPI_KEY, "url": url}
    proxied_url = f"{SCRAPERAPI_ENDPOINT}?{urlencode(params)}"

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(proxied_url, timeout=70)
            if resp.status_code == 200:
                return resp.text
            print(f"  Attempt {attempt}/{MAX_ATTEMPTS}: HTTP {resp.status_code} "
                  f"from ScraperAPI — first 300 chars: {resp.text[:300]}")
        except requests.exceptions.RequestException as e:
            last_error = e
            print(f"  Attempt {attempt}/{MAX_ATTEMPTS} failed: {e}")

        wait = 5 * attempt
        print(f"  Retrying in {wait}s...")
        time.sleep(wait)

    raise RuntimeError(f"All {MAX_ATTEMPTS} attempts failed.") from last_error


def parse_articles(html):
    articles = {}
    for path, inner_html in ARTICLE_LINK_RE.findall(html):
        image = extract_image(inner_html)

        text = TAG_STRIP_RE.sub(" ", inner_html)
        text = READ_TIME_RE.sub("", text)
        text = re.sub(r"\s+", " ", text).strip(" -")

        byline_match = BYLINE_RE.search(text)
        byline = byline_match.group(0).strip() if byline_match else None
        title = BYLINE_RE.sub("", text).strip()

        if not title or len(title) < 3:
            continue

        url = BASE + path
        category = path.split("/")[3]
        articles[url] = {
            "title": title,
            "category": category,
            "image": image,
            "byline": byline,
        }

    if not articles:
        raw_count = len(re.findall(r"/en-gb/journal/[a-z0-9/-]+", html, re.IGNORECASE))
        print(f"  DEBUG: no articles matched. HTML length={len(html)}, "
              f"raw '/en-gb/journal/...' substrings found={raw_count}")
        for marker in ("captcha", "access denied", "blocked", "are you human", "px-captcha"):
            if marker in html.lower():
                print(f"  DEBUG: page HTML contains suspicious marker: '{marker}'")
        print("  DEBUG: first 1000 chars of fetched HTML:")
        print(html[:1000])

    return articles


def post_to_telegram(new_items):
    """Send one message per newly-found article to a Telegram channel.
    new_items: list of (url, title, category, image) tuples, oldest first.
    Never raises — a Telegram failure shouldn't block the feed commit."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing) — skipping posting.")
        return

    text_api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    photo_api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"

    for url, title, category, image in new_items:
        caption = f"<b>{title}</b>\n#{category}\n{url}"
        try:
            if image:
                resp = requests.post(
                    photo_api_url,
                    data={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "photo": image,
                        "caption": caption,
                        "parse_mode": "HTML",
                    },
                    timeout=20,
                )
            else:
                resp = requests.post(
                    text_api_url,
                    data={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": caption,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": False,
                    },
                    timeout=20,
                )
            if resp.status_code != 200:
                print(f"Telegram send failed for '{title}': HTTP {resp.status_code} — {resp.text[:300]}")
            else:
                print(f"Posted to Telegram: {title}")
        except requests.exceptions.RequestException as e:
            print(f"Telegram send failed for '{title}': {e}")

        time.sleep(1)  # stay well under Telegram's rate limits


def guess_image_type(url):
    ext = url.rsplit(".", 1)[-1].lower().split("?")[0]
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
    }.get(ext, "image/jpeg")


def build_feed(state):
    fg = FeedGenerator()
    fg.title("MR PORTER — The Journal")
    fg.link(href=JOURNAL_URL, rel="alternate")
    fg.description("Latest articles from MR PORTER's The Journal.")
    fg.language("en")

    items = sorted(state.items(), key=lambda kv: kv[1]["first_seen"], reverse=True)[:MAX_ITEMS]
    for url, data in items:
        fe = fg.add_entry()
        fe.id(url)
        fe.title(data["title"])
        fe.link(href=url)
        fe.category(term=data["category"])
        fe.pubDate(data["first_seen"])

        image = data.get("image")
        description_parts = []
        if image:
            description_parts.append(f'<img src="{escape(image)}" alt="{escape(data["title"])}"/>')
        description_parts.append(f"<p>{escape(data['title'])}</p>")
        if data.get("byline"):
            description_parts.append(f"<p>{escape(data['byline'])}</p>")
        fe.description("".join(description_parts))

        if image:
            fe.enclosure(image, "0", guess_image_type(image))

    fg.rss_file(FEED_FILE, pretty=True)


def main():
    state = load_state()

    all_articles = {}
    any_page_succeeded = False
    for url in PAGES_TO_FETCH:
        print(f"Fetching {url} ...")
        try:
            html = fetch_html(url)
        except RuntimeError as e:
            print(f"  Could not fetch this page: {e}")
            continue
        any_page_succeeded = True
        page_articles = parse_articles(html)
        print(f"  Found {len(page_articles)} article(s) on this page.")
        all_articles.update(page_articles)  # later pages don't overwrite meaningfully differently

    if not any_page_succeeded:
        print("Could not fetch any Journal page this run.")
        print("Skipping this run — no changes made. Will try again on the next schedule.")
        sys.exit(0)

    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

    new_count = 0
    new_items = []  # (url, title, category, image), in the order encountered
    for url, data in all_articles.items():
        if url not in state:
            state[url] = {
                "title": data["title"],
                "category": data["category"],
                "image": data["image"],
                "byline": data["byline"],
                "first_seen": now,
            }
            new_count += 1
            new_items.append((url, data["title"], data["category"], data["image"]))

    save_state(state)
    build_feed(state)
    print(f"Checked {len(all_articles)} unique articles across {len(PAGES_TO_FETCH)} pages, "
          f"{new_count} new, feed has {len(state)} total items.")

    if new_items:
        new_items.reverse()  # oldest-first, so the channel reads chronologically
        post_to_telegram(new_items)


if __name__ == "__main__":
    main()
