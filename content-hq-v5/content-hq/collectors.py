"""Data collectors. Each returns (items, error). Items are plain dicts:
platform, account, title, url, metric, value, baseline, outlier, published."""
import re
import statistics
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

UA = {"User-Agent": "ContentHQ/1.0 (+personal research tool)"}
TIMEOUT = 25


# ---------- helpers ----------
def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child_text(el, name):
    for ch in el:
        if _local(ch.tag) == name and ch.text:
            return ch.text.strip()
    return ""


def _iso(s):
    """Best-effort parse of RSS/Atom dates to ISO 8601 (UTC). Returns '' on failure."""
    if not s:
        return ""
    s = s.strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return ""


def score_outliers(items, min_samples=5):
    """Attach baseline (median) and outlier multiplier per account.
    Median, not mean, so one viral post does not distort the baseline."""
    by_acct = {}
    for it in items:
        if it.get("value") is not None:
            by_acct.setdefault(it["account"], []).append(it["value"])
    for it in items:
        vals = by_acct.get(it["account"], [])
        if it.get("value") is not None and len(vals) >= min_samples:
            base = statistics.median(vals)
            it["baseline"] = base
            it["outlier"] = round(it["value"] / base, 2) if base > 0 else None
    return items


# ---------- RSS / Atom (news, Substack) ----------
def parse_feed(xml_text, limit=5):
    root = ET.fromstring(xml_text)
    out = []
    kind = _local(root.tag)
    if kind == "rss":
        feed_title = _child_text(root.find("channel"), "title") if root.find("channel") is not None else ""
        for it in root.iter("item"):
            link = _child_text(it, "link")
            title = _child_text(it, "title")
            if title and link:
                out.append({"title": title, "url": link, "published": _iso(_child_text(it, "pubDate")), "feed": feed_title})
    elif kind == "feed":
        feed_title = _child_text(root, "title")
        for e in root:
            if _local(e.tag) != "entry":
                continue
            link = ""
            for ch in e:
                if _local(ch.tag) == "link" and ch.attrib.get("href"):
                    if ch.attrib.get("rel", "alternate") == "alternate":
                        link = ch.attrib["href"]
                        break
            title = _child_text(e, "title")
            pub = _child_text(e, "published") or _child_text(e, "updated")
            if title and link:
                out.append({"title": title, "url": link, "published": _iso(pub), "feed": feed_title})
    out.sort(key=lambda x: x["published"], reverse=True)
    return out[:limit]


