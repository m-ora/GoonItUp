#!/usr/bin/env python3
"""Local Reddit media gallery. Serves the page and proxies Reddit listings."""

from __future__ import annotations

import base64
import html
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 8765
REDIRECT = f"http://127.0.0.1:{PORT}/oauth"
TOKEN_PATH = ROOT / ".reddit.json"
OAUTH_UA = "windows:goonitup-gallery:1.0 (local slideshow)"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
SUB_RE = re.compile(r"^[A-Za-z0-9_+\-]+$")
SORTS = {"hot", "new", "top", "rising", "best"}
TIMES = {"hour", "day", "week", "month", "year", "all"}
BLOCK = re.compile(
    r"\b(loli|lolita|shota|jailbait|preteen|pre-teen|underage|child\s*porn|pedophil)",
    re.I,
)
ATOM = {"a": "http://www.w3.org/2005/Atom", "m": "http://search.yahoo.com/mrss/"}
HREF_RE = re.compile(r'href="([^"]+)"', re.I)
IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.I)
VREDDIT_RE = re.compile(r"v\.redd\.it/([A-Za-z0-9]+)", re.I)
REDGIF_RE = re.compile(
    r"(?:redgifs\.com|gifdeliverynetwork\.com)/(?:watch/|ifr/)?([A-Za-z0-9]+)",
    re.I,
)
PREVIEW_RE = re.compile(r"preview\.redd\.it/([A-Za-z0-9._-]+)", re.I)

_lock = threading.Lock()
_oauth_state = {"value": None}
_redgifs_token = None
_last_anon = 0.0


def load_creds() -> dict:
    if not TOKEN_PATH.exists():
        return {}
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_creds(data: dict) -> None:
    TOKEN_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def clean_url(value: str | None) -> str:
    if not value:
        return ""
    return html.unescape(value).replace("&amp;", "&").strip()


def blocked(title: str, subreddit: str = "", flair: str = "") -> bool:
    return bool(BLOCK.search(f"{title} {subreddit} {flair}"))


def http_request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict | None = None,
    method: str | None = None,
    timeout: int = 25,
) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            meta = {k.lower(): v for k, v in resp.headers.items()}
            return resp.getcode() or 200, resp.read(), meta
    except urllib.error.HTTPError as err:
        body = err.read() if err.fp else b""
        meta = {k.lower(): v for k, v in err.headers.items()} if err.headers else {}
        return err.code, body, meta


