"""Content HQ: a personal daily content-research dashboard."""
import json
import os
import re
import threading
import uuid
from datetime import date

import requests
from dotenv import load_dotenv

load_dotenv()

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, jsonify, request, send_from_directory, session
from werkzeug.utils import secure_filename

import db
import collectors
import generator as gen
import pipeline

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOADS = os.path.join(BASE, "data", "uploads")
os.makedirs(UPLOADS, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
db.init()

PASSWORD = os.environ.get("APP_PASSWORD", "")
SOURCE_TYPES = {"youtube", "news", "instagram", "facebook", "x"}
IMAGE_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
MAX_PICTURES = 20

# ---------- auth (optional) ----------
@app.before_request
def guard():
    if not PASSWORD or not request.path.startswith("/api/"):
        return
    if request.path in ("/api/login", "/api/session"):
        return
    if not session.get("ok"):
        abort(401)


@app.post("/api/login")
def login():
    if not PASSWORD or (request.json or {}).get("password") == PASSWORD:
        session["ok"] = True
        return jsonify(ok=True)
    return jsonify(error="Wrong password."), 401


@app.get("/api/session")
def session_state():
    return jsonify(auth_required=bool(PASSWORD), logged_in=(not PASSWORD) or bool(session.get("ok")))


# ---------- settings ----------
def clean_settings(patch):
    out = {}
    if "profile" in patch:
        p = {}
        for k in ("creator_name", "brand_name", "handle", "niche", "language", "tone", "pictures_label",
                  "brand_hashtag", "language_rule"):
            if k in patch["profile"]:
                p[k] = str(patch["profile"][k]).strip()[:500]
        if "creator_name" in p and not p["creator_name"]:
            p["creator_name"] = "Creator"
        if "brand_name" in p and not p["brand_name"]:
            p["brand_name"] = "My Brand"
        out["profile"] = p
    if "platforms" in patch:
        out["platforms"] = {k: bool(v) for k, v in patch["platforms"].items() if k in SOURCE_TYPES}
    if "x_budget" in patch:
        out["x_budget"] = max(0.0, min(500.0, float(patch["x_budget"])))
    if "content" in patch:
        c = patch["content"]
        o = {}
        if "reels_per_day" in c:
            o["reels_per_day"] = max(0, min(6, int(c["reels_per_day"])))
        for k in ("long_video", "article"):
            if k in c:
                o[k] = bool(c[k])
        if "extra_instructions" in c:
            o["extra_instructions"] = str(c["extra_instructions"])[:2000]
        if "weekly_mix" in c:
            o["weekly_mix"] = str(c["weekly_mix"])[:500]
        if "min_engagement" in c:
            o["min_engagement"] = max(0, min(10 ** 9, int(c["min_engagement"])))
        if "long_video_categories" in c:
            o["long_video_categories"] = str(c["long_video_categories"])[:300]
        out["content"] = o
    if "schedule" in patch:
        sc = patch["schedule"]
        o = {}
        if "enabled" in sc:
            o["enabled"] = bool(sc["enabled"])
        if "time" in sc:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(sc["time"])):
                raise ValueError("Time must look like 09:00")
            o["time"] = sc["time"]
        if "timezone" in sc:
            o["timezone"] = str(sc["timezone"]).strip()
        out["schedule"] = o
    if "notify" in patch:
        out["notify"] = {"telegram": bool(patch["notify"].get("telegram"))}
    return out