def fetch_news(source):
    try:
        r = requests.get(source["url"], headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        entries = parse_feed(r.text, limit=5)
    except Exception as e:  # network, HTTP or XML problems
        return [], f"{type(e).__name__}: {e}"
    items = [
        {"platform": "news", "account": source["name"], "title": e["title"], "url": e["url"],
         "metric": None, "value": None, "baseline": None, "outlier": None, "published": e["published"]}
        for e in entries
    ]
    return items, None


# ---------- YouTube ----------
YT = "https://www.googleapis.com/youtube/v3"
NS = {"a": "http://www.w3.org/2005/Atom", "m": "http://search.yahoo.com/mrss/", "yt": "http://www.youtube.com/xml/schemas/2015"}


def parse_youtube_rss(xml_text, account):
    root = ET.fromstring(xml_text)
    out = []
    for e in root.findall("a:entry", NS):
        title = (e.findtext("a:title", default="", namespaces=NS) or "").strip()
        link_el = e.find("a:link", NS)
        url = link_el.attrib.get("href", "") if link_el is not None else ""
        pub = _iso(e.findtext("a:published", default="", namespaces=NS))
        stats = e.find("m:group/m:community/m:statistics", NS)
        views = float(stats.attrib["views"]) if stats is not None and "views" in stats.attrib else None
        if title and url:
            out.append({"platform": "youtube", "account": account, "title": title, "url": url,
                        "metric": "views", "value": views, "baseline": None, "outlier": None, "published": pub})
    return out


def _yt_channel_id(ref, key):
    m = re.search(r"(UC[\w-]{22})", ref)
    if m:
        return m.group(1), None
    if not key:
        return None, "Add the channel ID (starts with UC) or set YOUTUBE_API_KEY so @handles can be resolved."
    handle = ref.strip()
    if "youtube.com/" in handle:
        handle = handle.split("youtube.com/", 1)[1]
    handle = handle.strip("/").split("/")[0]
    if not handle.startswith("@"):
        handle = "@" + handle
    r = requests.get(f"{YT}/channels", params={"part": "id", "forHandle": handle, "key": key}, timeout=TIMEOUT)
    r.raise_for_status()
    its = r.json().get("items") or []
    if not its:
        return None, f"Channel {handle} not found."
    return its[0]["id"], None


def fetch_youtube(source, api_key=None, max_videos=30):
    try:
        cid, err = _yt_channel_id(source["url"], api_key)
        if err:
            return [], err
        if api_key:
            # uploads playlist = UU + channel id without UC. 1 quota unit per call (search.list costs 100).
            pl = "UU" + cid[2:]
            r = requests.get(f"{YT}/playlistItems", params={
                "part": "contentDetails", "playlistId": pl, "maxResults": max_videos, "key": api_key}, timeout=TIMEOUT)
            r.raise_for_status()
            ids = [i["contentDetails"]["videoId"] for i in r.json().get("items", [])]
            if not ids:
                return [], None
            r = requests.get(f"{YT}/videos", params={
                "part": "snippet,statistics", "id": ",".join(ids), "key": api_key}, timeout=TIMEOUT)
            r.raise_for_status()
            items = []
            for v in r.json().get("items", []):
                views = v.get("statistics", {}).get("viewCount")
                items.append({"platform": "youtube", "account": source["name"],
                              "title": v["snippet"]["title"], "url": f"https://www.youtube.com/watch?v={v['id']}",
                              "metric": "views", "value": float(views) if views is not None else None,
                              "baseline": None, "outlier": None, "published": _iso(v["snippet"].get("publishedAt", ""))})
        else:
            # No key: the public RSS feed has the latest 15 videos with view counts and costs no quota.
            r = requests.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            items = parse_youtube_rss(r.text, source["name"])
        return score_outliers(items), None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


# ---------- Instagram (via Apify) ----------
def fetch_instagram(sources, token, per_account=12):
    """One Apify run for all accounts. Returns (items_by_source_id, error)."""
    if not token:
        return {}, "APIFY_TOKEN is not set."
    handles = {}
    for s in sources:
        h = s["url"].strip().rstrip("/").split("/")[-1].lstrip("@")
        handles[h.lower()] = s
    try:
        r = requests.post(
            "https://api.apify.com/v2/acts/apify~instagram-scraper/run-sync-get-dataset-items",
            headers={"Authorization": f"Bearer {token}"},
            json={"directUrls": [f"https://www.instagram.com/{h}/" for h in handles],
                  "resultsType": "posts", "resultsLimit": per_account},
            timeout=300)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {}, f"{type(e).__name__}: {e}"
    out = {s["id"]: [] for s in sources}
    for p in data if isinstance(data, list) else []:
        owner = (p.get("ownerUsername") or "").lower()
        src = handles.get(owner)
        url = p.get("url")
        if not src or not url:
            continue
        likes = p.get("likesCount")
        likes = float(likes) if isinstance(likes, (int, float)) and likes >= 0 else None
        cap = (p.get("caption") or "").strip().replace("\n", " ")
        out[src["id"]].append({
            "platform": "instagram", "account": src["name"], "title": cap[:200] or "(no caption)", "url": url,
            "metric": "likes", "value": likes, "baseline": None, "outlier": None,
            "published": _iso(p.get("timestamp") or "")})
    for sid in out:
        score_outliers(out[sid])
    return out, None


# ---------- Facebook Pages (via Apify) ----------
FB_ACTOR_DEFAULT = "apify/facebook-posts-scraper"


def _first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v >= 0 else None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def parse_facebook_posts(data, source):
    """Reads the output of Apify Facebook post scrapers. Field names differ a little between
    scrapers, so each value is looked up under the common names."""
    out = []
    for p in data if isinstance(data, list) else []:
        if not isinstance(p, dict):
            continue
        url = _first(p, "url", "postUrl", "post_url", "permalink", "topLevelUrl")
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        text = str(_first(p, "text", "post_text", "message", "caption") or "").strip().replace("\n", " ")
        raw_t = _first(p, "time", "timestamp", "publish_date", "date", "publishedAt")
        if isinstance(raw_t, (int, float)) and not isinstance(raw_t, bool):
            ts = raw_t / 1000 if raw_t > 1e11 else raw_t
            published = datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
        else:
            published = _iso(str(raw_t or ""))
        out.append({"platform": "facebook", "account": source["name"], "title": text[:200] or "(no text)", "url": url,
                    "metric": "likes", "value": _num(_first(p, "likes", "likesCount", "reactionsCount", "reactions")),
                    "baseline": None, "outlier": None, "published": published})
    return score_outliers(out)


def fetch_facebook(source, token, actor=None, per_account=12):
    """One Apify run per Page, so results always belong to the right source."""
    if not token:
        return [], "APIFY_TOKEN is not set."
    actor = (actor or FB_ACTOR_DEFAULT).strip().replace("/", "~")
    url = source["url"].strip()
    if not url.startswith("http"):
        url = "https://www.facebook.com/" + url.lstrip("@/")
    try:
        r = requests.post(f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"startUrls": [{"url": url}], "resultsLimit": per_account}, timeout=300)
        if r.status_code >= 400:
            return [], f"Apify {r.status_code}: {r.text[:200]}"
        data = r.json()
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"
    items = parse_facebook_posts(data, source)
    if not items and isinstance(data, list) and data:
        first = data[0] if isinstance(data[0], dict) else {}
        why = first.get("error") or first.get("errorDescription") or "no posts could be read"
        return [], f"Apify returned data but {why}. Check the Page URL and APIFY_FACEBOOK_ACTOR."
    return items, None