def reddit_json(url: str, token: str | None = None) -> tuple[int, dict]:
    headers = {
        "User-Agent": OAUTH_UA if token else BROWSER_UA,
        "Accept": "application/json",
        "Cookie": "over18=1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, raw, _ = http_request(url, headers=headers)
    if not raw:
        return status, {"error": f"Reddit {status}"}
    try:
        return status, json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return status, {"error": raw.decode("utf-8", errors="replace")[:240]}


def basic_auth(client_id: str, secret: str) -> str:
    blob = f"{client_id}:{secret or ''}".encode("utf-8")
    return "Basic " + base64.b64encode(blob).decode("ascii")


def token_request(creds: dict, body: dict) -> tuple[int, dict]:
    encoded = urllib.parse.urlencode(body).encode("utf-8")
    status, raw, _ = http_request(
        "https://www.reddit.com/api/v1/access_token",
        data=encoded,
        headers={
            "User-Agent": OAUTH_UA,
            "Authorization": basic_auth(creds.get("client_id", ""), creds.get("client_secret", "")),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        payload = {"error": raw.decode("utf-8", errors="replace")[:240]}
    return status, payload


def apply_token(creds: dict, payload: dict) -> dict:
    if payload.get("access_token"):
        creds["access_token"] = payload["access_token"]
        creds["token_type"] = payload.get("token_type", "bearer")
        creds["expires_at"] = time.time() + int(payload.get("expires_in") or 3600) - 30
        if payload.get("refresh_token"):
            creds["refresh_token"] = payload["refresh_token"]
        save_creds(creds)
    return creds


def valid_token() -> str | None:
    creds = load_creds()
    token = creds.get("access_token")
    if token and creds.get("expires_at", 0) > time.time():
        return token
    if creds.get("refresh_token") and creds.get("client_id"):
        status, payload = token_request(
            creds,
            {
                "grant_type": "refresh_token",
                "refresh_token": creds["refresh_token"],
            },
        )
        if status == 200:
            apply_token(creds, payload)
            return creds.get("access_token")
    return None


def whoami(token: str) -> str:
    status, payload = reddit_json("https://oauth.reddit.com/api/v1/me", token)
    if status == 200 and isinstance(payload, dict):
        return payload.get("name") or ""
    return ""


def wait_anon() -> None:
    global _last_anon
    with _lock:
        gap = 3.2 - (time.time() - _last_anon)
        if gap > 0:
            time.sleep(gap)
        _last_anon = time.time()


def item(
    *,
    post_id: str,
    media_type: str,
    src: str,
    title: str,
    subreddit: str,
    author: str,
    permalink: str,
    poster: str = "",
    score: int = 0,
    over18: bool = False,
) -> dict | None:
    if not src or blocked(title, subreddit):
        return None
    return {
        "postId": post_id,
        "type": media_type,
        "src": clean_url(src),
        "poster": clean_url(poster),
        "title": title,
        "subreddit": subreddit,
        "author": author.lstrip("/u"),
        "score": score,
        "permalink": permalink,
        "over18": over18,
    }


def classify_direct(url: str) -> tuple[str, str] | None:
    url = clean_url(url)
    if not url:
        return None
    if REDGIF_RE.search(url):
        return "redgifs", url
    if VREDDIT_RE.search(url):
        return "vreddit", url
    if url.lower().endswith(".gifv") and "imgur.com" in url:
        return "video", re.sub(r"\.gifv$", ".mp4", url, flags=re.I)
    if re.search(r"\.(mp4|webm|mov)(\?|$)", url, re.I):
        return "video", url
    if re.search(r"\.(jpe?g|png|webp|avif|gif)(\?|$)", url, re.I):
        return "image", url
    if "i.redd.it" in url or "i.imgur.com" in url:
        return "image", url
    return None


def preview_to_ireddit(url: str) -> str:
    match = PREVIEW_RE.search(url or "")
    if match:
        return f"https://i.redd.it/{match.group(1)}"
    return clean_url(url)


def from_gallery(d: dict) -> list[dict]:
    out = []
    meta = d.get("media_metadata") or {}
    order = d.get("gallery_data", {}).get("items") or [{"media_id": key} for key in meta]
    info = (
        d.get("id") or "",
        d.get("title") or "",
        d.get("subreddit") or "",
        d.get("author") or "",
        f"https://www.reddit.com{d.get('permalink')}" if d.get("permalink") else "",
        int(d.get("score") or 0),
        bool(d.get("over_18")),
    )
    for entry in order:
        media = meta.get(entry.get("media_id"), {})
        if not media or media.get("status") == "failed":
            continue
        source = media.get("s") or {}
        if media.get("e") == "AnimatedImage" and (source.get("mp4") or source.get("gif")):
            row = item(
                post_id=info[0],
                media_type="video",
                src=source.get("mp4") or source.get("gif"),
                poster=source.get("gif") or source.get("u") or "",
                title=info[1],
                subreddit=info[2],
                author=info[3],
                permalink=info[4],
                score=info[5],
                over18=info[6],
            )
        elif source.get("u") or source.get("gif"):
            row = item(
                post_id=info[0],
                media_type="image",
                src=source.get("u") or source.get("gif"),
                title=info[1],
                subreddit=info[2],
                author=info[3],
                permalink=info[4],
                score=info[5],
                over18=info[6],
            )
        else:
            row = None
        if row:
            out.append(row)
    return out


def from_post(d: dict) -> list[dict]:
    if not d or d.get("stickied"):
        return []
    if d.get("crosspost_parent_list"):
        return from_post(d["crosspost_parent_list"][0])
    if blocked(d.get("title") or "", d.get("subreddit") or "", d.get("link_flair_text") or ""):
        return []
    if d.get("is_gallery") and d.get("media_metadata"):
        return from_gallery(d)
    hosted = (
        (d.get("secure_media") or {}).get("reddit_video")
        or (d.get("media") or {}).get("reddit_video")
        or (d.get("preview") or {}).get("reddit_video_preview")
    )
    permalink = f"https://www.reddit.com{d['permalink']}" if d.get("permalink") else ""
    preview = ""
    try:
        preview = d["preview"]["images"][0]["source"]["url"]
    except Exception:
        pass
    if hosted and hosted.get("fallback_url"):
        row = item(
            post_id=d.get("id") or "",
            media_type="video",
            src=hosted["fallback_url"],
            poster=preview,
            title=d.get("title") or "",
            subreddit=d.get("subreddit") or "",
            author=d.get("author") or "",
            permalink=permalink,
            score=int(d.get("score") or 0),
            over18=bool(d.get("over_18")),
        )
        return [row] if row else []
    url = d.get("url_overridden_by_dest") or d.get("url") or ""
    classified = classify_direct(url)
    if classified:
        row = item(
            post_id=d.get("id") or "",
            media_type=classified[0],
            src=classified[1],
            poster=preview,
            title=d.get("title") or "",
            subreddit=d.get("subreddit") or "",
            author=d.get("author") or "",
            permalink=permalink,
            score=int(d.get("score") or 0),
            over18=bool(d.get("over_18")),
        )
        return [row] if row else []
    if preview:
        row = item(
            post_id=d.get("id") or "",
            media_type="image",
            src=preview_to_ireddit(preview),
            title=d.get("title") or "",
            subreddit=d.get("subreddit") or "",
            author=d.get("author") or "",
            permalink=permalink,
            score=int(d.get("score") or 0),
            over18=bool(d.get("over_18")),
        )
        return [row] if row else []
    return []


def listing_from_json(payload: dict) -> dict:
    children = payload.get("data", {}).get("children") or []
    items = []
    for child in children:
        items.extend(from_post(child.get("data") or {}))
    return {
        "after": payload.get("data", {}).get("after"),
        "source": "api",
        "items": items,
    }


def fetch_oauth_listing(sub: str, sort: str, after: str, time_filter: str, limit: int) -> tuple[int, dict]:
    token = valid_token()
    if not token:
        return 401, {"error": "not connected"}
    query = {"limit": str(limit), "raw_json": "1"}
    if after:
        query["after"] = after
    if sort == "top":
        query["t"] = time_filter
    url = f"https://oauth.reddit.com/r/{sub}/{sort}?{urllib.parse.urlencode(query)}"
    status, payload = reddit_json(url, token)
    if status != 200:
        return status, {"error": payload.get("message") or payload.get("error") or f"Reddit {status}"}
    return 200, listing_from_json(payload)


def parse_rss(xml_text: str, fallback_sub: str) -> dict:
    root = ET.fromstring(xml_text)
    items = []
    last_id = None
    for entry in root.findall("a:entry", ATOM):
        post_id = (entry.findtext("a:id", default="", namespaces=ATOM) or "").replace("t3_", "")
        last_id = entry.findtext("a:id", default="", namespaces=ATOM) or last_id
        title = entry.findtext("a:title", default="", namespaces=ATOM) or ""
        author = (entry.findtext("a:author/a:name", default="", namespaces=ATOM) or "").replace("/u/", "")
        permalink = ""
        link = entry.find("a:link", ATOM)
        if link is not None:
            permalink = link.attrib.get("href") or ""
        subreddit = fallback_sub.split("+")[0]
        category = entry.find("a:category", ATOM)
        if category is not None:
            subreddit = category.attrib.get("term") or subreddit
        if blocked(title, subreddit):
            continue
        content = html.unescape(entry.findtext("a:content", default="", namespaces=ATOM) or "")
        imgs = [html.unescape(src) for src in IMG_RE.findall(content)]
        thumb = entry.find("m:thumbnail", ATOM)
        thumb_url = clean_url(thumb.attrib.get("url") if thumb is not None else "")
        link_match = re.search(r'href="([^"]+)"\s*>\s*\[link\]', content, re.I)
        media_url = html.unescape(link_match.group(1)) if link_match else ""
        classified = classify_direct(media_url)
        src = ""
        media_type = "image"
        if classified:
            media_type, src = classified
        elif media_url and "/gallery/" in media_url:
            src = thumb_url or (imgs[0] if imgs else "")
            media_type = "image"
        elif thumb_url or imgs:
            src = preview_to_ireddit(thumb_url or imgs[0])
            media_type = "image"
        row = item(
            post_id=post_id,
            media_type=media_type,
            src=src,
            poster=clean_url(thumb_url),
            title=title,
            subreddit=subreddit,
            author=author,
            permalink=permalink,
        )
        if row:
            items.append(row)
    after = last_id if last_id and last_id.startswith("t3_") else None
    return {"after": after, "source": "rss", "items": items}


def fetch_rss_listing(sub: str, sort: str, after: str, time_filter: str, limit: int) -> tuple[int, dict]:
    wait_anon()
    query = {"limit": str(min(limit, 100)), "include_over_18": "on"}
    if after:
        query["after"] = after
    if sort == "top":
        query["t"] = time_filter
    path = f"/r/{sub}/{sort}.rss"
    url = f"https://www.reddit.com{path}?{urllib.parse.urlencode(query)}"
    status, raw, meta = http_request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "application/atom+xml,application/xml,text/xml,*/*",
            "Cookie": "over18=1",
        },
    )
    if status == 429:
        retry = meta.get("retry-after", "60")
        return 429, {"error": f"Reddit rate-limited RSS. Retry in {retry}s.", "retry_after": retry}
    if status != 200:
        text = raw.decode("utf-8", errors="replace")[:240]
        return status, {"error": f"Reddit RSS {status}: {text}"}
    text = raw.decode("utf-8", errors="replace")
    if "<feed" not in text and "<rss" not in text:
        return 502, {"error": "Reddit RSS did not return a feed. Try again in a minute."}
    try:
        parsed = parse_rss(text, sub)
    except ET.ParseError:
        return 502, {"error": "Could not parse Reddit RSS."}
    return 200, parsed


def fetch_listing(sub: str, sort: str, after: str, time_filter: str, limit: int) -> tuple[int, dict]:
    if valid_token():
        status, payload = fetch_oauth_listing(sub, sort, after, time_filter, limit)
        if status == 200:
            return status, payload
    return fetch_rss_listing(sub, sort, after, time_filter, limit)


def round_robin(buckets: list[list[dict]]) -> list[dict]:
    out: list[dict] = []
    index = 0
    while True:
        added = False
        for bucket in buckets:
            if index < len(bucket):
                out.append(bucket[index])
                added = True
        if not added:
            break
        index += 1
    return out


def interleave_by_sub(items: list[dict], names: list[str]) -> list[dict]:
    wanted = [name.lower() for name in names]
    buckets = {name: [] for name in wanted}
    extra: list[dict] = []
    for row in items:
        key = (row.get("subreddit") or "").lower()
        if key in buckets:
            buckets[key].append(row)
        else:
            extra.append(row)
    lists = [buckets[name] for name in wanted]
    if extra:
        lists.append(extra)
    return round_robin(lists)


def parse_after_map(after: str) -> dict[str, str]:
    if not after:
        return {}
    if ":" not in after:
        return {}
    out: dict[str, str] = {}
    for part in after.split(","):
        if ":" not in part:
            continue
        name, cursor = part.split(":", 1)
        name = name.strip().lower()
        cursor = cursor.strip()
        if name and cursor:
            out[name] = cursor
    return out


def encode_after_map(mapping: dict[str, str | None]) -> str | None:
    parts = [f"{name}:{cursor}" for name, cursor in mapping.items() if cursor]
    return ",".join(parts) if parts else None


def fetch_each_sub(names: list[str], sort: str, after: str, time_filter: str, limit: int) -> tuple[int, dict]:
    after_map = parse_after_map(after)
    per = max(3, (limit + max(len(names), 1) - 1) // max(len(names), 1))
    buckets: list[list[dict]] = []
    new_after: dict[str, str | None] = {}
    sources: set[str] = set()
    last_error = None
    for name in names:
        cursor = after_map.get(name.lower(), "")
        status, payload = fetch_listing(name, sort, cursor, time_filter, per)
        if status == 429:
            if any(buckets):
                break
            return status, payload
        if status != 200:
            last_error = payload
            buckets.append([])
            new_after[name.lower()] = cursor or None
            continue
        buckets.append(payload.get("items") or [])
        new_after[name.lower()] = payload.get("after")
        sources.add(payload.get("source") or "rss")
    items = round_robin(buckets)[:limit]
    if not items and last_error:
        return 502, last_error
    return 200, {
        "items": items,
        "after": encode_after_map(new_after),
        "source": "api" if "api" in sources else "rss",
        "subs": names,
    }


def fetch_media(sub: str, sort: str, after: str, time_filter: str, limit: int) -> tuple[int, dict]:
    names = [part for part in sub.split("+") if part]
    if len(names) <= 1:
        status, payload = fetch_listing(sub, sort, after, time_filter, limit)
        if status == 200:
            payload["subs"] = names or [sub]
        return status, payload
    if valid_token():
        return fetch_each_sub(names, sort, after, time_filter, limit)
    cursor = after if after.startswith("t3_") else ""
    status, payload = fetch_listing(sub, sort, cursor, time_filter, limit)
    if status != 200:
        return status, payload
    payload["items"] = interleave_by_sub(payload.get("items") or [], names)[:limit]
    payload["subs"] = names
    return status, payload


VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv"}
BOORU_UA = "goonitup-gallery/1.0 (personal local slideshow)"
TAGS_RE = re.compile(r"^[A-Za-z0-9_:\-\*\(\)\/~! \.]+$")


def media_type_from_url(url: str) -> str:
    lower = (url or "").split("?", 1)[0].lower()
    for ext in VIDEO_EXT:
        if lower.endswith(ext):
            return "video"
    return "image"


def booru_item(site: str, post_id: str, src: str, poster: str, title: str, author: str, score: int, permalink: str) -> dict | None:
    if not src or blocked(title, site, ""):
        return None
    return item(
        post_id=f"{site}_{post_id}",
        media_type=media_type_from_url(src),
        src=src,
        poster=poster,
        title=title,
        subreddit=site,
        author=author or site,
        permalink=permalink,
        score=int(score or 0),
        over18=True,
    )


def extract_gel_posts(payload) -> list:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        posts = payload.get("post") or payload.get("posts") or []
        if isinstance(posts, dict):
            posts = [posts]
        return [row for row in posts if isinstance(row, dict)]
    return []


def fetch_e621(tags: str, page: int, limit: int) -> tuple[int, dict]:
    query = urllib.parse.urlencode(
        {"tags": tags, "limit": str(limit), "page": str(max(1, page + 1))}
    )
    status, raw, _ = http_request(
        f"https://e621.net/posts.json?{query}",
        headers={"User-Agent": BOORU_UA, "Accept": "application/json"},
    )
    if status != 200:
        text = raw.decode("utf-8", errors="replace")[:240]
        return status, {"error": f"e621 {status}: {text}"}
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return 502, {"error": "e621 returned invalid JSON."}
    items = []
    for post in data.get("posts") or []:
        file_info = post.get("file") or {}
        preview = (post.get("preview") or {}).get("url") or ""
        sample = (post.get("sample") or {}).get("url") or ""
        src = file_info.get("url") or sample or preview
        tags = post.get("tags") or {}
        tag_blob = " ".join(tags.get("general") or [])[:80]
        row = booru_item(
            "e621",
            str(post.get("id") or ""),
            src or "",
            sample or preview,
            tag_blob or f"e621 {post.get('id')}",
            (post.get("tags") or {}).get("artist", ["unknown"])[0] if (post.get("tags") or {}).get("artist") else "e621",
            (post.get("score") or {}).get("total") or 0,
            f"https://e621.net/posts/{post.get('id')}",
        )
        if row:
            items.append(row)
    return 200, {"items": items, "after": str(page + 1) if items else None, "source": "e621"}


def fetch_gel_style(site: str, base: str, view: str, tags: str, page: int, limit: int) -> tuple[int, dict]:
    params = {
        "page": "dapi",
        "s": "post",
        "q": "index",
        "json": "1",
        "limit": str(limit),
        "pid": str(max(0, page)),
        "tags": tags,
    }
    creds = load_creds()
    key_name = "gelbooru" if site == "gelbooru" else "r34"
    api_key = creds.get(f"{key_name}_api_key") or ""
    user_id = creds.get(f"{key_name}_user_id") or ""
    if api_key and user_id:
        params["api_key"] = api_key
        params["user_id"] = user_id
    url = f"{base}?{urllib.parse.urlencode(params)}"
    status, raw, _ = http_request(
        url,
        headers={"User-Agent": BOORU_UA, "Accept": "application/json"},
    )
    text = raw.decode("utf-8", errors="replace")
    if status != 200:
        return status, {"error": f"{site} {status}: {text[:240]}"}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return 502, {"error": f"{site} returned invalid JSON."}
    if isinstance(payload, str) and "auth" in payload.lower():
        where = (
            "https://gelbooru.com/index.php?page=account&s=options"
            if site == "gelbooru"
            else "https://rule34.xxx/index.php?page=account&s=options"
        )
        return 401, {"error": f"{site} needs an API key. Create an account and paste user id + api key from {where}"}
    items = []
    for post in extract_gel_posts(payload):
        src = post.get("file_url") or post.get("sample_url") or post.get("preview_url") or ""
        if src and src.startswith("//"):
            src = "https:" + src
        poster = post.get("preview_url") or post.get("sample_url") or ""
        if poster and poster.startswith("//"):
            poster = "https:" + poster
        post_id = str(post.get("id") or "")
        row = booru_item(
            site,
            post_id,
            src,
            poster,
            (post.get("tags") or "")[:80] or f"{site} {post_id}",
            post.get("owner") or site,
            int(post.get("score") or 0),
            f"{view}{post_id}",
        )
        if row:
            items.append(row)
    return 200, {"items": items, "after": str(page + 1) if items else None, "source": site}


RB_ITEM_RE = re.compile(
    r'id="p(\d+)"\s+href="([^"]+)"\s*>\s*<img\s+src="([^"]+)"\s+title="([^"]*)"',
    re.I,
)
RB_THUMB_RE = re.compile(
    r"/thumbnails/([0-9a-f]{2})/([0-9a-f]{2})/thumbnail_([0-9a-f]+)\.",
    re.I,
)
FILE_HOSTS = {"realbooru.com", "www.realbooru.com"}


def realbooru_probe(url: str) -> bool:
    status, raw, meta = http_request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Referer": "https://realbooru.com/",
            "Range": "bytes=0-16",
        },
        timeout=8,
    )
    ctype = (meta.get("content-type") or "").lower()
    return status in (200, 206) and (ctype.startswith("image/") or ctype.startswith("video/") or (raw[:3] == b"\xff\xd8\xff") or raw[:4] == b"\x89PNG")


