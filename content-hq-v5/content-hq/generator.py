"""Turns collected items into a daily content brief, and rewrites single sections.
Real mode calls the Anthropic API. Demo mode (no key) builds a clearly labelled sample."""
import copy
import json
import os
import re
from datetime import date

import requests

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
MAX_ITEMS = 30          # only the best items go to the model: keeps cost low
MAX_TOKENS = 16000      # Devanagari text uses many tokens; scripts + outline need room

from db import HINDI_RULE  # noqa: E402  (one shared wording for the Hindi + English rule)


def pictures_label(profile):
    return profile.get("pictures_label") or f"{profile.get('creator_name', 'Creator')} Pictures"


def categories(content):
    cats = [c.strip() for c in str(content.get("long_video_categories", "")).split(",") if c.strip()]
    return cats or ["News deep-dive", "Tutorial"]


def pick_category(content, last_category=None):
    """Alternate the long-video category day by day."""
    cats = categories(content)
    if last_category in cats:
        return cats[(cats.index(last_category) + 1) % len(cats)]
    return cats[date.today().toordinal() % len(cats)]


# ---------- prompts ----------
def system_prompt(profile, content):
    p = profile
    who = p["creator_name"] + (f" ({p['handle']})" if p.get("handle") else "")
    lines = [
        f"You are a content strategist for {who}, brand: {p['brand_name']}.",
        f"Niche: {p['niche'] or 'general'}. Audience language: {p['language']}.",
    ]
    if p.get("language_rule"):
        lines.append(f"Language rule: {p['language_rule']}")
    lines.append(f"Voice and tone: {p['tone']}")
    if content.get("weekly_mix"):
        lines.append(f"Content mix per week: {content['weekly_mix']}")
    lines += [
        "",
        "Rules:",
        "1. Use ONLY the items provided by the user as evidence. Never invent news, numbers, quotes or links.",
        "2. Every trending topic and every piece must cite one source_url copied exactly from the provided items.",
        "3. Prefer items with a high outlier value (they beat their own account's median) and recent dates.",
        "4. If the items do not support a good topic, say so in notes instead of guessing.",
        "5. Reel scripts are 200-300 words, spoken, short sentences. Start with a shocking hook sentence, build like a story, "
        "end with a call to action. Include [PAUSE] and [SCREEN: show X] directions.",
        "6. Reply with one JSON object only. No markdown fences, no commentary.",
    ]
    if content.get("extra_instructions"):
        lines += ["", "Extra instructions from the creator:", content["extra_instructions"]]
    return "\n".join(lines)


def _hashtag_note(profile):
    tag = profile.get("brand_hashtag", "").strip()
    return f" Include the brand hashtag {tag}." if tag else ""


def reel_shape(profile, kind):
    strategy = "Shock + Curiosity" if kind == "news" else "Secret knowledge + FOMO"
    topic = "trending news from today's data" if kind == "news" else "a specific tool, technique or idea, different from the other reel"
    return {"type": kind, "topic": topic, "hook_strategy": strategy, "hook": "opening hook sentence",
            "thumbnail": "visual description: text on screen, expression, background colour, what is visible",
            "caption": "3-5 lines, ends with a CTA like 'Comment X and I will DM you'",
            "hashtags": ["#tag"] , "script": "COMPLETE word-for-word script, 200-300 words",
            "cta": "the closing call to action", "source_url": "exact url from the items"}


def long_video_shape(category):
    return {"title": "clickbait but honest title", "subtitle": "English version of the title", "category": category,
            "thumbnail": "face expression, text overlay, background, colours",
            "outline": [
                {"section": "INTRO", "time": "0:00-1:30", "dialogue": "actual opening script: hook in the first 5 seconds"},
                {"section": "SECTION 1", "time": "1:30-5:00", "dialogue": "topic + full explanation with examples"},
                {"section": "SECTION 2", "time": "5:00-10:00", "dialogue": "deeper dive + demo instructions"},
                {"section": "SECTION 3", "time": "10:00-15:00", "dialogue": "real-world use case + storytelling"},
                {"section": "OUTRO", "time": "15:00-end", "dialogue": "emotional close + subscribe CTA"}],
            "source_url": "exact url from the items"}


ARTICLE_SHAPE = {"title": "title", "platform": "Substack or LinkedIn", "reading_time": "~5 min",
                 "opening_hook": "first paragraph", "sections": ["3-4 bullet points"],
                 "closing_line": "last line", "source_url": "exact url from the items"}


def _trim(items):
    return [{k: it.get(k) for k in ("platform", "account", "title", "url", "metric", "value", "outlier", "published")}
            for it in items]


