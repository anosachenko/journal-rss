import json
import re
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from xml.sax.saxutils import escape

import requests

BASE = "https://www.mrporter.com"
JOURNAL_URL = f"{BASE}/en-gb/journal"
CATEGORIES = ["fashion", "grooming", "watches", "travel", "lifestyle"]
PAGES_TO_FETCH = [JOURNAL_URL] + [f"{JOURNAL_URL}/{cat}" for cat in CATEGORIES]

STATE_FILE = os.path.join(os.path.dirname(__file__), "seen_articles.json")
MAX_ATTEMPTS = 4
RETENTION_DAYS = 30
DATE_FORMAT = "%a, %d %b %Y %H:%M:%S %z"

SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY")
SCRAPERAPI_ENDPOINT = "https://api.scraperapi.com/"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Article cards on this site are <a href="..." data-type="article">...</a>;
# product cards use data-type="product" and are excluded by requiring this
# exact marker. Attribute order (href vs data-type) isn't fixed across card
# variants, so lookaheads are used instead of anchoring on a fixed order.
ARTICLE_A_RE = re.compile(
    r'<a\b(?=[^>]*\bhref="([^"]+)")(?=[^>]*\bdata-type="article")[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
TITLE_TAG_RE = re.compile(r"<h[12][^>]*>(.*?)</h[12]>", re.IGNORECASE | re.DOTALL)
PARAGRAPH_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
PICTURE_BLOCK_RE = re.compile(r"<picture.*?</picture>", re.IGNORECASE | re.DOTALL)
NOSCRIPT_BLOCK_RE = re.compile(r"<noscript.*?</noscript>", re.IGNORECASE | re.DOTALL)
TAG_STRIP_RE = re.compile(r"<[^>]+>")
READ_TIME_RE = re.compile(r"\d+\s*MINUTE\s*READ", re.IGNORECASE)

IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.IGNORECASE)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def prune_old_entries(state):
    """Drop articles first seen more than RETENTION_DAYS ago, so the
    dedup file stops growing forever."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    kept = {}
    dropped = 0
    for url, data in state.items():
        try:
            seen_at = datetime.strptime(data["first_seen"], DATE_FORMAT)
        except (KeyError, ValueError):
            kept[url] = data  # keep anything we can't parse, rather than lose it silently
            continue
        if seen_at >= cutoff:
            kept[url] = data
        else:
            dropped += 1
    if dropped:
        print(f"Pruned {dropped} article(s) older than {RETENTION_DAYS} days.")
    return kept


def normalize_url(url):
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE + url
    return url


def clean_text(raw_html_fragment):
    text = TAG_STRIP_RE.sub(" ", raw_html_fragment)
    return re.sub(r"\s+", " ", text).strip()


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
    for path, inner_html in ARTICLE_A_RE.findall(html):
        if "/en-gb/journal/" not in path:
            continue
        segments = [p for p in path.split("/") if p]
        # expect en-gb / journal / <category> / <slug>
        if len(segments) < 4:
            continue  # topic nav links like /en-gb/journal/fashion itself

        image_match = IMG_SRC_RE.search(inner_html)
        image = normalize_url(image_match.group(1)) if image_match else None

        # Strip picture/noscript blocks so image markup doesn't pollute title/teaser text
        text_html = PICTURE_BLOCK_RE.sub("", inner_html)
        text_html = NOSCRIPT_BLOCK_RE.sub("", text_html)

        title_match = TITLE_TAG_RE.search(text_html)
        if not title_match:
            continue
        title = clean_text(title_match.group(1))
        if not title:
            continue

        # Teaser: first <p> after the title that isn't just the read-time
        # marker and has enough length to be real body copy, not a UI label
        # like "Continue Reading".
        teaser = None
        for p_match in PARAGRAPH_RE.finditer(text_html[title_match.end():]):
            p_text = clean_text(p_match.group(1))
            if not p_text or READ_TIME_RE.fullmatch(p_text):
                continue
            if len(p_text) > 40:
                teaser = p_text
                break

        url = BASE + path
        category = segments[2]
        articles[url] = {
            "title": title,
            "category": category,
            "image": image,
            "teaser": teaser,
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


def build_caption(data, url):
    """Telegram caption: bold title, teaser (if any), category hashtag, link.
    Telegram captions are capped at 1024 chars, so the teaser is trimmed."""
    parts = [f"<b>{escape(data['title'])}</b>"]
    if data.get("teaser"):
        teaser = data["teaser"]
        max_teaser_len = 700
        if len(teaser) > max_teaser_len:
            teaser = teaser[:max_teaser_len].rsplit(" ", 1)[0] + "…"
        parts.append(escape(teaser))
    parts.append(f"#{data['category']}")
    parts.append(url)
    return "\n\n".join(parts)


def send_telegram_one(text_api_url, photo_api_url, caption, image):
    """Send a single Telegram message, retrying on 429 by waiting the
    server-specified retry_after. Returns the final response (or raises
    on network error)."""
    max_retries = 5
    for attempt in range(max_retries):
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

        if resp.status_code != 429:
            return resp

        try:
            retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
        except ValueError:
            retry_after = 5
        print(f"  Rate limited by Telegram, waiting {retry_after}s before retry "
              f"({attempt + 1}/{max_retries})...")
        time.sleep(retry_after + 1)

    return resp  # exhausted retries; caller logs the final failed response


def post_to_telegram(new_items):
    """Send one message per newly-found article to a Telegram channel,
    including the teaser text and image. new_items: list of (url, data)
    tuples, oldest first. Never raises — a Telegram failure here shouldn't
    block saving the dedup state."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing) — skipping posting.")
        return

    text_api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    photo_api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"

    for url, data in new_items:
        caption = build_caption(data, url)
        try:
            resp = send_telegram_one(text_api_url, photo_api_url, caption, data.get("image"))
            if resp.status_code != 200:
                print(f"Telegram send failed for '{data['title']}': HTTP {resp.status_code} — {resp.text[:300]}")
            else:
                print(f"Posted to Telegram: {data['title']}")
        except requests.exceptions.RequestException as e:
            print(f"Telegram send failed for '{data['title']}': {e}")

        time.sleep(1.5)  # stay well under Telegram's rate limits


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
        all_articles.update(page_articles)

    if not any_page_succeeded:
        print("Could not fetch any Journal page this run.")
        print("Skipping this run — no changes made. Will try again on the next schedule.")
        sys.exit(0)

    now = datetime.now(timezone.utc).strftime(DATE_FORMAT)

    new_items = []  # (url, data), in the order encountered
    for url, data in all_articles.items():
        if url not in state:
            state[url] = {
                "title": data["title"],
                "category": data["category"],
                "image": data["image"],
                "teaser": data["teaser"],
                "first_seen": now,
            }
            new_items.append((url, state[url]))

    state = prune_old_entries(state)
    save_state(state)
    print(f"Checked {len(all_articles)} unique articles across {len(PAGES_TO_FETCH)} pages, "
          f"{len(new_items)} new (retention: {RETENTION_DAYS} days, {len(state)} tracked).")

    if new_items:
        new_items.reverse()  # oldest-first, so the channel reads chronologically
        post_to_telegram(new_items)


if __name__ == "__main__":
    main()