def fetch_realbooru(tags: str, page: int, limit: int) -> tuple[int, dict]:
    tags = tags.replace(" ", "+")
    pid = max(0, page) * max(limit, 40)
    query = urllib.parse.urlencode({"page": "post", "s": "list", "tags": tags, "pid": str(pid)})
    status, raw, _ = http_request(
        f"https://realbooru.com/index.php?{query}",
        headers={"User-Agent": BROWSER_UA, "Accept": "text/html", "Referer": "https://realbooru.com/"},
    )
    if status != 200:
        text = raw.decode("utf-8", errors="replace")[:240]
        return status, {"error": f"realbooru {status}: {text}"}
    html = raw.decode("utf-8", errors="replace")
    items = []
    for match in RB_ITEM_RE.finditer(html):
        post_id, href, thumb, title = match.groups()
        href = html_unescape(href)
        thumb = html_unescape(thumb)
        title = html_unescape(title)
        src = thumb
        hashed = RB_THUMB_RE.search(thumb)
        if hashed:
            a, b, digest = hashed.groups()
            videoish = bool(re.search(r"\b(webm|mp4|video|animated)\b", title, re.I))
            exts = (".webm", ".mp4", ".jpeg", ".jpg") if videoish else (".jpeg", ".jpg", ".png", ".gif", ".webp")
            for ext in exts:
                candidate = f"https://realbooru.com/images/{a}/{b}/{digest}{ext}"
                if realbooru_probe(candidate):
                    src = candidate
                    break
        row = booru_item(
            "realbooru",
            post_id,
            f"/api/file?u={urllib.parse.quote(src, safe='')}",
            f"/api/file?u={urllib.parse.quote(thumb, safe='')}",
            title[:80] or f"realbooru {post_id}",
            "realbooru",
            0,
            f"https://realbooru.com/index.php?page=post&s=view&id={post_id}",
        )
        if row:
            items.append(row)
        if len(items) >= limit:
            break
    return 200, {"items": items, "after": str(page + 1) if items else None, "source": "realbooru"}