def user_prompt(profile, content, items, hooks, category):
    n = int(content.get("reels_per_day", 2))
    shape = {"trending": [{"topic": "", "why_it_matters": "one line", "source_url": "exact url from the items"}]}
    if n:
        shape["reels"] = [reel_shape(profile, "news" if i == 0 else "tool") for i in range(n)]
    if content.get("long_video"):
        shape["long_video"] = long_video_shape(category)
    if content.get("article"):
        shape["article"] = ARTICLE_SHAPE
    shape["backup_ideas"] = ["one-liner"]
    shape["hooks"] = [{"text": "reusable hook line from today's content", "category": "Curiosity|Shock|FOMO|Story|Contradiction"}]
    shape["notes"] = ""
    return (
        f"Today is {date.today().isoformat()}.\n"
        f"Create today's brief. Exactly 3 trending items, exactly 3 backup ideas, exactly 5 hooks.{_hashtag_note(profile)}\n"
        + (f"Long video category for today: {category}.\n" if content.get("long_video") else "")
        + f"\nITEMS (evidence):\n{json.dumps(_trim(items), ensure_ascii=False)}\n\n"
        f"HOOK BANK (styles that worked, for inspiration only):\n{json.dumps(hooks[:10], ensure_ascii=False)}\n\n"
        f"Return JSON with exactly this shape:\n{json.dumps(shape, ensure_ascii=False)}"
    )


def section_shape(profile, content, brief, section):
    """Returns (shape, label) for one regenerable section, or raises KeyError."""
    m = re.fullmatch(r"reel(\d+)", section)
    if m:
        i = int(m.group(1))
        reels = brief.get("reels") or []
        if i >= len(reels):
            raise KeyError(section)
        return reel_shape(profile, reels[i].get("type") or ("news" if i == 0 else "tool")), reels[i]
    if section == "long_video" and brief.get("long_video"):
        return long_video_shape(brief["long_video"].get("category") or categories(content)[0]), brief["long_video"]
    if section == "article" and brief.get("article"):
        return ARTICLE_SHAPE, brief["article"]
    raise KeyError(section)


def regen_prompt(profile, content, brief, section, items, hooks):
    shape, current = section_shape(profile, content, brief, section)
    ctx = {"trending": brief.get("trending", []),
           "other_topics": [r.get("topic") for r in brief.get("reels", [])],
           "long_video_title": (brief.get("long_video") or {}).get("title"),
           "article_title": (brief.get("article") or {}).get("title")}
    return (
        f"Today is {date.today().isoformat()}.\n"
        f"Rewrite ONLY the section \"{section}\" of today's brief with a FRESH, different angle or topic than the current version, "
        f"still supported by the items below.{_hashtag_note(profile)}\n\n"
        f"Context (other sections, keep consistent, do not repeat them):\n{json.dumps(ctx, ensure_ascii=False)}\n\n"
        f"CURRENT VERSION (to be replaced):\n{json.dumps(current, ensure_ascii=False)[:3000]}\n\n"
        f"ITEMS (evidence):\n{json.dumps(_trim(items), ensure_ascii=False)}\n\n"
        f"HOOK BANK:\n{json.dumps(hooks[:10], ensure_ascii=False)}\n\n"
        f"Return one JSON object with exactly this shape:\n{json.dumps(shape, ensure_ascii=False)}"
    )


