import io
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("APP_PASSWORD", None)

import collectors as col  # noqa: E402
import generator as gen  # noqa: E402
import app as appmod  # noqa: E402

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
<item><title>Older</title><link>https://x.test/1</link><pubDate>Mon, 01 Jun 2026 10:00:00 GMT</pubDate></item>
<item><title>Newer</title><link>https://x.test/2</link><pubDate>Tue, 02 Jun 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>A</title>
<entry><title>Post</title><link rel="alternate" href="https://x.test/a"/><published>2026-06-03T10:00:00Z</published></entry></feed>"""

YT = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
<entry><title>Vid</title><link rel="alternate" href="https://www.youtube.com/watch?v=abc"/>
<published>2026-06-03T10:00:00+00:00</published>
<media:group><media:community><media:statistics views="12345"/></media:community></media:group></entry></feed>"""


class Parsers(unittest.TestCase):
    def test_rss_sorted_newest_first(self):
        r = col.parse_feed(RSS)
        self.assertEqual([x["title"] for x in r], ["Newer", "Older"])

    def test_atom(self):
        self.assertEqual(col.parse_feed(ATOM)[0]["url"], "https://x.test/a")

    def test_youtube_rss_views(self):
        v = col.parse_youtube_rss(YT, "Chan")[0]
        self.assertEqual(v["value"], 12345.0)

    def test_outlier_uses_median(self):
        items = [{"account": "a", "value": v} for v in (100, 100, 100, 100, 1000)]
        col.score_outliers(items)
        self.assertEqual(items[-1]["baseline"], 100)
        self.assertEqual(items[-1]["outlier"], 10.0)

    def test_outlier_needs_enough_samples(self):
        items = [{"account": "a", "value": v} for v in (1, 2, 3)]
        col.score_outliers(items)
        self.assertIsNone(items[0].get("outlier"))