# ---------- X (optional, pay-per-use) ----------
X_COST_USER = 0.010   # per user object read
X_COST_POST = 0.005   # per post read


def fetch_x(source, bearer, budget_left, per_account=5):
    """Returns (items, error, cost). Stops before exceeding budget_left (USD)."""
    est = X_COST_USER + per_account * X_COST_POST
    if not bearer:
        return [], "X_BEARER_TOKEN is not set.", 0.0
    if budget_left < est:
        return [], "Monthly X budget reached. Raise the limit in Settings to continue.", 0.0
    h = source["url"].strip().rstrip("/").split("/")[-1].lstrip("@")
    hdr = {"Authorization": f"Bearer {bearer}"}
    cost = 0.0
    try:
        r = requests.get(f"https://api.x.com/2/users/by/username/{h}", headers=hdr, timeout=TIMEOUT)
        r.raise_for_status()
        cost += X_COST_USER
        uid = r.json()["data"]["id"]
        r = requests.get(f"https://api.x.com/2/users/{uid}/tweets", headers=hdr, timeout=TIMEOUT, params={
            "max_results": max(5, per_account), "exclude": "retweets,replies",
            "tweet.fields": "public_metrics,created_at"})
        r.raise_for_status()
        posts = r.json().get("data", [])
        cost += X_COST_POST * len(posts)
        items = [{"platform": "x", "account": source["name"], "title": p["text"].replace("\n", " ")[:200],
                  "url": f"https://x.com/{h}/status/{p['id']}", "metric": "likes",
                  "value": float(p.get("public_metrics", {}).get("like_count", 0)),
                  "baseline": None, "outlier": None, "published": _iso(p.get("created_at", ""))} for p in posts]
        return score_outliers(items), None, cost
    except Exception as e:
        return [], f"{type(e).__name__}: {e}", cost
