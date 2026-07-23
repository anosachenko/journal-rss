import json
import re
import os
import sys
import time
import html as html_module
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from feedgen.feed import FeedGenerator

BASE = "https://www.mrporter.com"
JOURNAL_URL = f"{BASE}/en-ru/journal"

STATE_FILE = os.path.join(os.path.dirname(__file__), "seen_articles.json")
FEED_FILE = os.path.join(os.path.dirname(__file__), "mrporter_journal_feed.xml")
MAX_ITEMS = 50
MAX_ATTEMPTS = 4

SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY")
SCRAPERAPI_ENDPOINT = "https://api.scraperapi.com/"

# Matches links like /en-ru/journal/fashion/logo-trend-25526080
ARTICLE_LINK_RE = re.compile(
    r'href="(/en-ru/journal/(?:fashion|grooming|watches|travel|lifestyle)/[a-z0-9-]+)(?:\?[^"]*)?"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
TAG_STRIP_RE = re.compile(r"<[^>]+>")
READ_TIME_RE = re.compile(r"\d+\s*MINUTE\s*READ", re.IGNORECASE)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_html(url):
    """Fetch a page through ScraperAPI's proxy."""
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
            print(f"Attempt {attempt}/{MAX_ATTEMPTS}: HTTP {resp.status_code} "
                  f"from ScraperAPI — first 300 chars: {resp.text[:300]}")
        except requests.exceptions.RequestException as e:
            last_error = e
            print(f"Attempt {attempt}/{MAX_ATTEMPTS} failed: {e}")

        wait = 5 * attempt
        print(f"Retrying in {wait}s...")
        time.sleep(wait)

    raise RuntimeError(f"All {MAX_ATTEMPTS} attempts failed.") from last_error


def parse_article_details(html):
    """Extract og:image and og:description from an article's HTML."""
    image_url = None
    description = None

    # Ищем og:image
    img_match = re.search(
        r'<meta\s+(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE
    )
    if not img_match:
        img_match = re.search(
            r'<meta\s+name=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']',
            html, re.IGNORECASE
        )
    if img_match:
        image_url = html_module.unescape(img_match.group(1))

    # Ищем og:description
    desc_match = re.search(
        r'<meta\s+(?:property|name)=["\']og:description["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE
    )
    if not desc_match:
        desc_match = re.search(
            r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)["\']',
            html, re.IGNORECASE
        )
    if desc_match:
        description = html_module.unescape(desc_match.group(1))
        # Очищаем от HTML-тегов, если они вдруг попали в описание
        description = TAG_STRIP_RE.sub(" ", description)
        description = re.sub(r"\s+", " ", description).strip()

    return image_url, description


def parse_articles(html):
    articles = {}
    for path, inner_html in ARTICLE_LINK_RE.findall(html):
        title = TAG_STRIP_RE.sub(" ", inner_html)
        title = READ_TIME_RE.sub("", title)
        title = re.sub(r"\s+", " ", title).strip(" -")
        if not title or len(title) < 3:
            continue
        url = BASE + path
        category = path.split("/")[3]
        articles[url] = {"title": title, "category": category}

    if not articles:
        raw_count = len(re.findall(r"/en-ru/journal/[a-z0-9/-]+", html, re.IGNORECASE))
        print(f"DEBUG: no articles matched. HTML length={len(html)}, "
              f"raw '/en-ru/journal/...' substrings found={raw_count}")
        for marker in ("captcha", "access denied", "blocked", "are you human", "px-captcha"):
            if marker in html.lower():
                print(f"DEBUG: page HTML contains suspicious marker: '{marker}'")
        print("DEBUG: first 1000 chars of fetched HTML:")
        print(html[:1000])

    return articles


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

        description = data.get("description") or data["title"]
        image_url = data.get("image")

        # Добавляем обычное текстовое описание
        fe.description(description)

        # Если есть картинка, добавляем её через enclosure (стандарт для RSS-ридеров)
        # и формируем HTML-версию описания, чтобы картинка отображалась прямо в тексте
        if image_url:
            fe.enclosure(image_url, 0, "image/jpeg")
            
            html_content = f'<img src="{image_url}" alt="{data["title"]}"/><br/><p>{description}</p>'
            fe.content(html_content, type='html')

    fg.rss_file(FEED_FILE, pretty=True)


def main():
    state = load_state()

    try:
        html = fetch_html(JOURNAL_URL)
        articles = parse_articles(html)
    except RuntimeError as e:
        print(f"Could not fetch the Journal page this run: {e}")
        print("Skipping this run — no changes made. Will try again on the next schedule.")
        sys.exit(0)

    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

    new_count = 0
    for url, data in articles.items():
        if url not in state:
            print(f"New article found: {data['title']}. Fetching details...")
            try:
                # Парсим саму статью ради картинки и описания
                article_html = fetch_html(url)
                image_url, description = parse_article_details(article_html)
                
                state[url] = {
                    "title": data["title"],
                    "category": data["category"],
                    "first_seen": now,
                    "image": image_url,
                    "description": description
                }
                new_count += 1
                # Небольшая пауза, чтобы не бомбардировать API запросами
                time.sleep(1) 
            except Exception as e:
                print(f"Failed to fetch details for {url}: {e}. Saving without image/description.")
                state[url] = {
                    "title": data["title"],
                    "category": data["category"],
                    "first_seen": now,
                    "image": None,
                    "description": None
                }
                new_count += 1

    save_state(state)
    build_feed(state)
    print(f"Checked {len(articles)} articles on page, {new_count} new, feed has {len(state)} total items.")


if __name__ == "__main__":
    main()
