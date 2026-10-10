#!/usr/bin/env python3
"""Personal dashboard builder.

Modules (each has its own interval in config.json):
  telegram - text + images of a public channel (no video)
  youtube  - newest unseen Persian videos for given queries (needs YOUTUBE_API_KEY)
Then builds docs/index.html (single page with tabs).

Usage:
  python fetch.py                 run modules that are due
  python fetch.py --force         run every module now
  python fetch.py --only=youtube  run one module now
"""
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).parent
DOCS = ROOT / "docs"
DATA = DOCS / "data"
MEDIA = DOCS / "media" / "tg"
UA = "Mozilla/5.0 (compatible; PersonalDashboard/2.0)"
TOLERANCE_H = 0.5          # a module is due when (interval - 30min) has passed
MAX_IMG_BYTES = 12 * 1024 * 1024
PERSIAN = re.compile(r"[؀-ۿ]")


def now():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or now()).isoformat(timespec="seconds")


def read_json(path, default):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path, obj):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# --------------------------------------------------------------------------
# Telegram (text + images)
# --------------------------------------------------------------------------
BG_URL = re.compile(r"url\(['\"]?([^'\")]+)")


def parse_tg(page):
    soup = BeautifulSoup(page, "html.parser")
    title_el = soup.select_one(".tgme_channel_info_header_title")
    title = title_el.get_text(strip=True) if title_el else None
    posts = []
    for wrap in soup.select(".tgme_widget_message_wrap"):
        msg = wrap.select_one(".tgme_widget_message")
        if not msg or not msg.get("data-post"):
            continue
        text = ""
        text_el = wrap.select_one(".tgme_widget_message_text")
        if text_el:
            for br in text_el.find_all("br"):
                br.replace_with("\n")
            text = text_el.get_text().strip()
        images = []
        for a in wrap.select("a.tgme_widget_message_photo_wrap"):
            m = BG_URL.search(a.get("style", ""))
            if m:
                images.append(m.group(1))
        has_video = bool(
            wrap.select_one(".tgme_widget_message_video_wrap, .tgme_widget_message_roundvideo_wrap, video")
        )
        if not text and not images:
            continue  # nothing we can show (video-only posts are skipped)
        time_el = wrap.select_one("time[datetime]")
        views_el = wrap.select_one(".tgme_widget_message_views")
        posts.append(
            {
                "id": int(msg["data-post"].split("/")[-1]),
                "text": text,
                "images_remote": images,
                "has_video": has_video,
                "date": time_el["datetime"] if time_el else None,
                "views": views_el.get_text(strip=True) if views_el else None,
                "url": f"https://t.me/{msg['data-post']}",
            }
        )
    ids = [int(m["data-post"].split("/")[-1]) for m in soup.select(".tgme_widget_message[data-post]")]
    return title, posts, (min(ids) if ids else None)


def fetch_channel(channel, max_posts):
    base = f"https://t.me/s/{channel}"
    title, posts, before = None, [], None
    for _ in range(12):
        page = http_get(base if before is None else f"{base}?before={before}").decode("utf-8", "replace")
        t, found, oldest = parse_tg(page)
        title = title or t
        posts.extend(found)
        if len(posts) >= max_posts or oldest is None or oldest <= 1:
            break
        before = oldest
    return title, posts


def download_images(posts):
    MEDIA.mkdir(parents=True, exist_ok=True)
    for p in posts:
        imgs = []
        for i, src in enumerate(p.pop("images_remote", [])):
            ext = pathlib.Path(urllib.parse.urlparse(src).path).suffix.lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                ext = ".jpg"
            name = f"{p['id']}_{i}{ext}"
            dest = MEDIA / name
            if not dest.exists():
                try:
                    data = http_get(src, timeout=45)
                    if len(data) > MAX_IMG_BYTES:
                        raise ValueError("too large")
                    dest.write_bytes(data)
                except Exception as e:  # keep remote link as fallback
                    print(f"  image failed ({name}): {e}")
                    name = None
            imgs.append({"src": f"media/tg/{name}" if name else None, "remote": src})
        p["images"] = imgs


def run_telegram(cfg, prev, args):
    channel = cfg["channel"].lstrip("@").strip()
    max_posts = int(cfg.get("max_posts", 100))
    tg_file = next((a.split("=", 1)[1] for a in args if a.startswith("--tg-file=")), None)
    if tg_file:  # offline test mode
        title, posts, _ = parse_tg(pathlib.Path(tg_file).read_text(encoding="utf-8"))
    else:
        title, posts = fetch_channel(channel, max_posts)
    if not posts:
        raise RuntimeError("no posts found (channel private, name wrong, or page blocked)")
    download_images(posts)

    old_posts = prev.get("posts")
    if old_posts is None:  # migrate from the first version of this project
        old_posts = read_json(DOCS / "data.json", {}).get("posts", [])
    merged = {p["id"]: p for p in old_posts}
    merged.update({p["id"]: p for p in posts})
    kept = sorted(merged.values(), key=lambda p: p["id"], reverse=True)[:max_posts]

    referenced = {pathlib.Path(im["src"]).name for p in kept for im in p.get("images", []) if im.get("src")}
    for f in MEDIA.glob("*"):
        if f.name not in referenced:
            f.unlink()
    return {"status": "ok", "channel": channel, "title": title or channel, "updated": iso(), "posts": kept}


