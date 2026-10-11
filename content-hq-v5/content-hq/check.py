"""One-command health check for Content HQ.

Run it from this folder:   .\\.venv\\Scripts\\python check.py

It tests your keys and your sources and prints a short report. Keys are never printed.
Cost: one tiny Claude request (a fraction of a cent) and, if you have a Facebook Page in Sources,
a 3-post Apify run for it."""
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Hindi text must not crash the Windows console
except AttributeError:
    pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv()
import requests  # noqa: E402

import collectors as col  # noqa: E402
import db  # noqa: E402
import generator as gen  # noqa: E402

DEVA = re.compile(r"[\u0900-\u097F]")


def env(name):
    return (os.environ.get(name) or "").strip()


def api_message(resp, limit=140):
    """The provider's own error text (never contains our key)."""
    try:
        data = resp.json() or {}
        err = data.get("error", data)
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:
        msg = ""
    return (msg or resp.text or "")[:limit].replace("\n", " ")


def check_claude(add):
    key = env("ANTHROPIC_API_KEY")
    if not key:
        add("Claude key", False, "ANTHROPIC_API_KEY is not set in .env")
        return None
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=60,
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                          json={"model": gen.MODEL, "max_tokens": 150, "system": gen.HINDI_RULE,
                                "messages": [{"role": "user", "content": "Write two short spoken sentences for a reel about AI tools. Start with a hook."}]})
    except requests.RequestException as e:
        add("Claude key", False, f"could not reach Anthropic ({type(e).__name__})")
        return None
    if r.status_code != 200:
        add("Claude key", False, f"HTTP {r.status_code}: {api_message(r)}")
        return None
    add("Claude key", True, f"works (model {gen.MODEL})")
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
    deva, arab = len(DEVA.findall(text)), gen.has_arabic_script(text)
    add("Claude writes Hindi + English", deva > 0 and not arab,
        f"Devanagari letters: {deva}; Arabic/Urdu-script letters: {'YES' if arab else 'none'}")
    return text


def check_youtube(add):
    key = env("YOUTUBE_API_KEY")
    if not key:
        add("YouTube key", None, "not set (optional: RSS is used instead)")
        return
    try:
        r = requests.get("https://www.googleapis.com/youtube/v3/channels", timeout=20,
                         params={"part": "id", "forHandle": "@YouTube", "key": key})
    except requests.RequestException as e:
        add("YouTube key", False, f"could not reach Google ({type(e).__name__})")
        return
    add("YouTube key", r.status_code == 200, "works" if r.status_code == 200 else f"HTTP {r.status_code}: {api_message(r)}")


def check_apify(add):
    token = env("APIFY_TOKEN")
    if not token:
        add("Apify token", False, "APIFY_TOKEN is not set in .env")
        return False
    try:
        r = requests.get("https://api.apify.com/v2/users/me", headers={"Authorization": f"Bearer {token}"}, timeout=20)
    except requests.RequestException as e:
        add("Apify token", False, f"could not reach Apify ({type(e).__name__})")
        return False
    if r.status_code == 200:
        add("Apify token", True, "connected as " + str((r.json().get("data") or {}).get("username") or "your account"))
        return True
    add("Apify token", False, "token rejected, create a new one in Apify Console" if r.status_code in (401, 403) else f"HTTP {r.status_code}")
    return False


def read_sources():
    try:
        con = sqlite3.connect(db.DB_PATH)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute("SELECT * FROM sources WHERE is_demo=0 ORDER BY type, name")]
        con.close()
        return rows
    except sqlite3.Error:
        return []


def check_sources(add, apify_ok):
    sources = read_sources()
    if not sources:
        add("Sources", None, "none added yet (Sources tab)")
        return
    for s in sources:
        st = s.get("last_status") or "not run yet"
        detail = f"{s['type']}: {st}, {s.get('last_count') or 0} items" + (f" ({(s.get('last_error') or '')[:90]})" if s.get("last_error") else "")
        add(f"Source: {s['name'][:28]}", True if st == "ok" else (None if st in ("not run yet", "off") else False), detail)
    fb = [s for s in sources if s["type"] == "facebook" and s["active"]]
    if not fb:
        add("Facebook live test", None, "no Facebook Page in Sources yet")
    elif not apify_ok:
        add("Facebook live test", None, "skipped, fix the Apify token first")
    else:
        items, err = col.fetch_facebook(fb[0], env("APIFY_TOKEN"), env("APIFY_FACEBOOK_ACTOR") or None, per_account=3)
        if err:
            add("Facebook live test", False, err[:160])
        else:
            add("Facebook live test", bool(items), f"{len(items)} posts from '{fb[0]['name']}'. First: {items[0]['title'][:60] if items else ''}")


def check_telegram(add):
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not (token or chat):
        add("Telegram", None, "not set (optional)")
        return
    if not (token and chat):
        add("Telegram", False, "both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are needed")
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=20)
    except requests.RequestException as e:
        add("Telegram", False, f"could not reach Telegram ({type(e).__name__})")
        return
    ok = r.status_code == 200
    add("Telegram bot", ok, "bot token works (use 'Send Telegram test' in Settings to test the chat ID)" if ok else f"HTTP {r.status_code}: {api_message(r, 80)}")


def check_settings(add):
    s = db.get_settings()
    p = s["profile"]
    add("Profile saved", p.get("creator_name") not in ("", "Creator"), f"name = {p.get('creator_name')!r}, brand = {p.get('brand_name')!r}, niche = {p.get('niche') or '(empty)'!r}")
    add("Hindi + English rule saved", "Devanagari" in (p.get("language_rule") or ""), "language = " + (p.get("language") or "(empty)"))
    add("Platforms on", None, ", ".join(k for k, v in s["platforms"].items() if v) or "none")


def main():
    rows = []

    def add(label, ok, detail=""):
        rows.append((label, "OK" if ok is True else ("--" if ok is None else "FAIL"), detail))

    db.init()
    check_settings(add)
    sample = check_claude(add)
    check_youtube(add)
    apify_ok = check_apify(add)
    check_sources(add, apify_ok)
    check_telegram(add)

    w = max(len(r[0]) for r in rows)
    print("\n  CONTENT HQ CHECK\n  " + "-" * 60)
    for label, status, detail in rows:
        print(f"  [{status:>4}] {label.ljust(w)}  {detail}")
    if sample:
        print("\n  Claude's Hindi + English test reply:\n  " + sample.replace("\n", "\n  "))
    fails = [r for r in rows if r[1] == "FAIL"]
    print("\n  " + ("Everything that was checked is working." if not fails else f"{len(fails)} problem(s) found: fix the FAIL lines above.") +
          "\n  Keys are never printed. Send a screenshot of this screen.\n")
    return rows


if __name__ == "__main__":
    main()
