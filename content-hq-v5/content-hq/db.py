"""SQLite storage. One small file, no ORM."""
import json
import os
import sqlite3
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE, "data", "hq.db"))

HINDI_RULE = (
    "Write all spoken content (scripts, hooks, captions, titles) in Hindi (Devanagari script) mixed with common English tech words, the way people really talk. Use only Devanagari and English (Latin) letters, never Arabic or Persian script, and prefer everyday Hindi or English words over Persian or Arabic-derived ones.")
# Older versions saved a different wording. It is replaced automatically when settings are loaded.
_OLD_HINDI_RULE = "Write all spoken content (scripts, hooks, captions, titles) in Hindi (Devanagari script) mixed with common English tech words, the way people really talk. Do NOT use Urdu script or Urdu-style words."

DEFAULTS = {
    "profile": {
        "creator_name": "Creator",
        "brand_name": "My Brand",
        "handle": "",
        "niche": "",
        "language": "English",
        "tone": "Personal and excited, like a friend sharing a discovery. Plain words, never formal.",
        "pictures_label": "",  # empty -> "{creator_name} Pictures"
        "brand_hashtag": "",
        "language_rule": "",
    },
    "platforms": {"youtube": True, "news": True, "instagram": True, "facebook": True, "x": False},
    "x_budget": 10.0,
    "content": {
        "reels_per_day": 2,
        "long_video": True,
        "article": True,
        "extra_instructions": "",
        "weekly_mix": "",
        "long_video_categories": "News deep-dive, Tutorial",
        "min_engagement": 5000,
    },
    "schedule": {"enabled": True, "time": "09:00", "timezone": ""},
    "notify": {"telegram": False},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, type TEXT NOT NULL,
  url TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, is_demo INTEGER NOT NULL DEFAULT 0,
  last_status TEXT, last_count INTEGER DEFAULT 0, last_error TEXT, last_run TEXT
);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, platform TEXT NOT NULL,
  account TEXT, title TEXT, url TEXT NOT NULL, metric TEXT, value REAL, baseline REAL,
  outlier REAL, published TEXT, fetched_at TEXT NOT NULL, is_demo INTEGER NOT NULL DEFAULT 0,
  UNIQUE(platform, url)
);
CREATE TABLE IF NOT EXISTS hooks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, source_url TEXT, tag TEXT,
  created TEXT NOT NULL, is_demo INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS briefs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, mode TEXT NOT NULL,
  data TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pictures (
  id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS x_usage (month TEXT PRIMARY KEY, spent REAL NOT NULL DEFAULT 0);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with conn() as c:
        c.executescript(SCHEMA)


def rows(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


def run(sql, args=()):
    with conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def get_settings():
    saved = {}
    for r in rows("SELECT key, value FROM settings"):
        try:
            saved[r["key"]] = json.loads(r["value"])
        except ValueError:
            pass
    out = _merge(DEFAULTS, saved)
    if out["profile"].get("language_rule") == _OLD_HINDI_RULE:
        out["profile"]["language_rule"] = HINDI_RULE
    return out


def save_settings(patch):
    cur = get_settings()
    new = _merge(cur, patch)
    with conn() as c:
        for k in DEFAULTS:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(new[k])),
            )
    return new


def x_spent(month):
    r = one("SELECT spent FROM x_usage WHERE month=?", (month,))
    return r["spent"] if r else 0.0


def x_add(month, amount):
    run(
        "INSERT INTO x_usage(month,spent) VALUES(?,?) "
        "ON CONFLICT(month) DO UPDATE SET spent=spent+excluded.spent",
        (month, amount),
    )