def html_unescape(value: str) -> str:
    return html.unescape(value.replace("&amp;", "&")).strip()


def fetch_booru(site: str, tags: str, page: int, limit: int) -> tuple[int, dict]:
    tags = (tags or "").strip() or "*"
    if not TAGS_RE.match(tags):
        return 400, {"error": "Tags can only use letters, numbers, and : _ - * () / ~ ."}
    if blocked(tags, site, ""):
        return 400, {"error": "Those tags are blocked."}
    limit = min(50, max(5, limit))
    page = max(0, page)
    if site == "e621":
        return fetch_e621(tags, page, limit)
    if site == "gelbooru":
        return fetch_gel_style(
            "gelbooru",
            "https://gelbooru.com/index.php",
            "https://gelbooru.com/index.php?page=post&s=view&id=",
            tags,
            page,
            limit,
        )
    if site in {"rule34", "r34"}:
        return fetch_gel_style(
            "rule34",
            "https://api.rule34.xxx/index.php",
            "https://rule34.xxx/index.php?page=post&s=view&id=",
            tags,
            page,
            limit,
        )
    if site in {"realbooru", "rb"}:
        return fetch_realbooru(tags, page, limit)
    return 400, {"error": "Unknown booru. Use e621, gelbooru, rule34, or realbooru."}


