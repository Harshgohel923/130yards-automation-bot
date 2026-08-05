# 130 Yards Scorecard Bot

Fully automated Instagram scorecard pipeline for the
[@130yardsofficial](https://instagram.com/130yardsofficial) football page.
It watches scheduled matches, scrapes live data, renders branded HT/FT
scorecard images, and posts them to Instagram — with Telegram alerts at every
point where something can go wrong.

Originally built for the FIFA World Cup 2026, now covering club football:
Europe's top-5 leagues, the UEFA Champions League, and pre-season friendlies.

---

## How a match gets posted (the short version)

1. You add a fixture to `matches.json` (team names, scraper URL, kickoff time).
2. You run `python validate_matches.py` — it normalizes team/competition
   names, verifies every crest exists, and uploads them to Cloudinary before
   match day.
3. You commit and push. Production runs from `master` on GitHub Actions.
4. An external cron fires the **dispatcher** workflow every 15 minutes. When
   a match window opens, the dispatcher spawns a **match worker** run for it.
5. The worker polls the scraper for match phase and live data. At HT and FT it
   renders a scorecard, uploads it to Cloudinary, generates a caption with
   Gemini, and posts to Instagram.
6. If you sent a match photo to the Telegram bot, the card is rendered as a
   photo overlay; otherwise the classic template card is used.
7. Any failure anywhere sends you a descriptive Telegram alert.

---

## Architecture

### Where it runs

Production is **GitHub Actions**, not a server:

| Workflow | Trigger | Job |
|---|---|---|
| `.github/workflows/dispatcher.yml` | external cron + `schedule` every 15 min | Runs `.github/scripts/dispatcher.py`; spawns a `match_bot.yml` run per match whose window is open. Detects already-running workers by scanning run names (`match-<id>`) — no external state. |
| `.github/workflows/match_bot.yml` | `workflow_dispatch` (from dispatcher) | Runs `match_worker_runner.py <match_id>` for one match. Timeout 245 min (30 min dispatch lead + 210 min max match + buffer). |
| `.github/workflows/telegram_bot.yml` | `workflow_dispatch` (from dispatcher) | Runs the photo-intake Telegram bot under `.github/scripts/bot_watchdog.py` while matches are active. |

GitHub's own `schedule:` cron is unreliable, so an **external cron job** also
fires the dispatcher every 15 minutes on the dot via the `workflow_dispatch`
API, authenticated with a classic no-expiration PAT (`repo` scope):

```
curl -X POST -H "Authorization: token <PAT>" \
  https://api.github.com/repos/<owner>/<repo>/actions/workflows/dispatcher.yml/dispatches \
  -d '{"ref":"master"}'
```

**Consequences of this setup:**
- Code changes only take effect after **commit + push to master**.
- Env vars in production come from the repo's **Actions secrets**, not the
  local `.env`.
- Pushing resets GitHub's 60-day scheduled-workflow inactivity timer.

### Match worker lifecycle (`main.py`)

Each worker thread handles one match end-to-end:

- Polls the scraper (allfootballapp) once per minute for everything: match
  phase, live events, scores, and statistics. One fetch drives the whole
  iteration.
- **Phase comes from the scraper**, via `derive_status()`. The scraper's own
  `status` (`Fixture` / `Playing` / `Played`) plus the live minute map onto
  `NS / 1H / HT / 2H / ET / AP / FT`. A penalty scoreline (`ps_A`/`ps_B`)
  means a shootout regardless of minute.
- **Half-time detection** is the one place the minute isn't enough: it does
  **not** count stoppage time, sitting at 45 through first-half injury time
  *and* the interval alike (likewise 90 in the second half). So at minute 45,
  `_at_half_time()` looks for either of two signals, whichever the scraper
  publishes first:
  - a `half_time` entry in the event timeline (the explicit marker), or
  - the half-time scoreline `hts_A`/`hts_B`, filled in at the whistle.

  Both persist for the rest of the match, so they only count while the minute
  reads 45 — otherwise they would read as HT long after the interval.
- **Pre-kickoff crest check**: the moment the worker starts, it verifies both
  teams' crests exist on Cloudinary and alerts you if not — while there is
  still time to fix it.
- **Early posting**: posts the HT card at scraper minute 45 and the FT card
  at minute 90, without waiting for the official whistle. If the score
  changes before the whistle (stoppage-time goal), it posts a corrected card
  and alerts you to delete the outdated one (IG's delete API is broken —
  see *Known limitations*).
- Handles extra time and penalty shootouts (shootout goals are backed out of
  the FT score; 120' shootout events are excluded from scorer lines).
- Worker crashes are caught, alerted, and the worker is respawned on the next
  registry check if the match window is still open.
- **Scraper outage fallback**: if the scraper stays unreachable for 15 min and
  the match cannot still be running (kickoff + 115 min, or + 165 for a
  knockout), the FT card is posted from the last cached scrape with a Telegram
  warning, rather than letting the outage swallow the post entirely.

---

## The pipeline, file by file

### Data in

| File | Role |
|---|---|
| `matches.json` | The fixture registry — the **single source of truth** for team names, competition, and kickoff. Hand-edited, then validated. |
| `football_scraper_dom.py` | Scrapes allfootballapp match pages: match phase, teams, scores (HT/FT/pens), event timeline (goals, assists, cards, subs), statistics, formations. **The single source of truth** — status, live data and competition name all come from here. |
| `telegram_bot.py` | Photo intake: you send a match photo via Telegram, it lands in Cloudinary keyed by match id, and the pipeline switches to the photo-overlay card style. |

### Rendering

| File | Role |
|---|---|
| `scorecard.py` | Classic template card: stamps crests, score, scorers (+minutes and event symbols), stage text, stadium onto the HT/FT template. Coordinates live at the top of the file as `*_BOX` tuples (picked with `pick_coords.py`). |
| `overlay_scorebar.py` | Photo card: renders a dark scorebar panel over the bottom of a match photo — crests, big score, team-colored underlines (colors derived from the crest), scorer lines, competition logo on the panel border, penalties strip. |
| `caption.py` | Instagram caption via Gemini (2.5-flash → 2.0-flash → plain fallback), fed the score line, events, and the optional hand-written `records` from `matches.json`. The hashtag line comes from `hashtags.py`, not the model. |
| `hashtags.py` | The five hashtags, built in code: competition, home team, away team, then each side's nickname. Names resolve through `logo_fetch`'s canonical slugs, so any spelling yields the same tags. |
| `pick_coords.py` | Interactive helper to click out coordinate boxes on a template image. |

Both renderers take `home_name` / `away_name` / `competition` overrides —
**the validated `matches.json` names are what appears on the card and drives
crest lookup**; raw scraper names are used only to match events to the right
team.

### Assets: crests and logos (`logo_fetch.py`)

Team crests and competition logos are fetched from
[football-logos.cc](https://football-logos.cc) (3000×3000 PNGs) and stored in
Cloudinary under **deterministic public ids**:

```
assets/national/<name>      e.g. assets/national/united-states
assets/club/<name>          e.g. assets/club/bayern-munich
assets/competition/<key>    e.g. assets/competition/premier-league
```

The name-resolution stack (all in `logo_fetch.py`):

- `NICKNAMES` — every variant spelling → canonical slug
  (`Man Utd`, `Spurs`, `Internazionale`, `Korea Republic`, …).
  Accents are stripped automatically (`Türkiye`, `Beşiktaş` work as typed).
- `SITE_OVERRIDES` — teams the source site spells differently
  (`usa-national-team`, `dutch-national-team`, `tottenham`, …).
- `DISPLAY_NAMES` — canonical slug → official display form
  (`AC Milan`, `Côte d'Ivoire`); common football acronyms (FC, SK, AFC…)
  stay uppercase automatically.
- `COMPETITIONS` / `COMPETITION_ALIASES` / `COMPETITION_DISPLAY` — the same
  system for competitions, absorbing TheSportsDB ("English Premier League"),
  scraper ("Premier League"), and shorthand ("EPL", "UCL", "Carabao Cup")
  spellings.
- Clubs sharing a slug across countries can't overwrite each other: the
  non-priority country gets a suffixed id (`assets/club/liverpool-uruguay`).

CLI:

```bash
python logo_fetch.py "Leeds United"                    # fetch a club crest
python logo_fetch.py --national Argentina Egypt        # national teams
python logo_fetch.py --competition "Premier League"    # competition logo
python logo_fetch.py england/liverpool                 # explicit country/slug
python logo_fetch.py --local crest.png "FC Ryukyu"     # team not on the site:
                                                       # upload your own PNG
python logo_fetch.py --local logo.png --competition "Community Shield"
```

`--local` refuses to overwrite an existing crest unless you pass `--force`.

### Crest/logo lookup at render time (`config.py`)

- `get_crest_url(name)` — legacy WC table first, then the deterministic
  paths (HEAD-checked against Cloudinary, hits cached per run). Only
  successful lookups are cached, so a crest uploaded **mid-match** is picked
  up by the next render without a push. Network blips are distinguished from
  real 404s (retry, then optimistic URL) so an outage never false-alarms.
- `get_competition_logo_url(name)` — competition logo, with fallback chain:
  known competition → its logo; **friendly → the dedicated Friendly Match
  logo** (`assets/competition/friendly`); unknown/missing → the 130 Yards
  brand logo. The card never renders an empty logo slot.
- Missing crests/logos send a throttled Telegram alert with the exact fix
  command.

### Validation (`validate_matches.py`)

Run after editing `matches.json`, before pushing:

```bash
python validate_matches.py             # normalize + verify + upload
python validate_matches.py --check     # dry run, changes nothing
python validate_matches.py --no-upload # verify only, skip Cloudinary
```

Per entry it:

1. Normalizes `home_team` / `away_team` to the official display name
   (`Man Utd` → `Manchester United`) and writes the fix back (atomic
   write — a crash can't truncate the file).
2. Confirms the crest is actually downloadable from football-logos.cc, or
   already on Cloudinary from a `--local` upload.
3. Uploads the crest to Cloudinary so it's ready before kickoff.
4. Does the same for the optional `competition` field (`"EPL"` →
   `"Premier League"`, logo pre-fetched).

Failures exit non-zero with did-you-mean suggestions (drawn from the site's
real ~3,600-team index) and a printed fix guide covering all the manual
options.

### Posting and storage

| File | Role |
|---|---|
| `instagram.py` | IG Graph API: publish image posts, look up post permalinks, (broken) delete. |
| `cloudinary_upload.py` | Uploads rendered cards and archives match data JSON. |
| `cloudinary_utils.py` | Template fetch/cache (cache key includes the Cloudinary public id, so changing `CLOUDINARY_TEMPLATES` busts stale caches automatically), match-photo fetch. |
| `database.py` | SQLite: which events have been posted per match (prevents duplicates). |
| `telegram_notify.py` | Fire-and-forget `send_alert(text, key, cooldown)` — per-key throttling, never raises. |

### Auth (`setup_token.py`)

One-time re-auth tool for the Instagram token:

```bash
python setup_token.py <SHORT_LIVED_TOKEN_FROM_GRAPH_EXPLORER>
```

Exchanges it for a long-lived user token, walks to the **Page access token
(never expires)**, verifies permissions and IG access, and writes it to
`.env`. Needs `FB_APP_ID` / `FB_APP_SECRET` in `.env` (local only — never add
these to GitHub). Re-run only if the token is invalidated (password change,
permission revoke, Meta-side invalidation). After re-running, update the
`IG_ACCESS_TOKEN` repo secret (paste the raw value, no quotes).

---

## Telegram alerts

Every failure point alerts you with a plain-language description and its
consequence. Emoji convention: **❌** = a post did not go out / a match is
uncovered; **⚠️** = degraded but self-healing or cosmetic.

Covered: state-file load, registry parse errors, scraper outages (and the
stale-cache FT fallback that follows a long one),
pipeline failures (HT/FT and early posts), overlay-render fallback, worker
crashes, scraper failures mid-match, bad kickoff dates, Cloudinary/IG delete
failures (with a tappable permalink so you can delete manually), missing
crests (pre-kickoff *and* render-time), and unknown competitions. Alerts are
throttled per key so a stuck poll loop can't spam you.

---

## Configuration

### `.env` (local) / Actions secrets (production)

| Variable | Purpose |
|---|---|
| `IG_ACCESS_TOKEN` | Never-expiring Page access token |
| `IG_USER_ID` | Instagram business account id |
| `CLOUDINARY_CLOUD_NAME` / `CLOUDINARY_API_KEY` / `CLOUDINARY_API_SECRET` | Cloudinary |
| `GEMINI_API_KEY` | Caption generation |
| `TELEGRAM_BOT_TOKEN` | Photo bot + alerts |
| `TELEGRAM_ALLOWED_USER_IDS` | Who may talk to the bot (also default alert target) |
| `TELEGRAM_ALERT_CHAT_ID` | Optional explicit alert chat |
| `FB_APP_ID` / `FB_APP_SECRET` | **Local only** — used by `setup_token.py` |

### `config.py`

Cloudinary public ids for templates (`CLOUDINARY_TEMPLATES`), the legacy WC
crest/tournament tables, the brand logo, local event symbols, stadium name
aliases, and the `get_crest_url` / `get_competition_logo_url` lookups.

### `matches.json` entry

```json
{
  "match_id": "54457604",
  "scraper_url": "https://m.allfootballapp.com/match/Main/Liverpool-vs-Leeds-United/54457604",
  "kickoff_utc": "2026-08-02T23:00:00Z",
  "home_team": "Liverpool",
  "away_team": "Leeds United",
  "competition": "Club Friendly",
  "post_ht": true,
  "knockout_match": false,
  "records": ["Optional hand-written storylines used in the caption…"]
}
```

`home_team` / `away_team` / `competition` are what appears on the card —
any reasonable spelling survives `validate_matches.py`.

---

## Templates

The HT/FT template PNGs live in Cloudinary (`CLOUDINARY_TEMPLATES`) and are
downloaded on demand to `assets/templates/` (gitignored cache). To change the
design: upload new templates, update the two ids in `config.py` — every stale
cache invalidates automatically. Re-pick coordinate boxes with
`pick_coords.py` and update the `*_BOX` constants in `scorecard.py`
(`COMP_LOGO_BOX` is the competition-logo slot; while it is `(0, 0, 0, 0)` the
logo is skipped).

---

## Known limitations

- **Instagram post deletion is broken Meta-side** (HTTP 400 "Fatal",
  subcode 2207085, across API versions and token types). When an outdated
  early post must go, the bot posts the corrected card and sends you the
  outdated post's permalink to delete manually.
- The scraper depends on allfootballapp's mobile DOM; a redesign there would
  need `football_scraper_dom.py` updated (you'd get scraper-failure alerts).
  It is now the only live data source, so an outage stalls tracking — the
  stale-cache FT fallback exists to keep a long one from costing you the post.
- Team A in the scraper is assumed to be the home team (the same assumption
  the scraper URL and scores already rely on).

---

## Local development

```bash
python -m venv venv && venv/bin/pip install -r requirements.txt
# create .env with the variables listed above
venv/bin/python validate_matches.py --check
venv/bin/python main.py          # run the full bot loop locally
```

Useful one-offs:

```bash
# render a card from an archived scrape without posting
venv/bin/python - <<'PY'
import json
from scorecard import generate_scorecard
data = json.load(open('data/<match>.json'))
print(generate_scorecard(data, event_type='FT',
                         home_name='Liverpool', away_name='Leeds United',
                         competition='Club Friendly'))
PY
```

Runtime artefacts (`output/`, `data/`, `assets/logos/`, `assets/templates/`,
`bot.db`, `state.json`) are gitignored; Cloudinary is the source of truth for
all images.
