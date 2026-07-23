import json
import re
import os
import sys
import time
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

# Регулярка для поиска ссылок на статьи
ARTICLE_LINK_RE = re.compile(
    r'href="(/en-ru/journal/(?:fashion|grooming|watches|travel|lifestyle)/[a-z0-9-]+)(?:\?[^"]*)?"',
    re.IGNORECASE,
)
IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
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


def fetch_html():
    if not SCRAPERAPI_KEY:
        raise RuntimeError(
            "SCRAPERAPI_KEY is not set. Add it as a GitHub repo secret "
            "(Settings → Secrets and variables → Actions) and reference it "
            "in the workflow's env."
        )

    params = {"api_key": SCRAPERAPI_KEY, "url": JOURNAL_URL}
    proxied_url = f"{SCRAPERAPI_ENDPOINT}?{urlencode(params)}"

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(proxied_url, timeout=70)
            if resp.status_code == 200:
                return resp.text
            print(
                f"Attempt {attempt}/{MAX_ATTEMPTS}: HTTP {resp.status_code} "
                f"from ScraperAPI — first 300 chars: {resp.text[:300]}"
            )
        except requests.exceptions.RequestException as e:
            last_error = e
            print(f"Attempt {attempt}/{MAX_ATTEMPTS} failed: {e}")

        wait = 5 * attempt
        print(f"Retrying in {wait}s...")
        time.sleep(wait)

    raise RuntimeError(f"All {MAX_ATTEMPTS} attempts failed.") from last_error


def parse_articles(html):
    articles = {}

    # Находим все уникальные пути статей
    paths = set(ARTICLE_LINK_RE.findall(html))

    for path in paths:
        url = BASE + path
        category = path.split("/")[3]

        # Ищем фрагмент HTML вокруг ссылки на статью (карточку)
        escaped_path = re.escape(path)
        card_match = re.search(
            r"(?:<article|<div)[^>]*>(?:(?!</article>|</div>).)*?"
            + escaped_path
            + r".*?(?:</article>|</div>)",
            html,
            re.IGNORECASE | re.DOTALL,
        )

        card_html = card_match.group(0) if card_match else ""

        # Извлекаем тексты из карточки
        texts = [
            re.sub(r"\s+", " ", TAG_STRIP_RE.sub(" ", t)).strip()
            for t in re.findall(r"<p[^>]*>(.*?)</p>|<h\d[^>]*>(.*?)</h\d>", card_html, re.DOTALL)
        ]
        flat_texts = [t for pair in texts for t in pair if t]

        # Первое текстовое вхождение — заголовок, последующие — описание/анонс
        title = ""
        summary = ""
        for t in flat_texts:
            t_clean = READ_TIME_RE.sub("", t).strip(" -")
            if not t_clean or len(t_clean) < 3:
                continue
            if not title:
                title = t_clean
            elif not summary and t_clean.lower() != title.lower():
                summary = t_clean

        if not title:
            continue

        # Картинка
        img_match = IMG_SRC_RE.search(card_html or html)
        img_url = ""
        if img_match:
            img_url = img_match.group(1)
            if img_url.startswith("//"):
                img_url = "https:" + img_url
            elif img_url.startswith("/"):
                img_url = BASE + img_url

        articles[url] = {
            "title": title,
            "category": category,
            "summary": summary,
            "image": img_url,
        }

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

        # Формируем HTML для поля description (картинка + анонс)
        content_parts = []
        img_url = data.get("image", "")
        if img_url:
            content_parts.append(f'<img src="{img_url}" alt="{data["title"]}" /><br/>')
            fe.enclosure(url=img_url, type="image/jpeg")

        summary = data.get("summary", "")
        if summary:
            content_parts.append(f"<p>{summary}</p>")

        fe.description("".join(content_parts) if content_parts else "")

    fg.rss_file(FEED_FILE, pretty=True)


def main():
    state = load_state()

    try:
        html = fetch_html()
        articles = parse_articles(html)
    except RuntimeError as e:
        print(f"Could not fetch the Journal page this run: {e}")
        print("Skipping this run — no changes made. Will try again on the next schedule.")
        sys.exit(0)

    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

    new_count = 0
    for url, data in articles.items():
        if url not in state or "summary" not in state[url]:
            state[url] = {
                "title": data["title"],
                "category": data["category"],
                "summary": data.get("summary", ""),
                "image": data.get("image", ""),
                "first_seen": state.get(url, {}).get("first_seen", now),
            }
            new_count += 1

    save_state(state)
    build_feed(state)
    print(f"Checked {len(articles)} articles on page, {new_count} updated/new, feed has {len(state)} total items.")


if __name__ == "__main__":
    main()