class Generator(unittest.TestCase):
    def test_unverified_source_removed(self):
        b = {"trending": [{"topic": "t", "source_url": "https://fake.test"},
                          {"topic": "u", "source_url": "https://real.test"}]}
        gen.verify_sources(b, [{"url": "https://real.test"}])
        self.assertIsNone(b["trending"][0]["source_url"])
        self.assertTrue(b["trending"][1]["source_verified"])
        self.assertIn("removed", b["notes"])

    def test_json_extraction_with_fences(self):
        self.assertEqual(gen._extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_prompt_uses_profile_variables(self):
        s = appmod.db.get_settings()
        s["profile"].update(creator_name="Sam", niche="fitness")
        p = gen.system_prompt(s["profile"], s["content"])
        self.assertIn("Sam", p)
        self.assertIn("fitness", p)

    def test_pictures_label_follows_name(self):
        self.assertEqual(gen.pictures_label({"creator_name": "Sam", "pictures_label": ""}), "Sam Pictures")
        self.assertEqual(gen.pictures_label({"creator_name": "Sam", "pictures_label": "Face bank"}), "Face bank")


def wait_run(c):
    for _ in range(100):
        if not c.get("/api/run/status").get_json()["running"]:
            break
        time.sleep(0.1)
    return c.get("/api/run/status").get_json()


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = appmod.app.test_client()

    def test_1_defaults_x_off(self):
        s = self.c.get("/api/state").get_json()
        self.assertFalse(s["settings"]["platforms"]["x"])
        self.assertEqual(s["mode"], "demo")

    def test_2_profile_edit_updates_label(self):
        r = self.c.put("/api/settings", json={"profile": {"creator_name": "Alex", "brand_name": "Alex HQ"}})
        self.assertEqual(r.get_json()["pictures_label"], "Alex Pictures")
        r = self.c.put("/api/settings", json={"profile": {"pictures_label": "Face bank"}})
        self.assertEqual(r.get_json()["pictures_label"], "Face bank")

    def test_3_bad_time_rejected(self):
        self.assertEqual(self.c.put("/api/settings", json={"schedule": {"time": "25:99"}}).status_code, 400)

    def test_4_source_validation(self):
        self.assertEqual(self.c.post("/api/sources", json={"name": "", "type": "news", "url": ""}).status_code, 400)
        self.assertEqual(self.c.post("/api/sources", json={"name": "a", "type": "tiktok", "url": "b"}).status_code, 400)
        r = self.c.post("/api/sources", json={"name": "a", "type": "news", "url": "b"})
        self.assertEqual(r.status_code, 201)
        self.c.delete(f"/api/sources/{r.get_json()['id']}")

    def test_5_run_without_data_reports_error(self):
        self.c.post("/api/run")
        st = wait_run(self.c)
        self.assertIn("No data", st["error"] or "")

    def test_6_demo_pipeline_end_to_end(self):
        self.c.post("/api/demo/seed")
        self.c.post("/api/run")
        st = wait_run(self.c)
        self.assertIsNone(st["error"], st)
        b = self.c.get("/api/briefs/latest").get_json()
        self.assertEqual(b["mode"], "demo")
        self.assertEqual(len(b["data"]["reels"]), 2)

    def test_7_x_budget_guard(self):
        r = col.fetch_x({"url": "@someone"}, "tok", 0.01)
        self.assertIn("budget", r[1])
        self.assertEqual(r[2], 0.0)

    def test_8_picture_rules(self):
        bad = self.c.post("/api/pictures", data={"file": (io.BytesIO(b"x"), "a.exe")},
                          content_type="multipart/form-data")
        self.assertEqual(bad.status_code, 400)
        ok = self.c.post("/api/pictures", data={"file": (io.BytesIO(b"\x89PNG"), "a.png")},
                         content_type="multipart/form-data")
        self.assertEqual(ok.status_code, 201)
        self.c.delete(f"/api/pictures/{ok.get_json()['id']}")

    def test_9_demo_clear(self):
        self.c.post("/api/demo/clear")
        self.assertEqual(len(self.c.get("/api/items").get_json()), 0)


class NewFeatures(unittest.TestCase):
    """Hindi rule, brand hashtag, alternating category, hook bank, section regenerate."""

    @classmethod
    def setUpClass(cls):
        cls.c = appmod.app.test_client()
        cls.c.post("/api/demo/clear")
        cls.c.post("/api/demo/seed")
        cls.c.put("/api/settings", json={
            "profile": {"brand_hashtag": "#mybrand", "language_rule": gen.HINDI_RULE, "language": "Hindi + English"},
            "content": {"weekly_mix": "6 reels, 2 videos", "long_video_categories": "News deep-dive, Tutorial"}})

    def setUp(self):
        self._orig = gen.call_claude
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def tearDown(self):
        gen.call_claude = self._orig
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_settings_roundtrip_new_fields(self):
        r = self.c.put("/api/settings", json={
            "profile": {"brand_hashtag": "#mybrand", "language_rule": gen.HINDI_RULE, "language": "Hindi + English"},
            "content": {"weekly_mix": "6 reels, 2 videos", "long_video_categories": "News deep-dive, Tutorial"}})
        s = r.get_json()["settings"]
        self.assertEqual(s["profile"]["brand_hashtag"], "#mybrand")
        self.assertIn("never Arabic or Persian script", s["profile"]["language_rule"])
        self.assertEqual(s["content"]["weekly_mix"], "6 reels, 2 videos")

    def test_prompts_carry_language_rule_and_hashtag(self):
        s = appmod.db.get_settings()
        sysp = gen.system_prompt(s["profile"], s["content"])
        self.assertIn("never Arabic or Persian script", sysp)
        self.assertIn("Content mix per week", sysp)
        usr = gen.user_prompt(s["profile"], s["content"], [], [], "Tutorial")
        self.assertIn("#mybrand", usr)
        self.assertIn("Long video category for today: Tutorial", usr)

    def test_category_alternates(self):
        c = {"long_video_categories": "News deep-dive, Tutorial"}
        self.assertEqual(gen.pick_category(c, "News deep-dive"), "Tutorial")
        self.assertEqual(gen.pick_category(c, "Tutorial"), "News deep-dive")
        self.assertIn(gen.pick_category(c, None), ["News deep-dive", "Tutorial"])

    def test_ai_pipeline_saves_hooks_and_strips_fake_links(self):
        items = appmod.db.rows("SELECT url FROM items")
        good = items[0]["url"]
        fake = {"trending": [{"topic": "t", "why_it_matters": "w", "source_url": "https://invented.example/x"},
                             {"topic": "u", "why_it_matters": "w", "source_url": good}],
                "reels": [{"type": "news", "topic": "a", "hook": "h", "script": "s", "source_url": good}],
                "backup_ideas": ["1", "2", "3"], "notes": "",
                "hooks": [{"text": "Yeh sunke hairaan reh jaoge", "category": "Shock"}]}
        gen.call_claude = lambda system, user, key: fake
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        self.c.post("/api/run")
        st = wait_run(self.c)
        self.assertIsNone(st["error"], st)
        b = self.c.get("/api/briefs/latest").get_json()
        self.assertEqual(b["mode"], "ai")
        self.assertIsNone(b["data"]["trending"][0]["source_url"])
        self.assertTrue(b["data"]["trending"][1]["source_verified"])
        hooks = [h["text"] for h in self.c.get("/api/hooks").get_json()]
        self.assertIn("Yeh sunke hairaan reh jaoge", hooks)

    def test_regenerate_demo_and_validation(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.c.post("/api/run"); wait_run(self.c)
        b = self.c.get("/api/briefs/latest").get_json()
        r = self.c.post(f"/api/briefs/{b['id']}/regenerate", json={"section": "reel0"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("regenerated", r.get_json()["data"]["reels"][0]["hook"])
        self.assertEqual(self.c.post(f"/api/briefs/{b['id']}/regenerate", json={"section": "nope"}).status_code, 400)
        self.assertEqual(self.c.post(f"/api/briefs/{b['id']}/regenerate", json={"section": "reel9"}).status_code, 400)
        self.assertEqual(self.c.post("/api/briefs/99999/regenerate", json={"section": "reel0"}).status_code, 404)

    def test_regenerate_with_ai_replaces_only_that_section(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.c.post("/api/run"); wait_run(self.c)
        b = self.c.get("/api/briefs/latest").get_json()
        before_reel1 = b["data"]["reels"][1]["topic"]
        new = {"type": "news", "topic": "Fresh angle", "hook": "naya hook", "script": "naya script",
               "source_url": "https://invented.example/y"}
        gen.call_claude = lambda system, user, key: new
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        r = self.c.post(f"/api/briefs/{b['id']}/regenerate", json={"section": "reel0"}).get_json()["data"]
        self.assertEqual(r["reels"][0]["topic"], "Fresh angle")
        self.assertIsNone(r["reels"][0]["source_url"])      # invented link removed
        self.assertEqual(r["reels"][1]["topic"], before_reel1)  # other reel untouched

    def test_regenerate_api_failure_is_reported(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.c.post("/api/run"); wait_run(self.c)
        b = self.c.get("/api/briefs/latest").get_json()
        def boom(*a, **k):
            raise RuntimeError("Anthropic API 401: bad key")
        gen.call_claude = boom
        os.environ["ANTHROPIC_API_KEY"] = "bad"
        r = self.c.post(f"/api/briefs/{b['id']}/regenerate", json={"section": "reel0"})
        self.assertEqual(r.status_code, 502)
        self.assertIn("bad key", r.get_json()["error"])


class FakeResp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._d, self.text = status, data, text

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FacebookAndApify(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = appmod.app.test_client()
        cls._post, cls._get = col.requests.post, col.requests.get

    def tearDown(self):
        col.requests.post, col.requests.get = self._post, self._get
        appmod.requests.get = self._get
        os.environ.pop("APIFY_TOKEN", None)
        os.environ.pop("APIFY_FACEBOOK_ACTOR", None)

    SRC = {"id": 1, "name": "Some Page", "type": "facebook", "url": "https://www.facebook.com/SomePage"}

    def test_parses_official_style_fields(self):
        data = [{"url": "https://www.facebook.com/SomePage/posts/%d" % i, "text": "Post %d" % i,
                 "likes": 100 if i < 5 else 1000, "comments": 3, "shares": 1, "time": "2026-10-0%dT10:00:00.000Z" % (i + 1)}
                for i in range(6)]
        items = col.parse_facebook_posts(data, self.SRC)
        self.assertEqual(len(items), 6)
        self.assertEqual(items[0]["platform"], "facebook")
        self.assertEqual(items[5]["outlier"], 10.0)   # beats the page's own median

    def test_parses_other_scraper_field_names(self):
        data = [{"post_url": "https://www.facebook.com/p/1", "post_text": "Hello\nworld", "likes": "1,200",
                 "publish_date": "2026-10-01"}, {"permalink": "https://www.facebook.com/p/2", "message": "x",
                 "likesCount": 5, "timestamp": 1760000000000}, {"text": "no link so skipped"}, "junk"]
        items = col.parse_facebook_posts(data, self.SRC)
        self.assertEqual([i["url"] for i in items], ["https://www.facebook.com/p/1", "https://www.facebook.com/p/2"])
        self.assertEqual(items[0]["title"], "Hello world")
        self.assertEqual(items[0]["value"], 1200.0)
        self.assertTrue(items[1]["published"].startswith("2025-10"))

    def test_requires_token(self):
        self.assertIn("APIFY_TOKEN", col.fetch_facebook(self.SRC, "")[1])

    def test_request_shape_and_actor_name(self):
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            seen.update(url=url, headers=headers, json=json)
            return FakeResp(201, [{"url": "https://www.facebook.com/p/1", "text": "t", "likes": 1}])
        col.requests.post = fake_post
        items, err = col.fetch_facebook(self.SRC, "SECRET", "someone/other-actor")
        self.assertIsNone(err)
        self.assertEqual(len(items), 1)
        self.assertIn("someone~other-actor", seen["url"])
        self.assertNotIn("SECRET", seen["url"])                      # token only in the header
        self.assertEqual(seen["json"]["startUrls"][0]["url"], self.SRC["url"])
        col.fetch_facebook({**self.SRC, "url": "@SomePage"}, "T")
        self.assertEqual(seen["json"]["startUrls"][0]["url"], "https://www.facebook.com/SomePage")  # bare name -> link

    def test_http_error_and_unreadable_data(self):
        col.requests.post = lambda *a, **k: FakeResp(402, None, "Not enough credits")
        self.assertIn("402", col.fetch_facebook(self.SRC, "T")[1])
        col.requests.post = lambda *a, **k: FakeResp(200, [{"error": "page not found"}])
        self.assertIn("page not found", col.fetch_facebook(self.SRC, "T")[1])

    def test_pipeline_collects_facebook_and_respects_toggle(self):
        self.c.post("/api/demo/clear")
        sid = self.c.post("/api/sources", json={"name": "FB Page", "type": "facebook",
                                                "url": "https://www.facebook.com/x"}).get_json()["id"]
        os.environ["APIFY_TOKEN"] = "T"
        col.requests.post = lambda *a, **k: FakeResp(200, [{"url": "https://www.facebook.com/x/posts/1", "text": "hi", "likes": 3}])
        self.c.put("/api/settings", json={"platforms": {"facebook": True}})
        self.c.post("/api/run"); st = wait_run(self.c)
        self.assertIsNone(st["error"], st)
        row = [s for s in self.c.get("/api/sources").get_json() if s["id"] == sid][0]
        self.assertEqual((row["last_status"], row["last_count"]), ("ok", 1))
        self.c.put("/api/settings", json={"platforms": {"facebook": False}})
        self.c.post("/api/run"); wait_run(self.c)
        row = [s for s in self.c.get("/api/sources").get_json() if s["id"] == sid][0]
        self.assertEqual(row["last_status"], "off")
        self.c.put("/api/settings", json={"platforms": {"facebook": True}})
        self.c.delete(f"/api/sources/{sid}")

    def test_source_test_endpoint(self):
        sid = self.c.post("/api/sources", json={"name": "FB", "type": "facebook",
                                                "url": "https://www.facebook.com/x"}).get_json()["id"]
        r = self.c.post(f"/api/sources/{sid}/test").get_json()
        self.assertFalse(r["ok"])
        self.assertIn("APIFY_TOKEN", r["error"])
        os.environ["APIFY_TOKEN"] = "T"
        col.requests.post = lambda *a, **k: FakeResp(200, [{"url": "https://www.facebook.com/x/1", "text": "Hello", "likes": 1}])
        r = self.c.post(f"/api/sources/{sid}/test").get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["sample"], ["Hello"])
        self.assertEqual(self.c.post("/api/sources/99999/test").status_code, 404)
        xid = self.c.post("/api/sources", json={"name": "X", "type": "x", "url": "@a"}).get_json()["id"]
        self.assertIn("per read", self.c.post(f"/api/sources/{xid}/test").get_json()["error"])
        for i in (sid, xid):
            self.c.delete(f"/api/sources/{i}")

    def test_apify_token_check_never_leaks_token(self):
        self.assertFalse(self.c.post("/api/test/apify").get_json()["ok"])
        os.environ["APIFY_TOKEN"] = "MYSECRETTOKEN"
        appmod.requests.get = lambda *a, **k: FakeResp(200, {"data": {"username": "vineet"}})
        r = self.c.post("/api/test/apify")
        self.assertEqual(r.get_json(), {"ok": True, "username": "vineet"})
        self.assertNotIn("MYSECRETTOKEN", r.get_data(as_text=True))
        appmod.requests.get = lambda *a, **k: FakeResp(401, {})
        self.assertIn("rejected", self.c.post("/api/test/apify").get_json()["error"])


class Finishing(unittest.TestCase):
    """Trust limit, Urdu retry, Telegram, X, schedule, password."""

    @classmethod
    def setUpClass(cls):
        cls.c = appmod.app.test_client()

    def setUp(self):
        self._post, self._get = col.requests.post, col.requests.get
        self._claude = gen.call_claude
        self._pw = appmod.PASSWORD

    def tearDown(self):
        col.requests.post, col.requests.get = self._post, self._get
        appmod.requests.post, appmod.requests.get = self._post, self._get
        appmod.pipeline.requests.post = self._post
        gen.call_claude = self._claude
        appmod.PASSWORD = self._pw
        for k in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "X_BEARER_TOKEN"):
            os.environ.pop(k, None)

    # ---- trust limit for tiny accounts ----
    def test_tiny_numbers_do_not_rank_as_outliers(self):
        items = [{"url": "a", "value": 1400, "outlier": 47.0, "published": "2026-10-01", "account": "t"},
                 {"url": "b", "value": 90000, "outlier": 3.0, "published": "2026-10-01", "account": "t"}]
        out = gen.pick_items(items, min_value=5000)
        self.assertEqual([i["url"] for i in out], ["b", "a"])    # 3x on 90k beats 47x on 1.4k
        small = [i for i in out if i["url"] == "a"][0]
        self.assertIsNone(small["outlier"])
        self.assertEqual(gen.pick_items(items, 0)[0]["url"], "a")  # limit off: old behaviour

    def test_default_limit_catches_the_1_4k_views_case(self):
        self.assertEqual(appmod.db.DEFAULTS["content"]["min_engagement"], 5000)
        out = gen.pick_items([{"url": "a", "value": 1400, "outlier": 47.62, "published": "", "account": "t"}], 5000)
        self.assertIsNone(out[0]["outlier"])

    def test_items_endpoint_marks_unreliable(self):
        self.c.post("/api/demo/clear"); self.c.post("/api/demo/seed")
        self.c.put("/api/settings", json={"content": {"min_engagement": 50000}})
        rows = {r["title"]: r for r in self.c.get("/api/items").get_json()}
        self.assertFalse(rows["Stop doing this if you want reach in 2026"]["reliable"])   # 38.2K likes
        self.assertTrue(rows["I replaced my whole workflow with one prompt"]["reliable"])  # 482K views
        self.c.put("/api/settings", json={"content": {"min_engagement": 5000}})
        self.c.post("/api/demo/clear")

    # ---- Urdu script check ----
    def test_has_arabic_script(self):
        self.assertTrue(has_arabic_script_text := gen.has_arabic_script({"a": ["\u06a9\u0648\u0646"]}))
        self.assertFalse(gen.has_arabic_script({"a": ["\u092f\u093e\u0930 hello"]}))   # Devanagari is fine

    def test_urdu_triggers_one_retry_then_passes(self):
        calls = []
        urdu, hindi = {"x": "\u06a9\u0648\u0646"}, {"x": "\u092f\u093e\u0930"}
        gen.call_claude = lambda s, u, k: (calls.append(u) or (urdu if len(calls) == 1 else hindi))
        out = gen.ask_with_language_check({"language_rule": gen.HINDI_RULE}, "s", "u", "k")
        self.assertEqual(out, hindi)
        self.assertEqual(len(calls), 2)
        self.assertIn("Arabic-script letters", calls[1])

    def test_urdu_still_there_after_retry_adds_warning(self):
        gen.call_claude = lambda s, u, k: {"x": "\u06a9\u0648\u0646"}
        out = gen.ask_with_language_check({"language_rule": gen.HINDI_RULE}, "s", "u", "k")
        self.assertIn("still found", out["notes"])

    def test_visible_texts_never_contain_the_word_urdu(self):
        self.assertNotIn("urdu", gen.HINDI_RULE.lower())
        s = appmod.db.get_settings()
        s["profile"]["language_rule"] = gen.HINDI_RULE
        self.assertNotIn("urdu", gen.system_prompt(s["profile"], s["content"]).lower())
        self.assertNotIn("urdu", open(os.path.join(os.path.dirname(appmod.__file__), "static", "app.js")).read().lower())

    def test_old_saved_rule_is_upgraded_automatically(self):
        old = ("Write all spoken content (scripts, hooks, captions, titles) in Hindi (Devanagari script) mixed with common "
               "English tech words, the way people really talk. Do NOT use Urdu script or Urdu-style words.")
        appmod.db.save_settings({"profile": {"language_rule": old}})
        self.assertEqual(appmod.db.get_settings()["profile"]["language_rule"], gen.HINDI_RULE)

    def test_script_ban_logic(self):
        ban = gen.arabic_script_banned
        self.assertTrue(ban({"language": "Hindi + English", "language_rule": gen.HINDI_RULE}))
        self.assertTrue(ban({"language": "English", "language_rule": ""}))
        self.assertTrue(ban({"language": "Hinglish", "language_rule": "never in Urdu"}))
        self.assertFalse(ban({"language": "Urdu", "language_rule": "write in Urdu"}))   # an Urdu creator is left alone
        self.assertFalse(ban({"language": "", "language_rule": ""}))

    def test_urdu_creator_is_not_retried(self):
        calls = []
        gen.call_claude = lambda s, u, k: (calls.append(1) or {"x": "\u06a9\u0648\u0646"})
        gen.ask_with_language_check({"language": "Urdu", "language_rule": "write in Urdu"}, "s", "u", "k")
        self.assertEqual(len(calls), 1)

    def test_no_retry_when_urdu_not_banned(self):
        calls = []
        gen.call_claude = lambda s, u, k: (calls.append(1) or {"x": "\u06a9\u0648\u0646"})
        gen.ask_with_language_check({"language_rule": ""}, "s", "u", "k")
        self.assertEqual(len(calls), 1)

    # ---- Telegram ----
    def test_telegram_test_endpoint(self):
        self.assertIn("must both be set", self.c.post("/api/test/telegram").get_json()["error"])
        os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = "123:SECRETBOT", "42"
        sent = {}
        appmod.requests.post = lambda url, json=None, timeout=None: (sent.update(url=url, json=json) or FakeResp(200, {"ok": True}))
        r = self.c.post("/api/test/telegram")
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(sent["json"]["chat_id"], "42")
        appmod.requests.post = lambda *a, **k: FakeResp(401, {"ok": False, "description": "Unauthorized"})
        r = self.c.post("/api/test/telegram")
        self.assertIn("Unauthorized", r.get_json()["error"])
        self.assertNotIn("SECRETBOT", r.get_data(as_text=True))          # token never echoed

    def test_brief_summary_goes_to_telegram_when_enabled(self):
        os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = "123:T", "42"
        sent = []
        appmod.pipeline.requests.post = lambda url, json=None, timeout=None: (sent.append(json) or FakeResp(200, {}))
        brief = {"trending": [{"topic": "Topic A"}], "reels": [{"hook": "Hook A"}]}
        logs = []
        appmod.pipeline.notify(brief, "ai", {"notify": {"telegram": True}}, logs.append)
        self.assertEqual(len(sent), 1)
        self.assertIn("Topic A", sent[0]["text"]); self.assertIn("Hook A", sent[0]["text"])
        sent.clear()
        appmod.pipeline.notify(brief, "ai", {"notify": {"telegram": False}}, logs.append)
        self.assertEqual(sent, [])                                      # off means off

    def test_telegram_failure_log_has_no_token(self):
        os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = "123:SECRETBOT", "42"
        def boom(*a, **k):
            raise col.requests.ConnectionError("https://api.telegram.org/bot123:SECRETBOT/sendMessage failed")
        appmod.pipeline.requests.post = boom
        logs = []
        appmod.pipeline.notify({"trending": [], "reels": []}, "ai", {"notify": {"telegram": True}}, logs.append)
        self.assertNotIn("SECRETBOT", " ".join(logs))

    # ---- X (optional, paid) ----
    def test_x_fetch_parses_and_counts_cost(self):
        calls = []
        def fake_get(url, headers=None, timeout=None, params=None):
            calls.append(url)
            if "/users/by/username/" in url:
                return FakeResp(200, {"data": {"id": "7"}})
            return FakeResp(200, {"data": [{"id": str(i), "text": "tweet %d" % i, "created_at": "2026-10-0%dT10:00:00.000Z" % (i + 1),
                                           "public_metrics": {"like_count": 10 * (i + 1)}} for i in range(5)]})
        col.requests.get = fake_get
        items, err, cost = col.fetch_x({"name": "A", "url": "@someone"}, "tok", 10.0)
        self.assertIsNone(err)
        self.assertEqual(len(items), 5)
        self.assertAlmostEqual(cost, 0.01 + 5 * 0.005)
        self.assertEqual(items[0]["url"], "https://x.com/someone/status/0")
        self.assertEqual(len(calls), 2)

    def test_x_stops_when_budget_too_small_and_reports_errors(self):
        col.requests.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call X"))
        self.assertIn("budget", col.fetch_x({"name": "A", "url": "@a"}, "tok", 0.02)[1])
        col.requests.get = lambda *a, **k: FakeResp(429, {})
        items, err, cost = col.fetch_x({"name": "A", "url": "@a"}, "tok", 10.0)
        self.assertEqual(items, []); self.assertIn("HTTP 429", err); self.assertEqual(cost, 0.0)

    def test_x_monthly_spend_is_tracked_by_pipeline(self):
        self.c.post("/api/demo/clear")
        sid = self.c.post("/api/sources", json={"name": "XA", "type": "x", "url": "@a"}).get_json()["id"]
        os.environ["X_BEARER_TOKEN"] = "tok"
        self.c.put("/api/settings", json={"platforms": {"x": True}, "x_budget": 1})
        def fake_get(url, headers=None, timeout=None, params=None):
            if "/users/by/username/" in url:
                return FakeResp(200, {"data": {"id": "7"}})
            return FakeResp(200, {"data": [{"id": "1", "text": "t", "public_metrics": {"like_count": 5}}]})
        col.requests.get = fake_get
        spent_before = self.c.get("/api/state").get_json()["x_spent"]
        self.c.post("/api/run"); wait_run(self.c)
        spent_after = self.c.get("/api/state").get_json()["x_spent"]
        self.assertGreater(spent_after, spent_before)
        self.c.put("/api/settings", json={"platforms": {"x": False}, "x_budget": 10})
        self.c.delete(f"/api/sources/{sid}")

    # ---- daily schedule ----
    def test_schedule_job_follows_settings(self):
        self.c.put("/api/settings", json={"schedule": {"enabled": True, "time": "07:30", "timezone": ""}})
        job = appmod.scheduler.get_job("daily")
        self.assertIsNotNone(job)
        self.assertIn("hour='7'", str(job.trigger)); self.assertIn("minute='30'", str(job.trigger))
        self.c.put("/api/settings", json={"schedule": {"enabled": False}})
        self.assertIsNone(appmod.scheduler.get_job("daily"))
        self.c.put("/api/settings", json={"schedule": {"enabled": True, "time": "09:00", "timezone": "Not/AZone"}})
        self.assertIsNone(appmod.scheduler.get_job("daily"))            # bad zone: no crash, no job
        self.c.put("/api/settings", json={"schedule": {"enabled": True, "time": "09:00", "timezone": "Asia/Kolkata"}})
        self.assertIsNotNone(appmod.scheduler.get_job("daily"))
        self.c.put("/api/settings", json={"schedule": {"timezone": ""}})

    def test_scheduled_run_does_not_double_start(self):
        appmod.RUN["running"] = True
        self.assertFalse(appmod.start_run())
        appmod.RUN["running"] = False

    # ---- password screen ----
    def test_password_protects_api_and_uploads(self):
        appmod.PASSWORD = "pw123"
        anon = appmod.app.test_client()
        self.assertEqual(anon.get("/api/state").status_code, 401)
        self.assertEqual(anon.get("/uploads/x.png").status_code, 401)
        self.assertEqual(anon.post("/api/run").status_code, 401)
        self.assertEqual(anon.post("/api/login", json={"password": "nope"}).status_code, 401)
        self.assertTrue(anon.get("/api/session").get_json()["auth_required"])
        self.assertEqual(anon.post("/api/login", json={"password": "pw123"}).status_code, 200)
        self.assertEqual(anon.get("/api/state").status_code, 200)
        self.assertEqual(anon.get("/").status_code, 200)               # login page itself loads


if __name__ == "__main__":
    unittest.main(verbosity=2)
