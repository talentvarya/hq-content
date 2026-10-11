"""Collect -> store -> generate -> save -> notify."""
import json
import os
from datetime import date, datetime, timedelta, timezone

import requests

import collectors as col
import db
import generator as gen


def _store(items, source_id):
    ts = db.now()
    with db.conn() as c:
        for it in items:
            c.execute(
                "INSERT INTO items(source_id,platform,account,title,url,metric,value,baseline,outlier,published,fetched_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(platform,url) DO UPDATE SET "
                "title=excluded.title, value=excluded.value, baseline=excluded.baseline, "
                "outlier=excluded.outlier, fetched_at=excluded.fetched_at",
                (source_id, it["platform"], it["account"], it["title"], it["url"], it["metric"], it["value"],
                 it["baseline"], it["outlier"], it["published"], ts))


def _health(source_id, items, error):
    db.run("UPDATE sources SET last_status=?, last_count=?, last_error=?, last_run=? WHERE id=?",
           ("error" if error else ("ok" if items else "idle"), len(items), error, db.now(), source_id))


def collect(settings, log):
    plats = settings["platforms"]
    sources = db.rows("SELECT * FROM sources WHERE active=1 AND is_demo=0")
    by_type = {}
    for s in sources:
        by_type.setdefault(s["type"], []).append(s)

    for t in ("news", "youtube", "instagram", "facebook", "x"):
        if t in by_type and not plats.get(t):
            for s in by_type[t]:
                db.run("UPDATE sources SET last_status='off', last_count=0, last_error=NULL WHERE id=?", (s["id"],))
            log(f"{t}: platform is off, skipped")

    if plats.get("news"):
        for s in by_type.get("news", []):
            items, err = col.fetch_news(s)
            _store(items, s["id"]); _health(s["id"], items, err)
            log(f"news / {s['name']}: {len(items)} items" + (f" (error: {err})" if err else ""))

    if plats.get("youtube"):
        key = os.environ.get("YOUTUBE_API_KEY", "").strip() or None
        for s in by_type.get("youtube", []):
            items, err = col.fetch_youtube(s, key)
            _store(items, s["id"]); _health(s["id"], items, err)
            log(f"youtube / {s['name']}: {len(items)} items" + (f" (error: {err})" if err else ""))

    if plats.get("instagram") and by_type.get("instagram"):
        out, err = col.fetch_instagram(by_type["instagram"], os.environ.get("APIFY_TOKEN", "").strip())
        for s in by_type["instagram"]:
            items = out.get(s["id"], [])
            _store(items, s["id"]); _health(s["id"], items, err)
        log(f"instagram: {sum(len(v) for v in out.values())} items" + (f" (error: {err})" if err else ""))

    if plats.get("facebook") and by_type.get("facebook"):
        token = os.environ.get("APIFY_TOKEN", "").strip()
        actor = os.environ.get("APIFY_FACEBOOK_ACTOR", "").strip() or None
        for s in by_type["facebook"]:
            items, err = col.fetch_facebook(s, token, actor)
            _store(items, s["id"]); _health(s["id"], items, err)
            log(f"facebook / {s['name']}: {len(items)} items" + (f" (error: {err})" if err else ""))

    if plats.get("x") and by_type.get("x"):
        month = date.today().strftime("%Y-%m")
        budget = float(settings.get("x_budget", 10))
        for s in by_type["x"]:
            left = budget - db.x_spent(month)
            items, err, cost = col.fetch_x(s, os.environ.get("X_BEARER_TOKEN", "").strip(), left)
            if cost:
                db.x_add(month, cost)
            _store(items, s["id"]); _health(s["id"], items, err)
            log(f"x / {s['name']}: {len(items)} items, ${cost:.3f}" + (f" ({err})" if err else ""))


def notify(brief, mode, settings, log):
    if not settings["notify"].get("telegram"):
        return
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (token and chat):
        log("telegram: token or chat id missing, skipped")
        return
    lines = [f"Your brief for {date.today().isoformat()}" + (" (demo)" if mode == "demo" else "")]
    for i, t in enumerate(brief.get("trending", [])[:3], 1):
        lines.append(f"{i}. {t.get('topic', '')}")
    for r in brief.get("reels", []):
        lines.append(f"Reel: {r.get('hook', '')}")
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": "\n".join(lines)[:3500]}, timeout=20)
        log("telegram: sent")
    except Exception as e:
        log(f"telegram: failed ({type(e).__name__})")


def recent_items(days=3, fallback=40):
    """Items collected recently; if none, the newest few so a rewrite still has evidence."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    items = db.rows("SELECT * FROM items WHERE fetched_at >= ?", (since,))
    if items or not fallback:
        return items
    return db.rows("SELECT * FROM items ORDER BY fetched_at DESC LIMIT ?", (fallback,))


def save_hooks(brief):
    """Reusable hooks from today's brief go into the Hook Bank (no duplicates)."""
    added = 0
    for h in brief.get("hooks") or []:
        text = (h.get("text") if isinstance(h, dict) else str(h) or "").strip()
        if not text or db.one("SELECT 1 FROM hooks WHERE text=?", (text,)):
            continue
        tag = (h.get("category") if isinstance(h, dict) else None) or None
        db.run("INSERT INTO hooks(text,tag,created) VALUES(?,?,?)", (text[:500], tag, db.now()))
        added += 1
    return added


def last_long_video_category():
    r = db.one("SELECT data FROM briefs ORDER BY id DESC LIMIT 1")
    if not r:
        return None
    try:
        return (json.loads(r["data"]).get("long_video") or {}).get("category")
    except ValueError:
        return None


def run(log=print):
    s = db.get_settings()
    log("collecting data")
    collect(s, log)
    items = recent_items(fallback=0)
    log(f"{len(items)} items available")
    if not items:
        raise RuntimeError("No data collected. Add sources in the Sources tab (or load demo data) and check the keys in .env.")
    hooks = [h["text"] for h in db.rows("SELECT text FROM hooks ORDER BY id DESC LIMIT 20")]
    log("writing brief")
    brief, mode = gen.generate(s["profile"], s["content"], items, hooks, last_long_video_category())
    n = save_hooks(brief)
    if n:
        log(f"{n} new hooks saved to the Hook Bank")
    bid = db.run("INSERT INTO briefs(day,mode,data,created) VALUES(?,?,?,?)",
                 (date.today().isoformat(), mode, json.dumps(brief, ensure_ascii=False), db.now()))
    notify(brief, mode, s, log)
    log("done")
    return bid