# ---------- Claude ----------
def _extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def call_claude(system, user, api_key):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": MAX_TOKENS, "system": system,
              "messages": [{"role": "user", "content": user}]},
        timeout=300,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Anthropic API {r.status_code}: {r.text[:300]}")
    parts = [b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text"]
    return _extract_json("".join(parts))


def verify_sources(brief, items):
    """Anti-hallucination check: a source_url not in the collected items is removed and flagged."""
    known = {it["url"] for it in items}
    flagged = 0

    def fix(obj):
        nonlocal flagged
        if isinstance(obj, dict):
            if "source_url" in obj:
                u = obj["source_url"]
                obj["source_verified"] = bool(u) and u in known
                if u and u not in known:
                    obj["source_url"] = None
                    flagged += 1
            for v in obj.values():
                fix(v)
        elif isinstance(obj, list):
            for v in obj:
                fix(v)

    fix(brief)
    if flagged:
        brief["notes"] = (brief.get("notes") or "") + f" [{flagged} source link(s) removed because they were not in the collected data.]"
    return brief


def pick_items(items, min_value=0):
    """Best evidence first: outliers, then recent. Capped to MAX_ITEMS.
    A post with fewer than min_value views/likes is not trusted as an outlier: a tiny channel's
    1,400 views can look like 47x its own median, so its ratio is dropped before ranking."""
    def trusted(i):
        return i.get("value") is None or i["value"] >= min_value

    ranked = sorted(items, key=lambda i: ((i.get("outlier") or 0) if trusted(i) else 0, i.get("published") or ""),
                    reverse=True)[:MAX_ITEMS]
    out = []
    for i in ranked:
        j = dict(i)
        if not trusted(j):
            j["outlier"] = None
        out.append(j)
    return out


ARABIC_SCRIPT = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")


def has_arabic_script(obj):
    """True if any text in the brief uses Arabic-script letters (which includes Urdu)."""
    if isinstance(obj, str):
        return bool(ARABIC_SCRIPT.search(obj))
    if isinstance(obj, dict):
        return any(has_arabic_script(v) for v in obj.values())
    if isinstance(obj, list):
        return any(has_arabic_script(v) for v in obj)
    return False


RETRY_NOTE = ("\n\nIMPORTANT: your previous answer contained Arabic-script letters. Rewrite the whole JSON again and "
              "follow the language rule exactly. Use only Devanagari and English (Latin) letters.")

ARABIC_LANGS = ("urdu", "arabic", "persian", "farsi")
OTHER_LANGS = ("hindi", "hinglish", "devanagari", "english")


def arabic_script_banned(profile):
    """True when the creator writes in Hindi/Hinglish/English and has not asked for an Arabic-script language.
    A rule that says 'never ... Urdu' also counts as a ban."""
    text = f"{profile.get('language', '')} {profile.get('language_rule', '')}".lower()
    wants_arabic = any(w in text for w in ARABIC_LANGS)
    negated = re.search(r"\b(not|never|no|without|avoid|don't)\b[^.\n]{0,40}\b(urdu|arabic|persian)", text)
    return any(w in text for w in OTHER_LANGS) and (not wants_arabic or bool(negated))


def ask_with_language_check(profile, system, user, key):
    """Calls Claude. If Arabic-script text comes back for a Hindi/English creator, retries once, then warns."""
    out = call_claude(system, user, key)
    if arabic_script_banned(profile) and has_arabic_script(out):
        out = call_claude(system, user + RETRY_NOTE, key)
        if has_arabic_script(out):
            out["notes"] = (out.get("notes") or "") + " [Arabic-script text was still found after one retry. Check the language rule.]"
    return out


# ---------- demo mode ----------
def demo_brief(profile, content, items, category):
    name = profile["creator_name"]
    top = items[:3] or []
    trending = [{"topic": t["title"], "why_it_matters": f"Seen on {t['account']} ({t['platform']}).",
                 "source_url": t["url"]} for t in top]
    first = top[0] if top else {"title": "your topic", "url": None}
    reels = []
    for i in range(int(content.get("reels_per_day", 2))):
        t = top[i] if i < len(top) else first
        reels.append({
            "type": "news" if i == 0 else "tool", "topic": t["title"],
            "hook_strategy": "Shock + Curiosity" if i == 0 else "Secret knowledge + FOMO",
            "hook": f"DEMO: nobody is talking about this yet, and {name} found it first.",
            "script": "DEMO SCRIPT. Add your Anthropic API key to .env to get a real script written in your voice from today's data.",
            "cta": "Comment 'GUIDE' and I will send the full breakdown.",
            "thumbnail": "Close-up face, surprised expression, 3 words of text.",
            "caption": "DEMO caption.", "hashtags": ["#demo"], "source_url": t.get("url")})
    brief = {"trending": trending, "reels": reels,
             "backup_ideas": ["DEMO idea 1", "DEMO idea 2", "DEMO idea 3"],
             "notes": "DEMO MODE: this brief was not written by AI. Add ANTHROPIC_API_KEY to .env for real output."}
    if content.get("long_video"):
        brief["long_video"] = {"title": "DEMO long video title", "subtitle": "", "category": category,
                               "thumbnail": "Split screen: before / after.",
                               "outline": [{"section": "INTRO", "time": "0:00-1:30", "dialogue": "DEMO dialogue."}],
                               "source_url": first.get("url")}
    if content.get("article"):
        brief["article"] = {"title": "DEMO article", "platform": "Substack", "reading_time": "5 min",
                            "opening_hook": "DEMO hook.", "sections": ["Point 1", "Point 2", "Point 3"],
                            "closing_line": "DEMO closing.", "source_url": first.get("url")}
    return brief


# ---------- public API ----------
def generate(profile, content, items, hooks, last_category=None):
    """Returns (brief_dict, mode). mode is 'ai' or 'demo'."""
    chosen = pick_items(items, int(content.get("min_engagement") or 0))
    category = pick_category(content, last_category)
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return demo_brief(profile, content, chosen, category), "demo"
    brief = ask_with_language_check(profile, system_prompt(profile, content),
                                    user_prompt(profile, content, chosen, hooks, category), key)
    return verify_sources(brief, chosen), "ai"


def regenerate(profile, content, brief, section, items, hooks):
    """Returns the brief with ONE section rewritten. Raises KeyError for an unknown section."""
    section_shape(profile, content, brief, section)  # validates the key
    chosen = pick_items(items, int(content.get("min_engagement") or 0))
    out = copy.deepcopy(brief)
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        new = copy.deepcopy(section_shape(profile, content, out, section)[1])
        for f in ("hook", "title"):
            if f in new:
                new[f] = str(new[f]).split(" (regenerated")[0] + " (regenerated, demo)"
    else:
        new = ask_with_language_check(profile, system_prompt(profile, content),
                                      regen_prompt(profile, content, out, section, chosen, hooks), key)
        new = verify_sources({"x": new}, chosen)["x"]
    m = re.fullmatch(r"reel(\d+)", section)
    if m:
        out["reels"][int(m.group(1))] = new
    else:
        out[section] = new
    return out