# --------------------------------------------------------------------------
# YouTube (official Data API v3)
# --------------------------------------------------------------------------
API = "https://www.googleapis.com/youtube/v3/"


def yt_api(endpoint, **params):
    url = API + endpoint + "?" + urllib.parse.urlencode(params)
    try:
        return json.loads(http_get(url).decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"YouTube API {e.code}: {body}")


def duration_s(iso_dur):
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_dur or "")
    if not m:
        return 0
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def title_key(title):
    return re.sub(r"[\W_]+", "", (title or "").lower())


def run_youtube(cfg, prev, args):
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        return {"status": "no_key"}
    queries = cfg["queries"]
    take = int(cfg.get("max_new_per_run", 10))
    keep = int(cfg.get("keep", 30))
    since = (now() - timedelta(days=int(cfg.get("days_back", 30)))).strftime("%Y-%m-%dT%H:%M:%SZ")

    seen_ids = list(prev.get("seen_ids", []))
    seen_titles = list(prev.get("seen_titles", []))
    seen_id_set, seen_title_set = set(seen_ids), set(seen_titles)

    candidates = []
    for q in queries:
        res = yt_api(
            "search", part="snippet", type="video", order="date", maxResults=25, q=q,
            relevanceLanguage="fa", publishedAfter=since, key=key,
        )
        candidates += [it["id"]["videoId"] for it in res.get("items", []) if it.get("id", {}).get("videoId")]
    ids = [i for i in dict.fromkeys(candidates) if i not in seen_id_set]

    details = []
    for i in range(0, len(ids), 50):
        res = yt_api("videos", part="snippet,contentDetails,statistics", id=",".join(ids[i:i + 50]), key=key)
        details += res.get("items", [])

    fresh = []
    for v in sorted(details, key=lambda v: v["snippet"]["publishedAt"], reverse=True):
        sn = v["snippet"]
        persian = PERSIAN.search(sn.get("title", "") + sn.get("channelTitle", "")) or str(
            sn.get("defaultAudioLanguage", "")
        ).startswith("fa")
        if not persian or sn.get("liveBroadcastContent", "none") != "none":
            continue
        tk = title_key(sn["title"])
        if tk in seen_title_set:
            continue  # same title as something already shown -> duplicate
        seen_title_set.add(tk)
        thumbs = sn.get("thumbnails", {})
        thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url")
        fresh.append(
            {
                "id": v["id"],
                "title": sn["title"],
                "channel": sn.get("channelTitle", ""),
                "published": sn["publishedAt"],
                "duration": duration_s(v.get("contentDetails", {}).get("duration")),
                "views": int(v.get("statistics", {}).get("viewCount", 0) or 0),
                "thumb": thumb,
                "url": f"https://www.youtube.com/watch?v={v['id']}",
                "first_seen": iso(),
            }
        )
        if len(fresh) >= take:
            break

    for it in fresh:
        seen_ids.append(it["id"])
        seen_titles.append(title_key(it["title"]))
    items = sorted(fresh + prev.get("items", []), key=lambda x: x["published"], reverse=True)[:keep]
    return {
        "status": "ok",
        "updated": iso(),
        "new_ids": [it["id"] for it in fresh],
        "items": items,
        "seen_ids": seen_ids[-1500:],
        "seen_titles": seen_titles[-1500:],
    }


# --------------------------------------------------------------------------
# Runner + site build
# --------------------------------------------------------------------------
MODULES = {"telegram": run_telegram, "youtube": run_youtube}


def main():
    args = sys.argv[1:]
    force = "--force" in args or os.environ.get("FORCE") == "1"
    only = next((a.split("=", 1)[1] for a in args if a.startswith("--only=")), None)
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    state = read_json(DATA / "state.json", {})

    for name, fn in MODULES.items():
        mcfg = cfg.get(name)
        if not mcfg or (only and only != name):
            continue
        interval = float(mcfg.get("interval_hours", 12))
        last = state.get(name)
        if last and not (force or only == name):
            elapsed_h = (now() - datetime.fromisoformat(last)).total_seconds() / 3600
            if elapsed_h < interval - TOLERANCE_H:
                print(f"[{name}] skipped (next in ~{interval - elapsed_h:.1f}h)")
                continue
        prev = read_json(DATA / f"{name}.json", {})
        try:
            out = fn(mcfg, prev, args)
            print(f"[{name}] {out.get('status')}")
            if out.get("status") == "ok":
                state[name] = iso()
        except Exception as e:
            print(f"[{name}] ERROR: {e}")
            out = dict(prev)
            out["error"] = str(e)[:300]
            out["error_at"] = iso()
        write_json(DATA / f"{name}.json", out)
    write_json(DATA / "state.json", state)

    yt = read_json(DATA / "youtube.json", {})
    yt.pop("seen_ids", None)
    yt.pop("seen_titles", None)
    site = {
        "generated": iso(),
        "intervals": {k: cfg.get(k, {}).get("interval_hours") for k in MODULES},
        "telegram": read_json(DATA / "telegram.json", {}),
        "youtube": yt,
        "divar": cfg.get("divar", {}),
        "prices": cfg.get("prices", {}),
    }
    template = (ROOT / "template.html").read_text(encoding="utf-8")
    blob = json.dumps(site, ensure_ascii=False).replace("</", "<\\/")
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(template.replace("__DATA__", blob), encoding="utf-8")
    print("site built")


if __name__ == "__main__":
    main()
