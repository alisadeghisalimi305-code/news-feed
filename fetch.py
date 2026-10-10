#!/usr/bin/env python3
"""Fetch latest text posts of a public Telegram channel and build docs/index.html."""
import json
import pathlib
import sys
import urllib.request
from datetime import datetime, timezone

from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).parent
DOCS = ROOT / "docs"
UA = "Mozilla/5.0 (compatible; ChannelReader/1.0)"


def get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def parse(page: str):
    soup = BeautifulSoup(page, "html.parser")
    title_el = soup.select_one(".tgme_channel_info_header_title")
    title = title_el.get_text(strip=True) if title_el else None
    posts = []
    for wrap in soup.select(".tgme_widget_message_wrap"):
        msg = wrap.select_one(".tgme_widget_message")
        if not msg or not msg.get("data-post"):
            continue
        text_el = wrap.select_one(".tgme_widget_message_text")
        if not text_el:
            continue  # text only: skip posts without text
        for br in text_el.find_all("br"):
            br.replace_with("\n")
        text = text_el.get_text().strip()
        if not text:
            continue
        post_id = int(msg["data-post"].split("/")[-1])
        time_el = wrap.select_one("time[datetime]")
        views_el = wrap.select_one(".tgme_widget_message_views")
        posts.append(
            {
                "id": post_id,
                "text": text,
                "date": time_el["datetime"] if time_el else None,
                "views": views_el.get_text(strip=True) if views_el else None,
                "url": f"https://t.me/{msg['data-post']}",
            }
        )
    ids = [int(m["data-post"].split("/")[-1]) for m in soup.select(".tgme_widget_message[data-post]")]
    return title, posts, (min(ids) if ids else None)


def fetch_channel(channel: str, max_posts: int):
    base = f"https://t.me/s/{channel}"
    title, posts, before = None, [], None
    for _ in range(10):
        page = get(base if before is None else f"{base}?before={before}")
        t, found, oldest = parse(page)
        title = title or t
        posts.extend(found)
        if len(posts) >= max_posts or oldest is None or oldest <= 1:
            break
        before = oldest
    return title, posts


def main():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    channel = cfg["channel"].lstrip("@").strip()
    max_posts = int(cfg.get("max_posts", 100))

    if len(sys.argv) > 2 and sys.argv[1] == "--file":  # offline test mode
        title, posts, _ = parse(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
    else:
        title, posts = fetch_channel(channel, max_posts)

    DOCS.mkdir(exist_ok=True)
    data_path = DOCS / "data.json"
    old = {}
    if data_path.exists():
        try:
            old = {p["id"]: p for p in json.loads(data_path.read_text(encoding="utf-8"))["posts"]}
        except Exception:
            old = {}
    old.update({p["id"]: p for p in posts})
    merged = sorted(old.values(), key=lambda p: p["id"], reverse=True)[:max_posts]

    data = {
        "channel": channel,
        "title": title or channel,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "posts": merged,
    }
    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    template = (ROOT / "template.html").read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    (DOCS / "index.html").write_text(template.replace("__DATA__", blob), encoding="utf-8")
    print(f"OK: {len(merged)} posts from @{channel}")


if __name__ == "__main__":
    main()