def redgifs_token() -> str | None:
    global _redgifs_token
    if _redgifs_token:
        return _redgifs_token
    status, raw, _ = http_request(
        "https://api.redgifs.com/v2/auth/temporary",
        headers={"User-Agent": OAUTH_UA, "Accept": "application/json"},
    )
    if status != 200:
        return None
    try:
        _redgifs_token = json.loads(raw.decode("utf-8")).get("token")
    except Exception:
        return None
    return _redgifs_token


def fetch_redgif(gif_id: str) -> tuple[int, dict]:
    token = redgifs_token()
    if not token:
        return 502, {"error": "Could not get a RedGifs token."}
    status, raw, _ = http_request(
        f"https://api.redgifs.com/v2/gifs/{urllib.parse.quote(gif_id)}",
        headers={
            "User-Agent": OAUTH_UA,
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    if status == 401:
        global _redgifs_token
        _redgifs_token = None
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return status, {"error": f"RedGifs {status}"}
    urls = (data.get("gif") or {}).get("urls") or {}
    src = urls.get("hd") or urls.get("sd")
    if not src:
        return 404, {"error": "No playable RedGifs URL."}
    return 200, {"src": src, "poster": urls.get("poster") or urls.get("thumbnail") or "", "type": "video"}


def resolve_vreddit(url_or_id: str) -> tuple[int, dict]:
    match = VREDDIT_RE.search(url_or_id) or re.fullmatch(r"[A-Za-z0-9]+", url_or_id)
    vid = match.group(1) if match and match.lastindex else (match.group(0) if match else "")
    if not vid:
        return 400, {"error": "Invalid v.redd.it id."}
    for quality in ("1080", "720", "480", "360", "220"):
        src = f"https://v.redd.it/{vid}/DASH_{quality}.mp4?source=fallback"
        status, _, _ = http_request(
            src,
            method="HEAD",
            headers={"User-Agent": BROWSER_UA, "Range": "bytes=0-1"},
            timeout=12,
        )
        if status in (200, 206):
            return 200, {"src": src, "type": "video"}
        status, raw, meta = http_request(
            src,
            headers={"User-Agent": BROWSER_UA, "Range": "bytes=0-1"},
            timeout=12,
        )
        if status in (200, 206) and (meta.get("content-type") or "").startswith("video"):
            return 200, {"src": src, "type": "video"}
        if status in (200, 206) and raw:
            return 200, {"src": src, "type": "video"}
    return 404, {"error": "No playable Reddit video file."}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        if args and str(args[0]).startswith(("GET /api/", "POST /api/", "GET /oauth")):
            super().log_message(fmt, *args)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/creds":
            body = self._read_json()
            creds = load_creds()
            client_id = str(body.get("client_id") or "").strip()
            secret = str(body.get("client_secret") or "").strip()
            if not re.match(r"^[A-Za-z0-9_\-]{10,64}$", client_id):
                return self._json(400, {"error": "That client id does not look valid."})
            creds["client_id"] = client_id
            creds["client_secret"] = secret
            save_creds(creds)
            return self._json(200, {"ok": True, "redirect": self.authorize_url(creds)})
        if parsed.path == "/api/disconnect":
            creds = load_creds()
            for key in ("access_token", "refresh_token", "expires_at", "username"):
                creds.pop(key, None)
            save_creds(creds)
            return self._json(200, {"ok": True})
        if parsed.path == "/api/booru-creds":
            body = self._read_json()
            creds = load_creds()
            for key in ("gelbooru_user_id", "gelbooru_api_key", "r34_user_id", "r34_api_key"):
                if key in body:
                    creds[key] = str(body.get(key) or "").strip()
            save_creds(creds)
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "Unknown API route."})

    def authorize_url(self, creds: dict) -> str:
        state = secrets.token_urlsafe(16)
        _oauth_state["value"] = state
        query = urllib.parse.urlencode(
            {
                "client_id": creds["client_id"],
                "response_type": "code",
                "state": state,
                "redirect_uri": REDIRECT,
                "duration": "permanent",
                "scope": "read identity",
            }
        )
        return f"https://www.reddit.com/api/v1/authorize?{query}"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/oauth":
            return self.handle_oauth(parsed)
        if parsed.path == "/api/listing":
            return self.handle_listing(parsed)
        if parsed.path == "/api/booru":
            return self.handle_booru(parsed)
        if parsed.path == "/api/file":
            return self.handle_file(parsed)
        if parsed.path == "/api/redgifs":
            return self.handle_redgifs(parsed)
        if parsed.path == "/api/vreddit":
            return self.handle_vreddit(parsed)
        if parsed.path == "/api/status":
            creds = load_creds()
            token = valid_token()
            return self._json(
                200,
                {
                    "connected": bool(token),
                    "username": creds.get("username") or "",
                    "has_client": bool(creds.get("client_id")),
                    "source": "api" if token else "rss",
                },
            )
        if parsed.path.startswith("/api/"):
            return self._json(404, {"error": "Unknown API route."})
        return super().do_GET()

    def handle_oauth(self, parsed: urllib.parse.ParseResult) -> None:
        qs = urllib.parse.parse_qs(parsed.query)
        error = (qs.get("error") or [""])[0]
        code = (qs.get("code") or [""])[0]
        state = (qs.get("state") or [""])[0]
        if error:
            return self.redirect(f"/?oauth=error&reason={urllib.parse.quote(error)}")
        if not code or state != _oauth_state.get("value"):
            return self.redirect("/?oauth=error&reason=bad_state")
        creds = load_creds()
        status, payload = token_request(
            creds,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
            },
        )
        if status != 200 or not payload.get("access_token"):
            return self.redirect("/?oauth=error&reason=token")
        apply_token(creds, payload)
        creds = load_creds()
        name = whoami(creds.get("access_token", ""))
        if name:
            creds["username"] = name
            save_creds(creds)
        return self.redirect("/?oauth=ok")

    def redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def handle_listing(self, parsed: urllib.parse.ParseResult) -> None:
        qs = urllib.parse.parse_qs(parsed.query)
        sub = (qs.get("sub") or ["pics"])[0].strip().replace(",", "+").replace(" ", "")
        sort = (qs.get("sort") or ["hot"])[0].strip().lower()
        after = (qs.get("after") or [""])[0].strip()
        time_filter = (qs.get("t") or ["day"])[0].strip().lower()
        try:
            limit = min(100, max(5, int((qs.get("limit") or ["100"])[0])))
        except ValueError:
            limit = 100
        if not SUB_RE.match(sub):
            return self._json(400, {"error": "Subreddit names can only use letters, numbers, _, +, and -."})
        if sort not in SORTS:
            return self._json(400, {"error": "Invalid sort."})
        if time_filter not in TIMES:
            time_filter = "day"
        if after and not re.match(r"^[A-Za-z0-9_+:,]+$", after):
            return self._json(400, {"error": "Invalid pagination cursor."})
        status, payload = fetch_media(sub, sort, after, time_filter, limit)
        self._json(status, payload)

    def handle_booru(self, parsed: urllib.parse.ParseResult) -> None:
        qs = urllib.parse.parse_qs(parsed.query)
        site = (qs.get("site") or [""])[0].strip().lower()
        tags = (qs.get("tags") or ["*"])[0]
        try:
            page = max(0, int((qs.get("page") or ["0"])[0]))
        except ValueError:
            page = 0
        try:
            limit = min(50, max(5, int((qs.get("limit") or ["15"])[0])))
        except ValueError:
            limit = 15
        status, payload = fetch_booru(site, tags, page, limit)
        self._json(status, payload)

    def handle_file(self, parsed: urllib.parse.ParseResult) -> None:
        qs = urllib.parse.parse_qs(parsed.query)
        raw_url = (qs.get("u") or [""])[0]
        target = urllib.parse.urlparse(raw_url)
        host = (target.hostname or "").lower()
        if target.scheme != "https" or host not in FILE_HOSTS:
            return self._json(400, {"error": "That file host is not allowed."})
        status, raw, meta = http_request(
            raw_url,
            headers={
                "User-Agent": BROWSER_UA,
                "Referer": "https://realbooru.com/",
                "Accept": "image/*,video/*,*/*",
            },
            timeout=40,
        )
        ctype = meta.get("content-type") or "application/octet-stream"
        if status not in (200, 206) or "text/html" in ctype:
            return self._json(status if status != 200 else 502, {"error": "Could not fetch Realbooru file."})
        body = raw
        self.send_response(200)
        self.send_header("Content-Type", ctype.split(";")[0])
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_redgifs(self, parsed: urllib.parse.ParseResult) -> None:
        qs = urllib.parse.parse_qs(parsed.query)
        gif_id = (qs.get("id") or [""])[0].strip()
        match = REDGIF_RE.search(gif_id) if gif_id else None
        if match:
            gif_id = match.group(1)
        if not re.match(r"^[A-Za-z0-9]+$", gif_id or ""):
            return self._json(400, {"error": "Invalid RedGifs id."})
        status, payload = fetch_redgif(gif_id)
        self._json(status, payload)

    def handle_vreddit(self, parsed: urllib.parse.ParseResult) -> None:
        qs = urllib.parse.parse_qs(parsed.query)
        vid = (qs.get("id") or [""])[0].strip()
        status, payload = resolve_vreddit(vid)
        self._json(status, payload)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Goonitup gallery → {url}")
    print("Leave this window open. Ctrl+C to stop.")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