def key_status():
    env = os.environ.get
    return {"anthropic": bool(env("ANTHROPIC_API_KEY")), "youtube": bool(env("YOUTUBE_API_KEY")),
            "apify": bool(env("APIFY_TOKEN")), "x": bool(env("X_BEARER_TOKEN")),
            "telegram": bool(env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"))}


@app.get("/api/state")
def state():
    s = db.get_settings()
    month = date.today().strftime("%Y-%m")
    return jsonify(settings=s, pictures_label=gen.pictures_label(s["profile"]), keys=key_status(),
                   x_spent=round(db.x_spent(month), 3), model=gen.MODEL,
                   mode="ai" if os.environ.get("ANTHROPIC_API_KEY") else "demo")


@app.put("/api/settings")
def put_settings():
    try:
        new = db.save_settings(clean_settings(request.get_json(force=True) or {}))
    except (ValueError, TypeError) as e:
        return jsonify(error=str(e)), 400
    schedule_job()
    return jsonify(settings=new, pictures_label=gen.pictures_label(new["profile"]))


# ---------- sources ----------
def clean_source(d):
    name, url, typ = str(d.get("name", "")).strip(), str(d.get("url", "")).strip(), d.get("type")
    if typ not in SOURCE_TYPES:
        raise ValueError("Type must be youtube, news, instagram, facebook or x.")
    if not name or not url:
        raise ValueError("Name and URL/handle are required.")
    return name[:120], typ, url[:500], 1 if d.get("active", True) else 0


@app.get("/api/sources")
def list_sources():
    return jsonify(db.rows("SELECT * FROM sources ORDER BY type, name"))


@app.post("/api/sources")
def add_source():
    try:
        name, typ, url, active = clean_source(request.get_json(force=True))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    sid = db.run("INSERT INTO sources(name,type,url,active) VALUES(?,?,?,?)", (name, typ, url, active))
    return jsonify(db.one("SELECT * FROM sources WHERE id=?", (sid,))), 201


@app.put("/api/sources/<int:sid>")
def edit_source(sid):
    try:
        name, typ, url, active = clean_source(request.get_json(force=True))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    db.run("UPDATE sources SET name=?,type=?,url=?,active=? WHERE id=?", (name, typ, url, active, sid))
    return jsonify(db.one("SELECT * FROM sources WHERE id=?", (sid,)))


@app.delete("/api/sources/<int:sid>")
def del_source(sid):
    db.run("DELETE FROM sources WHERE id=?", (sid,))
    return jsonify(ok=True)


def run_source_test(src):
    """Fetch one source right now, without saving anything or calling the AI."""
    t, env = src["type"], os.environ.get
    if t == "news":
        items, err = collectors.fetch_news(src)
    elif t == "youtube":
        items, err = collectors.fetch_youtube(src, (env("YOUTUBE_API_KEY") or "").strip() or None)
    elif t == "instagram":
        out, err = collectors.fetch_instagram([src], (env("APIFY_TOKEN") or "").strip())
        items = out.get(src["id"], [])
    elif t == "facebook":
        items, err = collectors.fetch_facebook(src, (env("APIFY_TOKEN") or "").strip(),
                                               (env("APIFY_FACEBOOK_ACTOR") or "").strip() or None)
    else:
        return {"ok": False, "count": 0, "sample": [], "error": "X is billed per read, so it is not tested from here. Turn it on in Settings and generate a brief."}
    return {"ok": not err and bool(items), "count": len(items), "sample": [i["title"][:90] for i in items[:3]],
            "error": err or (None if items else "The source worked but returned no posts.")}


@app.post("/api/sources/<int:sid>/test")
def source_test(sid):
    src = db.one("SELECT * FROM sources WHERE id=?", (sid,))
    if not src:
        return jsonify(error="Source not found"), 404
    return jsonify(run_source_test(src))


@app.post("/api/test/apify")
def test_apify():
    """Checks that the Apify token is valid. Never returns the token."""
    token = (os.environ.get("APIFY_TOKEN") or "").strip()
    if not token:
        return jsonify(ok=False, error="APIFY_TOKEN is not set in .env.")
    try:
        r = requests.get("https://api.apify.com/v2/users/me", headers={"Authorization": f"Bearer {token}"}, timeout=20)
    except requests.RequestException as e:
        return jsonify(ok=False, error=f"Could not reach Apify: {type(e).__name__}")
    if r.status_code in (401, 403):
        return jsonify(ok=False, error="Apify rejected the token. Create a new one in Apify Console.")
    if r.status_code >= 400:
        return jsonify(ok=False, error=f"Apify error {r.status_code}.")
    data = (r.json() or {}).get("data", {})
    return jsonify(ok=True, username=data.get("username"))


@app.post("/api/test/telegram")
def test_telegram():
    """Sends one test message. Never returns the bot token."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        return jsonify(ok=False, error="TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set in .env.")
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": "Content HQ: test message. Telegram is connected."}, timeout=20)
    except requests.RequestException as e:
        return jsonify(ok=False, error=f"Could not reach Telegram ({type(e).__name__}).")
    if r.status_code == 200:
        return jsonify(ok=True)
    try:
        why = (r.json() or {}).get("description", "")
    except ValueError:
        why = ""
    return jsonify(ok=False, error=f"Telegram refused the message ({r.status_code}). {why}".strip())


# ---------- items / competitors ----------
@app.get("/api/items")
def items():
    plat = request.args.get("platform")
    sql, args = "SELECT * FROM items", ()
    if plat:
        sql, args = sql + " WHERE platform=?", (plat,)
    low = int(db.get_settings()["content"].get("min_engagement") or 0)
    rows = db.rows(sql + " ORDER BY COALESCE(outlier,0) DESC, published DESC LIMIT 300", args)
    for r in rows:  # ratios on tiny numbers are not trustworthy
        r["reliable"] = r["value"] is None or r["value"] >= low
    return jsonify(rows)


# ---------- hooks ----------
@app.get("/api/hooks")
def hooks():
    return jsonify(db.rows("SELECT * FROM hooks ORDER BY id DESC"))


@app.post("/api/hooks")
def add_hook():
    d = request.get_json(force=True) or {}
    text = str(d.get("text", "")).strip()
    if not text:
        return jsonify(error="Write the hook first."), 400
    hid = db.run("INSERT INTO hooks(text,source_url,tag,created) VALUES(?,?,?,?)",
                 (text[:500], str(d.get("source_url", ""))[:500] or None, str(d.get("tag", ""))[:60] or None, db.now()))
    return jsonify(db.one("SELECT * FROM hooks WHERE id=?", (hid,))), 201


@app.delete("/api/hooks/<int:hid>")
def del_hook(hid):
    db.run("DELETE FROM hooks WHERE id=?", (hid,))
    return jsonify(ok=True)


# ---------- briefs ----------
def brief_row(r):
    return {"id": r["id"], "day": r["day"], "mode": r["mode"], "created": r["created"], "data": json.loads(r["data"])}


@app.get("/api/briefs")
def briefs():
    return jsonify([{k: r[k] for k in ("id", "day", "mode", "created")}
                    for r in db.rows("SELECT id,day,mode,created FROM briefs ORDER BY id DESC LIMIT 100")])


@app.get("/api/briefs/latest")
def brief_latest():
    r = db.one("SELECT * FROM briefs ORDER BY id DESC LIMIT 1")
    return jsonify(brief_row(r) if r else None)


@app.get("/api/briefs/<int:bid>")
def brief_get(bid):
    r = db.one("SELECT * FROM briefs WHERE id=?", (bid,))
    return jsonify(brief_row(r)) if r else (jsonify(error="Not found"), 404)


@app.post("/api/briefs/<int:bid>/regenerate")
def brief_regenerate(bid):
    """Rewrite one section (reel0, reel1, long_video, article) without redoing the whole brief."""
    section = str((request.get_json(force=True) or {}).get("section", ""))
    r = db.one("SELECT * FROM briefs WHERE id=?", (bid,))
    if not r:
        return jsonify(error="Brief not found"), 404
    s = db.get_settings()
    hooks = [h["text"] for h in db.rows("SELECT text FROM hooks ORDER BY id DESC LIMIT 20")]
    try:
        new = gen.regenerate(s["profile"], s["content"], json.loads(r["data"]), section,
                             pipeline.recent_items(), hooks)
    except KeyError:
        return jsonify(error="That section can't be rewritten."), 400
    except Exception as e:  # API or JSON problems
        return jsonify(error=f"Rewrite failed: {e}"[:300]), 502
    db.run("UPDATE briefs SET data=? WHERE id=?", (json.dumps(new, ensure_ascii=False), bid))
    return jsonify(brief_row(db.one("SELECT * FROM briefs WHERE id=?", (bid,))))


# ---------- run pipeline ----------
RUN = {"running": False, "log": [], "error": None, "brief_id": None}
RUN_LOCK = threading.Lock()


def _worker():
    try:
        RUN["brief_id"] = pipeline.run(lambda m: RUN["log"].append(m))
    except Exception as e:
        RUN["error"] = str(e)
    finally:
        RUN["running"] = False


def start_run():
    with RUN_LOCK:
        if RUN["running"]:
            return False
        RUN.update(running=True, log=[], error=None, brief_id=None)
    threading.Thread(target=_worker, daemon=True).start()
    return True


@app.post("/api/run")
def run_now():
    return jsonify(started=start_run()), 202


@app.get("/api/run/status")
def run_status():
    return jsonify(RUN)


@app.get("/api/prompt-preview")
def prompt_preview():
    s = db.get_settings()
    return jsonify(system=gen.system_prompt(s["profile"], s["content"]))


# ---------- pictures ----------
@app.get("/api/pictures")
def pictures():
    return jsonify(db.rows("SELECT * FROM pictures ORDER BY id DESC"))


@app.post("/api/pictures")
def add_picture():
    if len(db.rows("SELECT id FROM pictures")) >= MAX_PICTURES:
        return jsonify(error=f"Limit is {MAX_PICTURES} pictures. Delete one first."), 400
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="Choose an image."), 400
    ext = f.filename.rsplit(".", 1)[-1].lower()
    if ext not in IMAGE_EXT:
        return jsonify(error="Use PNG, JPG, WEBP or GIF."), 400
    name = f"{uuid.uuid4().hex[:12]}_{secure_filename(f.filename)}"
    f.save(os.path.join(UPLOADS, name))
    pid = db.run("INSERT INTO pictures(filename,created) VALUES(?,?)", (name, db.now()))
    return jsonify(db.one("SELECT * FROM pictures WHERE id=?", (pid,))), 201


@app.delete("/api/pictures/<int:pid>")
def del_picture(pid):
    r = db.one("SELECT * FROM pictures WHERE id=?", (pid,))
    if r:
        try:
            os.remove(os.path.join(UPLOADS, r["filename"]))
        except OSError:
            pass
        db.run("DELETE FROM pictures WHERE id=?", (pid,))
    return jsonify(ok=True)


@app.get("/uploads/<path:name>")
def uploads(name):
    if PASSWORD and not session.get("ok"):
        abort(401)
    return send_from_directory(UPLOADS, name)


# ---------- demo data ----------
DEMO_SOURCES = [("Demo AI Newsletter", "news", "https://example.com/feed"),
                ("Demo Creator A", "youtube", "UC" + "A" * 22),
                ("Demo Creator B", "instagram", "demo_creator_b")]
DEMO_ITEMS = [
    ("news", "Demo AI Newsletter", "A new open model lands and developers are split", None, None, None),
    ("youtube", "Demo Creator A", "I replaced my whole workflow with one prompt", "views", 482000, 4.7),
    ("youtube", "Demo Creator A", "5 mistakes beginners make with AI tools", "views", 91000, 0.9),
    ("instagram", "Demo Creator B", "Stop doing this if you want reach in 2026", "likes", 38200, 6.1),
    ("instagram", "Demo Creator B", "My morning routine, automated", "likes", 6100, 1.0),
    ("news", "Demo AI Newsletter", "Why every team is rethinking its content calendar", None, None, None),
]


@app.post("/api/demo/seed")
def demo_seed():
    ts = db.now()
    ids = {}
    for name, typ, url in DEMO_SOURCES:
        ids[name] = db.run("INSERT INTO sources(name,type,url,active,is_demo) VALUES(?,?,?,1,1)", (name, typ, url))
    for i, (plat, acct, title, metric, val, outl) in enumerate(DEMO_ITEMS):
        db.run("INSERT OR IGNORE INTO items(source_id,platform,account,title,url,metric,value,baseline,outlier,published,"
               "fetched_at,is_demo) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
               (ids[acct], plat, acct, title, f"https://example.com/demo/{i}", metric, val,
                (val / outl) if val and outl else None, outl, ts, ts))
    db.run("INSERT INTO hooks(text,tag,created,is_demo) VALUES(?,?,?,1)",
           ("Nobody tells you this about [topic], but it changes everything.", "curiosity", ts))
    return jsonify(ok=True)


@app.post("/api/demo/clear")
def demo_clear():
    for t in ("items", "sources", "hooks"):
        db.run(f"DELETE FROM {t} WHERE is_demo=1")
    return jsonify(ok=True)


# ---------- scheduler ----------
scheduler = BackgroundScheduler()


def schedule_job():
    sc = db.get_settings()["schedule"]
    if scheduler.get_job("daily"):
        scheduler.remove_job("daily")
    if sc["enabled"]:
        h, m = sc["time"].split(":")
        kw = {"timezone": sc["timezone"]} if sc.get("timezone") else {}
        try:
            scheduler.add_job(start_run, "cron", hour=int(h), minute=int(m), id="daily", **kw)
        except Exception as e:  # bad timezone name
            print("schedule not set:", e)


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


def start_scheduler():
    if not scheduler.running:
        scheduler.start()
    schedule_job()


if __name__ == "__main__":
    start_scheduler()
    port = int(os.environ.get("PORT", "8000"))
    print(f"Content HQ running on http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
