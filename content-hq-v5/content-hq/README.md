# Content HQ

A personal dashboard that collects what your competitors and favourite newsletters posted,
finds the posts that beat their own normal results, and writes your daily content brief
(reels, a long video, an article, backup ideas) in your voice.

Your name, brand, handle, niche, language, tone and the "Pictures" tab label are all editable
in **Settings > Profile**. Nothing about a person is hardcoded.

## Run it

```bash
cd content-hq
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                    # then add the keys you have
python app.py
```

Open http://localhost:8000. With no keys it runs in **demo mode** (sample briefs), so you can
explore first. Settings > Sample data loads fake sources and posts.

## Add your Claude API key (Windows)

1. In the `content-hq` folder, make a copy of `.env.example` named `.env` (`copy .env.example .env`).
2. Open `.env` in Notepad. Find the line `ANTHROPIC_API_KEY=` and paste your key right after the `=`,
   with no spaces and no quotes. Save the file.
3. Stop the app (`Ctrl + C` in the window) and start it again: `.\.venv\Scripts\python app.py`.
4. The yellow "Demo mode" banner disappears. Press **Generate brief now**.

Never paste the key into a chat, a screenshot or GitHub. `.env` is private to your computer.

## Hindi + English

Settings > Profile > **Use Hindi + English preset** fills in the language and a rule that tells the AI to
write Hindi in Devanagari with English tech words, using only Devanagari and English letters. Press **Save settings**.
You can write your own rule instead (for example Hinglish in Roman letters).

## Other features

- **Rewrite this section**: on a reel, the long video or the article, rewrites just that part with a fresh
  angle. The rest of the brief stays as it is.
- **Long video category** alternates every day (Strategy > Long video categories, default
  "News deep-dive, Tutorial").
- **Brand hashtag** (Settings > Profile) is added to every reel's hashtags.
- **Hook Bank fills itself**: each brief returns 5 reusable hooks that are saved automatically.
- **Weekly content mix** (Strategy) is optional text the AI sees, such as "6 reels and 2 long videos".

## Facebook Pages (via Apify)

1. Sources > Type **Facebook Page** > paste the public Page link, like `https://www.facebook.com/PageName`.
2. It uses the same `APIFY_TOKEN` as Instagram. No Facebook login is needed or asked for.
3. The default scraper is `apify/facebook-posts-scraper`. To use another Apify scraper, put its name in
   `APIFY_FACEBOOK_ACTOR` in `.env`. Different scrapers use slightly different field names; the app reads
   the common ones. If a scraper returns something it cannot read, the Source health shows the reason.
4. Check the scraper's page on Apify for its price before running it on many Pages.

Facebook scraping is unofficial like Instagram. Use public Pages and small volumes.

## Windows: one-click start

- **First time:** double-click `setup.bat` (installs everything, creates `.env`). Open `.env` in Notepad and add your keys.
- **Every day:** double-click `start.bat`. It starts the app and opens the dashboard. Keep that black window open;
  the 9:00 daily brief only runs while it is open.
- To start it automatically when Windows starts: press `Win + R`, type `shell:startup`, and put a shortcut to
  `start.bat` in that folder.

## Safety checks built in

- **Tiny accounts:** a post with fewer than 5,000 views or likes (Strategy > the "Ignore the vs. usual ratio" box)
  shows "(low volume)" instead of a big multiplier, and is not ranked as an outlier. 1,400 views can look like
  47x on a small channel. Set 0 to turn this off.
- **Script check:** for Hindi, Hinglish or English creators, if Arabic-script letters come back the app asks the
  AI once more. If they still appear, a warning is added to the brief.
- **Invented links:** any source link the AI returns that was not in the collected data is removed.
- **Keys:** never shown in the browser, and never put in error messages.

## Connection tests (Settings)

Test Apify token, Send Telegram test, and the Test button on each source. Use these once after adding keys.

## Test buttons (no AI cost)

- **Sources > Test** fetches one source right now and tells you how many posts came back, or the exact error.
  Nothing is saved and the AI is not called.
- **Settings > Test Apify token** checks the token is valid. The token itself is never shown.

## What each key unlocks

| Key in `.env` | What it enables | Needed? |
|---|---|---|
| `ANTHROPIC_API_KEY` | Real briefs written by AI from today's data | For real use |
| `YOUTUBE_API_KEY` | YouTube channels by @handle, up to 30 videos each | Optional (RSS works without it, 15 videos, needs the UC channel ID) |
| `APIFY_TOKEN` | Instagram accounts and Facebook Pages | Only if you use them |
| `X_BEARER_TOKEN` | X accounts (pay-per-use) | Only if you turn X on |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Daily summary on Telegram | Optional |
| `APP_PASSWORD` | Password screen | Set this if the app is reachable from the internet |

Keys stay on the server. The browser never sees them.

## How it works

1. **Collect** (`collectors.py`): RSS/Substack, YouTube, Instagram (Apify), X (optional).
2. **Score**: each post is compared with that account's **median** (not average). 2.5x is above
   average, 5x is an outlier.
3. **Write** (`generator.py`): the best 30 items go to the AI. It may only use those items as
   evidence. Any source link it returns that is not in the collected data is removed and flagged.
4. **Save and notify** (`pipeline.py`): the brief is stored in History; Telegram is optional.
5. **Schedule**: a daily job runs at the time set in Settings. The app has to be running then.

## X (Twitter) is optional

Off by default. When on, fetching stops once the monthly budget (Settings) is reached.
Spend is tracked per calendar month using the published per-read prices, as an estimate.

## Costs to expect

Roughly: a few cents per brief for AI writing if you only generate once a day, plus whatever your
scraping provider charges. Check current prices before relying on this:
platform.claude.com/docs (AI), apify.com/pricing, and X's developer pricing page.

## Files

```
app.py          web server, API, scheduler
collectors.py   data sources and outlier scoring
generator.py    prompt, AI call, demo mode, source verification
pipeline.py     collect -> write -> save -> notify
db.py           SQLite storage (data/hq.db)
static/         the dashboard (HTML, CSS, JS)
tests/          python -m unittest tests.test_core
```

## Known limits

- Scrapers for Instagram are unofficial and can break or be restricted by the platform.
  Use public data and small volumes.
- Built for one user. The password screen is basic; put it behind HTTPS if you deploy it.
- Use a real WSGI server (for example `gunicorn -w 1 app:app`) when deploying, and keep one
  worker so the daily job does not run twice. Call `start_scheduler()` on startup in that case.
