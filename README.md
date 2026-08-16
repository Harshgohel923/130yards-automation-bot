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
7. Set `post_ft_stats` on a fixture and its FT post becomes a two-slide
   carousel: the scorecard, then a **match statistics page**.
8. Give several fixtures the same `carousel_group` id and they post as **one
   carousel** instead of one post each — one scorecard per match, published by
   the dispatcher once the last of them has finished.
9. Any failure anywhere sends you a descriptive Telegram alert.

---

## Architecture

### Where it runs

Production is **GitHub Actions**, not a server:

| Workflow | Trigger | Job |
|---|---|---|
| `.github/workflows/dispatcher.yml` | external cron + `schedule` every 15 min | Runs `.github/scripts/dispatcher.py`; spawns a `match_bot.yml` run per match whose window is open. Detects already-running workers by scanning run names (`match-<id>`) — no external state. Also **publishes carousel groups** that are complete and **prunes finished fixtures** from `matches.json`, so it carries the same secrets a worker does, needs `contents: write`, and runs under a `concurrency` group to stay a single writer. |
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
- **Photo reminders** at 35', 80' and 110' — see *Reminders* below.
- **Early posting**: posts the HT card at scraper minute 45 and the FT card
  at minute 90, without waiting for the official whistle. If the score
  changes before the whistle (stoppage-time goal), it deletes the outdated
  post and puts up a corrected card. If the delete fails you get a 🙋 alert
  with the permalink instead — see *Deleting a superseded post*.
- Handles extra time and penalty shootouts (shootout goals are backed out of
  the FT score; 120' shootout events are excluded from scorer lines).
- Worker crashes are caught, alerted, and the worker is respawned on the next
  registry check if the match window is still open.
- **Scraper outage fallback**: if the scraper stays unreachable for 15 min and
  the match cannot still be running (kickoff + 115 min, or + 165 for a
  knockout), the FT card is posted from the last cached scrape with a Telegram
  warning, rather than letting the outage swallow the post entirely.
- **Grouped matches take a different exit**: a match with a `carousel_group`
  never posts on its own, so every early-posting path above is switched off for
  it. At the confirmed whistle it uploads its card and a manifest and exits —
  see *Carousel groups* below.

---

## Registry cleanup

The dispatcher prunes finished fixtures out of `matches.json` **once a day**,
at 10:00 German time (`PRUNE_HOUR_LOCAL`), and commits the trimmed file back to
master together with the day's match log — one housekeeping commit, not one per
fixture. Commits look like:

```
chore: remove 3 finished fixtures

- Juventus vs Palermo (2026-08-11T10:00:00Z)
- Napoli vs Aris Thessaloniki (2026-08-12T19:00:00Z)
- Marseille vs Atlético Madrid (2026-08-14T15:30:00Z)
```

A fixture is only dropped when **all** of these hold:

- **kickoff + 6 hours has passed** (`PRUNE_AFTER_HOURS`). A worker gives up at
  kickoff + 210 min, so by six hours nothing can still act on the entry.
- **no worker is running for it** — the same in-progress scan used for dispatch.
- **it isn't holding up a carousel.** `publish_group()` reads a group's
  membership straight from `matches.json`, so pruning a member early would
  quietly shrink the post, or drop the group entirely if every member went. A
  grouped fixture stays until its group has a `POSTED` marker.

A fixture whose `kickoff_utc` can't be parsed is never pruned — that's a human's
problem to look at, not something to silently delete.

**Why once a day.** Pruning per tick sounds like it would commit constantly but
didn't: a fixture only becomes prunable six hours after its own kickoff, so the
commits landed one per *distinct kickoff time* — nine on 15 August, five of them
removing a single fixture. Batching them into one daily pass is the only
difference; nothing about which fixtures qualify has changed. The hour is late
enough that an overnight MLS kickoff (02:30 local) is past its six hours by
08:30, and early enough to be clear of the European afternoon.

The dispatcher ticks four times inside that hour. The first does the work; the
rest find nothing left to prune, which is also what makes a failed push retry
fifteen minutes later at no cost. If the window is missed entirely — the
external cron was down — the next tick that sees a fixture more than
`PRUNE_CATCHUP_HOURS` (24) past its deadline tidies up immediately rather than
waiting for tomorrow. No state is stored between runs: both answers come from
the clock and the fixtures already in hand.

Note that a fixture which **failed** to post is pruned too. Six hours on, no
process will ever pick it up again, and the failure was alerted at the time.

The push rebases onto master first, so a fixture you add by hand while the
dispatcher is mid-run isn't clobbered — both changes survive. A failed push
costs nothing: the runner is thrown away and the next tick retries. `git log`
is the audit trail, so nothing is truly lost. No Telegram alert is sent for
pruning — it's routine housekeeping, not something anyone needs to act on.

---

## Carousel groups

Matches sharing a `carousel_group` id in `matches.json` go out as **one**
Instagram post — one scorecard per match — instead of cluttering the page with
a post each.

The hard part is that every match runs in its own GitHub Actions run: separate
filesystem, separate SQLite, no shared memory. So the group rendezvouses on
Cloudinary, which is already holding every rendered card:

```
carousel/<group>/<match_id>        the scorecard image
carousel/<group>/<match_id>.json   what that match was, for the caption
carousel/<group>/POSTED.json       written once the group has been posted
```

The split of duties (`carousel.py`):

- A **worker** finishing a grouped match uploads its card and manifest, then
  nudges the dispatcher and exits. It never posts.
- The **dispatcher** publishes: on each tick it groups `matches.json` by
  `carousel_group` and posts any group whose members have all landed.

The publisher is the dispatcher rather than the last worker to finish for two
reasons. A worker that crashes after its match would strand its group forever,
with no process left alive to notice; the dispatcher keeps ticking every 15
minutes regardless. And two matches whistling in the same minute would both
believe they were last — the dispatcher is a single serialised writer, which is
what the workflow's `concurrency` group guarantees.

The worker's nudge (`workflow_dispatch` on `dispatcher.yml`, which is why
`match_bot.yml` now needs `actions: write`) is pure latency optimisation: it
gets the group posted in about a minute instead of up to fifteen. If it fails,
nothing breaks.

**If a member never arrives**, the group still posts. Four hours after the last
kickoff in the group (`GROUP_TIMEOUT_HOURS`), the dispatcher posts what it has
and Telegram-alerts which matches were left out.

**Caption**: `generate_group_caption` asks Gemini to pick the one or two
stories worth telling and write them as narrative. It is explicitly forbidden
from stating any scoreline — the scorecards carry every result, and a model slip
would otherwise publish a wrong score that Instagram will not let you edit. It
returns the match ids it wrote about, and those matches' teams become the
hashtags (at most 5), so the tags always describe what the caption is about.

**Size**: Instagram caps a carousel at 10 slides, so a group is capped at 10
matches. `validate_matches.py` fails on a bigger group, on the day you edit the
fixture list rather than at full time.

---

## The pipeline, file by file

### Data in

| File | Role |
|---|---|
| `matches.json` | The fixture registry — the **single source of truth** for team names, competition, and kickoff. Hand-edited, then validated. Finished fixtures are pruned automatically — see *Registry cleanup*. |
| `football_scraper_dom.py` | Scrapes allfootballapp's **mobile** match pages: match phase, teams, scores (HT/FT/pens), event timeline (goals, assists, cards, subs), statistics, formations. **The single source of truth** — status, live data and competition name all come from here. Only the `m.` host carries the data blob; `www.` returns nothing. |
| `allfootball_desktop.py` | **Secondary, strictly additive.** Reads the desktop match page for two things mobile lacks: `minute_extra` (stoppage-time offset — mobile reports both a 90th-minute and a 90+3 booking as `90'`) and the `tendencies` momentum series, archived with the match data. Its `format_minute()` is what both renderers use to draw scorer minutes, so a stoppage-time goal reads `90+3'` instead of `90'`; events without an offset are returned unchanged, so it is safe to call unconditionally. Best-effort throughout: every failure path leaves the match data untouched, so it can never affect posting. Fetched only once a post is in prospect (HT/FT/ET/AP, or minute ≥ 43), to avoid doubling request volume against the same site. |
| `telegram_bot.py` | Photo intake: you send a match photo via Telegram, it lands in Cloudinary keyed by match id, and the pipeline switches to the photo-overlay card style. |

### Rendering

| File | Role |
|---|---|
| `scorecard.py` | Classic template card: stamps crests, score, scorers (+minutes and event symbols), stage text, stadium onto the HT/FT template. Coordinates live at the top of the file as `*_BOX` tuples (picked with `pick_coords.py`). |
| `overlay_scorebar.py` | Photo card: renders a dark scorebar panel over the bottom of a match photo — crests, big score, team-colored underlines (colors derived from the crest), scorer lines, competition logo on the panel border, penalties strip. |
| `stats_card.py` | The FT statistics page — slide two of a `post_ft_stats` post. Same template as the scorecard (`scorecard.load_template`, seeded by `match_id`, so both slides share a background) but blurred and veiled, since the page is dense with small marks. Header is the **momentum chart**, not the score: slide one already carries that. Body is a priority-ordered stat list as team-coloured split bars. |
| `carousel.py` | Carousel groups: the Cloudinary manifest store, the dispatcher nudge, and `publish_group`. See *Carousel groups* above. |
| `caption.py` | Instagram caption via Gemini (2.5-flash → 2.0-flash → plain fallback), fed the score line, events, and the optional hand-written `records` from `matches.json`. The hashtag line comes from `hashtags.py`, not the model. |
| `hashtags.py` | The five hashtags, built in code: competition, home team, away team, then each side's nickname. Names resolve through `logo_fetch`'s canonical slugs, so any spelling yields the same tags. |
| `pick_coords.py` | Interactive helper to click out coordinate boxes on a template image. |

Both renderers take `home_name` / `away_name` / `competition` overrides —
**the validated `matches.json` names are what appears on the card and drives
crest lookup**; raw scraper names are used only to match events to the right
team.

### The statistics page (`stats_card.py`)

Opt in per fixture with `"post_ft_stats": true`. **Full time only** — a stats
page at half time is half a story — and **never for a grouped match**, where
the carousel is one scorecard per match. If a match sets both, the group wins
and no stats page is rendered.

What it draws:

- **Momentum**, from the desktop feed's `tendencies` series: one bar per minute
  around a centre line, home above and away below in their own colours, with
  the interval marked and the minute ticks the feed itself publishes.
- **Goal markers** at the tip of the scoring team's bar, using the same symbol
  files as the scorer lines — white for a normal goal, green for a penalty,
  purple for an own goal. The tendencies feed only says *a goal happened here*,
  so the type is resolved against the mobile events list: nearest unclaimed goal
  by minute, then chronological order, then a plain goal. Goals a minute apart
  slide sideways rather than stacking, which would collide with the minute row.
- **Stat rows**, chosen by `STAT_PRIORITY` from whatever the match actually
  published — the scraper gives seven keys for a friendly and nearly thirty for
  a big game, so the row height adapts to how many are found. Winning value in
  white, losing in grey, and a proportional split bar in the two teams' colours.
  Colours come from the overlay card's crest sampling, with a luminance floor
  (so a black or navy team doesn't vanish into the background) and a minimum
  mutual distance (so two red teams don't draw identical bars).

Two degradations, neither of which costs you the post:

- **No momentum** — the desktop source is best-effort by design, and this is
  the first time it appears in a visible design. When it's missing the header
  falls back to a crest–VS–crest strip and the rest of the page is unaffected.
- **No statistics at all** — `generate_stats_card` returns `None`, the scorecard
  posts on its own, and you get a ⚠️ alert saying so.

### Assets: crests and logos (`logo_fetch.py`)

Team crests and competition logos are fetched from
[football-logos.cc](https://football-logos.cc) (3000×3000 PNGs) and stored in
Cloudinary under **deterministic public ids**.

#### Where everything lives in Cloudinary

```
assets/club/<slug>            crests          e.g. assets/club/ac-milan
assets/national/<slug>        crests          e.g. assets/national/argentina
assets/competition/<key>      badges          e.g. assets/competition/mls
match_photos/<id>_<HT|FT>     your photos     uploaded via the Telegram bot
scorecards/<random>           rendered cards  Cloudinary-named, posted to IG
carousel/<group>/…            staging         group rendezvous
logs/<match_id>.json          staging         per-match log, see Match log
```

The rule behind the crest paths: **a public id comes from the team's own
official name, never from how the source site spells it**
(`logo_public_id`, `logo_fetch.py`). Spurs live at
`assets/club/tottenham-hotspur` even though the site files them under
`tottenham`. The site's spellings are its business; yours shouldn't change
when it renames something.

Clubs sharing a slug across countries can't overwrite each other either: the
non-priority country gets a suffixed id (`assets/club/liverpool-uruguay`).

#### The normalization tables

Six hand-maintained dicts, all in `logo_fetch.py`. There is no naming data
anywhere else — not in `config.py`, not in Cloudinary, not in the index.
(Line numbers drift as the file is edited; the table name is the reliable
anchor.)

| Table | Line | Job |
|---|---|---|
| `NICKNAMES` | 105 | any *input* spelling → canonical slug (`usa` → `united-states`, `spurs` → `tottenham-hotspur`) |
| `DISPLAY_NAMES` | 212 | canonical slug → what prints on the card (`ac-milan` → `AC Milan`) |
| `SITE_OVERRIDES` | 239 | canonical slug → where the *site* keeps it, as `(country, site_slug, is_national)` |
| `COMPETITIONS` | 398 | canonical key → site location, as `(country, site_slug, variant)`; `None` = not on the site |
| `COMPETITION_ALIASES` | 427 | any spelling → canonical key (`epl`, `ucl`, `saudi-pl`) |
| `COMPETITION_DISPLAY` | 488 | canonical key → printed name |

The distinction that trips people up: **`NICKNAMES` is for spellings other
people use; `SITE_OVERRIDES` is for when your canonical name and the site's
filename disagree.** Tottenham need both — `spurs` → `tottenham-hotspur` in
`NICKNAMES`, and `tottenham-hotspur` → `england/tottenham` in
`SITE_OVERRIDES`. Accents are stripped automatically, so `Türkiye` and
`Beşiktaş` work as typed without an entry.

#### The site's team data (`data/logo_index.json`)

**3,622 teams** — 223 national, 3,399 clubs — built by scraping the site's
image sitemap. The format is `{slug: [countries]}`, which is how the
duplicate-slug handling above knows a slug is shared (18 of them are).

It is **gitignored and rebuilt automatically** when missing or older than
`INDEX_MAX_AGE_DAYS` (30), so every Actions run builds its own. It is a cache
of the source site: **never edit it** — your edits vanish on the next refresh.
It is also where the did-you-mean hints come from, so those slugs are always
real pages on the site.

#### What actually happens to a name

`resolve_team`:

```
"Man Utd"
  → _slugify        → "man-utd"                  lowercase, accents stripped
  → NICKNAMES       → "manchester-united"        canonical from here on
  → SITE_OVERRIDES? → absent, so the canonical doubles as the site slug
  → index lookup    → tries "manchester-united-national-team", then
                       "manchester-united" → found in ['england']
  → Cloudinary id     assets/club/manchester-united
  → DISPLAY_NAMES   → "Manchester United"        what prints on the card
```

A miss raises `LookupError` carrying the five closest real slugs.

#### Adding a fixture without typing any names

`matches.json` entries fill themselves in. A new fixture can be **just the
URL** — `validate_matches.py` reads `match_id`, `kickoff_utc`, both teams and
the competition off the match page, normalizes them, and writes them back. It
only fills blanks; anything you typed is left alone.

```json
[{ "scraper_url": "https://m.allfootballapp.com/match/Main/x/54329126" }]
```

```
Filled from the match page:
  entry #1: home_team = 'Nashville SC'
  entry #1: away_team = 'Inter Miami CF'
  entry #1: competition = 'MLS'
Normalized names:
  match 54329126: away_team 'Inter Miami CF' → 'Inter Miami'
```

So the normal workflow is: paste URLs, run validate, read the diff. Hand-typing
team names is the exception, not the routine.

#### When a name does need you

| Situation | What to do | Push needed? |
|---|---|---|
| The name resolves | Nothing | — |
| The site has the team under a different name | Add to `NICKNAMES`; add `SITE_OVERRIDES` too if the site's slug differs from the official name | **Yes** |
| The site doesn't have the team at all | `python logo_fetch.py --local crest.png "Team Name"` | No |

Only the middle row needs a code change. The third takes effect immediately —
`get_crest_url` caches hits but re-checks misses, so a crest uploaded
mid-match is picked up by the next render.

Competitions work the same way, with one extra convenience: an unrecognised
competition name is looked up under its slugified form, so
`--local --competition "Saudi PL"` is enough — no `COMPETITIONS` entry
required. See *Crest/logo lookup at render time*.

#### CLI

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
  logo** (`assets/competition/friendly`); **a competition with no entry at
  all → whatever has been uploaded under its slugified name**
  (`"Saudi PL"` → `assets/competition/saudi-pl`, exactly where
  `--local --competition` writes it); nothing found → the 130 Yards brand
  logo. The card never renders an empty logo slot.

  That third step is what makes a new competition work without a code change.
  `--local --competition` has always accepted any name, but the lookup used to
  give up as soon as `resolve_competition` returned `None`, so an uploaded
  badge was never found and the alert said a developer was needed. Both ends
  now agree on the same public id.
- Missing crests/logos send a throttled Telegram alert with the exact fix
  command — `--competition` when the source site carries it, `--local
  --competition` when it doesn't.

### Validation (`validate_matches.py`)

Run after editing `matches.json`, before pushing:

```bash
python validate_matches.py             # normalize + verify + upload
python validate_matches.py --check     # dry run, changes nothing
python validate_matches.py --no-upload # verify only, skip Cloudinary
```

**Either host works in `scraper_url`.** The desktop site lists fixtures the
mobile UI never surfaces, so a match is often far easier to find there — paste
that URL and the mobile one is computed for you:

```json
{ "scraper_url": "https://www.allfootballapp.com/match/54457613" }
```

becomes

```json
{
  "match_id": "54457613",
  "scraper_url": "https://m.allfootballapp.com/match/Main/liverpool-vs-leeds-united/54457613",
  "desktop_url": "https://www.allfootballapp.com/match/54457613",
  …
}
```

The conversion is reliable because the mobile slug is decoration: the server
resolves on the trailing id alone — verified with real, dummy and deliberately
wrong slugs, all of which return the same match. The slug is still built from
the teams so the URL reads properly when you check it, and it is written after
name normalization, so it carries `rayo-vallecano` rather than the scraper's
`vallecano`. An existing mobile URL is never rewritten, however its teams are
spelled. `desktop_url` is filled in either direction and is there for you, not
the code — the bot derives the desktop page from `match_id` at runtime.

**Fixtures are sorted by kick-off, earliest first,** on every run. Nothing
downstream cares about order — the dispatcher scans the whole list each tick —
but a file you hand-edit is easier to work with in the order matches happen.

**`kickoff_local` shows the kick-off in German time**, recomputed from
`kickoff_utc` on every run:

```json
"kickoff_utc":   "2026-08-16T00:30:00Z",
"kickoff_local": "Sun 16 Aug 2026, 02:30 CEST",
```

The zone abbreviation is the useful part: **CEST means summer time is on, CET
means it is off**, so the field answers the daylight-saving question rather
than leaving it to be worked out. The conversion uses the IANA `Europe/Berlin`
zone (`LOCAL_TZ` in `config.py`, shared with the match log), so the changeover
weekends are handled exactly — a 01:30 UTC kickoff on 29 March 2026 reads
03:30 CEST, because 02:00–03:00 local does not exist that night.

It is **derived, never a source of truth**. `kickoff_utc` is the only kickoff
the bot reads; editing `kickoff_local` moves nothing. A hand-edited value is
reported and then overwritten:

```
Kick-off in German time:
  match 54493161: Sat 15 Aug 2026, 18:00 CEST → Sat 15 Aug 2026, 21:30 CEST
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
5. An unknown competition is a failure **only if no badge has been uploaded
   for it**. Upload one with `--local --competition "Saudi PL"` and the name
   is accepted as typed from then on — an uploaded badge is proof the name was
   deliberate rather than a typo. Failures print did-you-mean suggestions and
   the exact upload command.
6. Type-checks `post_ft_stats` and `carousel_group`, and **fails when any
   carousel group holds more than 10 matches** — Instagram's carousel cap, and
   a group is one slide per match. It also prints each group's size, warns when
   a group has only one match (usually a typo in the id), and notes when a
   grouped match sets `post_ft_stats`, which has no effect.

Failures exit non-zero with did-you-mean suggestions (drawn from the site's
real ~3,600-team index) and a printed fix guide covering all the manual
options.

### Posting and storage

| File | Role |
|---|---|
| `instagram.py` | IG Graph API: publish image posts, look up post permalinks, delete. |
| `cloudinary_upload.py` | Uploads rendered cards and archives match data JSON. |
| `cloudinary_utils.py` | Template fetch/cache (cache key includes the Cloudinary public id, so changing `CLOUDINARY_TEMPLATES` busts stale caches automatically), match-photo fetch. |
| `database.py` | SQLite: which events have been posted per match (prevents duplicates). |
| `telegram_notify.py` | Fire-and-forget `send_alert(text, key, cooldown)` — per-key throttling, never raises. Also `send_music_reminder()`, sent after every publish. |
| `matchlog.py` | Per-match event log. Workers append milestones and errors to Cloudinary; see *Match log*. |
| `logbook.py` | Renders those logs into `logs/<day>.md` for the dispatcher to commit. |

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

## Match log

**[`logs/`](logs/) is the one place to find out what happened to a match.**
One Markdown file per day, newest listed first in
[`logs/README.md`](logs/README.md).

It exists because nothing else survives a match. Every worker runs in its own
Actions runner and `bot.db`, `state.json` and `data/` are all gitignored, so
when the run ends the only record was its stdout — buried in the Actions UI
and deleted after 90 days. Telegram tells you when something breaks; this tells
you what happened.

```
## Manchester United vs AC Milan

**16:45** · Club Friendly · carousel group `…` · 2 worker runs · id `54457598`

16:16:04    worker started             first run
16:16:22    crests checked             both found
16:47:31    status 1H                  minute 3 · 0-0
17:02:11 ❌ worker crashed             AttributeError: 'list' object has no…
17:14:02    worker started             run 2
17:15:40 ⚠️ live feed lost             no data from the scraper
```

**All times are German local** (CET/CEST, DST handled), and a match is filed
under the German calendar day it kicks off on — so a 00:30 UTC MLS match
appears under the following day, where you would look for it.

How it is written, and why that way:

- A **worker** appends events to Cloudinary at `logs/<match_id>.json`. It is
  the sole writer for its own match, so there is no contention — the same
  reason the carousel rendezvouses there.
- The **dispatcher** renders every staged log into `logs/<day>.md` and commits
  it in the same push as the pruned `matches.json`. It is a single serialised
  writer that already pushes to master, so it is the only process that safely
  can.
- That render happens **once a day**, in the same 10:00 window as the prune. A
  live match rewrites its log every minute, so committing per tick would mean a
  commit every fifteen — far more churn than the fixture pruning that window
  was introduced to fix. Nothing is lost by waiting: the events reach Cloudinary
  as they happen, and Telegram still alerts you in real time. To read a log
  before the daily pass, render it locally:

  ```bash
  python logbook.py            # write logs/<day>.md for every staged log
  python logbook.py --print    # print them to the terminal instead
  ```
- **A respawned worker continues its log rather than replacing it.** The record
  of why the first one died is the most valuable thing in the file; overwriting
  it on restart would delete the evidence exactly when it started to matter.
- **Every Telegram alert is logged automatically** — `main.py` wraps
  `send_alert` rather than editing twenty call sites, so an alert added later
  is recorded without anyone remembering to. What you were told and what was
  recorded cannot drift apart.
- Writes are debounced to once a minute, but warnings, errors, posts and
  handovers flush immediately, and a **SIGTERM flushes everything** — a job
  killed at its timeout is precisely the run whose log you want.
- Logging never raises. Every function swallows its own exceptions: a log that
  fails to write is worth strictly less than a post that goes out.

Staged logs older than `RETAIN_DAYS` (14) are dropped from Cloudinary once
rendered. The Markdown in git is the permanent copy.

---

## Telegram alerts

Every failure point alerts you with a plain-language description and its
consequence. Alerts are written for whoever is watching the phone, not for
whoever wrote the code: they name the match by its teams ("Liverpool vs Leeds
United", never a match id), say in one line what it means for the page, and put
any raw error last under *Technical detail*.

Emoji convention:

| | Meaning | Do you need to do anything? |
|---|---|---|
| **❌** | A post did not go out, or a match is uncovered | Usually no — most retry automatically. Read the second line. |
| **⚠️** | Degraded but self-healing, or cosmetic | No, unless it asks you to check something |
| **🙋** | The bot can't finish this — a person must | **Yes** |
| **📸 / 🎵** | A routine reminder — nothing is wrong | Only if you want the photo or the music |

**🙋** is used for exactly one thing today: a superseded post the bot tried and
failed to delete (see *Deleting a superseded post*). It should now be rare —
if you are seeing it regularly, the delete API has regressed again.

### Deleting a superseded post

When a stoppage-time goal changes the score after an early card has gone up,
`_delete_early_post` removes the outdated post before the corrected one is
published. This **works** — confirmed 2026-08-16 on Deportivo Alavés vs Getafe,
where the 2–0 early FT card was deleted and reposted as 3–0.

It did not always. Through July 2026 the endpoint failed for this account with
a persistent Meta-side error (HTTP 400 `"Fatal"`, subcode **2207085**) despite
a token holding `instagram_manage_contents` and requests matching the docs
exactly — tested across API v19–v25 with both Page and user tokens. Meta fixed
it server-side; nothing in this repo changed.

The manual-delete fallback stays in place for the regression, and the two
outcomes are easy to tell apart:

| Outcome | What you see |
|---|---|
| Delete succeeded | Nothing. Silence is the success signal. `[instagram] Deleted post: <id>` in the run log. |
| Delete failed | 🙋 alert with a tappable permalink, and `Warning: Instagram delete failed` in the run log. |

### Reminders

Two things the bot can't do for itself, so it asks:

**📸 Send the match photo** — fired from the worker's poll loop while the match
is live, so there is time to act before the card is built:

| When | For |
|---|---|
| 35' (first half) | the half-time card — skipped when `post_ht` is false |
| 80' (second half) | the full-time card |
| 110' (extra time) | the full-time card again, worded as a last call |

Each fires **once per worker run, and only if no photo has been uploaded** for
that match and moment — a HEAD check against Cloudinary, not a download. So it
is a nudge, not a nag: send the photo and it goes quiet. The 110' reminder has
its own flag, so it still fires for a match that went to extra time after the
80' one — unless a photo arrived in between, in which case it stays silent. If
you'd rather be asked again at 110' even when a photo is already in (to swap in
an extra-time shot), drop the `match_photo_exists` check for that one case.

**🎵 Add the music** — fired straight after every successful publish, with a
tappable link to the post: single cards, stats carousels, early posts and group
carousels alike. Instagram's publishing API can't attach audio, so every post
the bot makes goes up silent. A corrected early post reminds again, which is
right — it's a new post, and it needs its own track.

Covered: state-file load, registry parse errors, scraper outages (and the
stale-cache FT fallback that follows a long one),
pipeline failures (HT/FT and early posts), overlay-render fallback, worker
crashes, scraper failures mid-match, bad kickoff dates, Cloudinary/IG delete
failures (with a tappable permalink so you can delete manually), missing
crests (pre-kickoff *and* render-time), and unknown competitions. Alerts are
throttled per key so a stuck poll loop can't spam you.

Carousel and stats alerts: a stats page that fails to render or has no
statistics to draw (⚠️, the scorecard still posts), a match that can't be handed
to its carousel group (❌, the group waits), a group posted without members that
never arrived (⚠️), a group whose deadline passed with nothing at all to post
(❌), a group larger than Instagram allows (⚠️, first 10 posted), and a group
that failed to publish (❌, retried every 15 min).

When editing an alert, keep it readable by someone who has never seen the
codebase: no module names, no field names (`post_ft_stats`, `kickoff_utc`), no
`scraper` / `worker` / `pipeline` / `Cloudinary`. Say what happened to the post.

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

Every secret except the Facebook app pair is now needed by **both**
`match_bot.yml` and `dispatcher.yml`, since the dispatcher publishes carousel
groups. `dispatcher.yml` installs the full `requirements.txt` for the same
reason, and `match_bot.yml` grants `actions: write` so a worker can nudge it.

### `config.py`

Cloudinary public ids for templates (`CLOUDINARY_TEMPLATES`), the legacy WC
crest/tournament tables, the brand logo, local event symbols, stadium name
aliases, and the `get_crest_url` / `get_competition_logo_url` lookups.

### `matches.json` entry

```json
{
  "match_id": "54457604",
  "scraper_url": "https://m.allfootballapp.com/match/Main/Liverpool-vs-Leeds-United/54457604",
  "desktop_url": "https://www.allfootballapp.com/match/54457604",
  "kickoff_utc": "2026-08-02T23:00:00Z",
  "kickoff_local": "Mon 3 Aug 2026, 01:00 CEST",
  "home_team": "Liverpool",
  "away_team": "Leeds United",
  "competition": "Club Friendly",
  "post_ht": true,
  "post_ft_stats": true,
  "knockout_match": false,
  "carousel_group": "sat-15aug",
  "records": ["Optional hand-written storylines used in the caption…"]
}
```

`home_team` / `away_team` / `competition` are what appears on the card —
any reasonable spelling survives `validate_matches.py`.

| Field | Effect |
|---|---|
| `post_ht` | Post a half-time card. Default true. |
| `post_ft_stats` | Add the statistics page as slide two of the FT post. Ignored when the match is in a carousel group. |
| `knockout_match` | Wait through a level scoreline at 90' for ET/penalties rather than posting early. |
| `carousel_group` | Post with every other match carrying the same id, as one carousel. Omit for a normal solo post. Max 10 matches per group. |

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

- The scraper depends on allfootballapp's mobile DOM; a redesign there would
  need `football_scraper_dom.py` updated (you'd get scraper-failure alerts).
  It is the only live data source, so an outage stalls tracking — the
  stale-cache FT fallback exists to keep a long one from costing you the post.
- The desktop page **cannot** replace the mobile one: it carries no penalty
  shootout scoreline (nor half-time / extra-time scores), which the knockout
  path depends on. It is enrichment only. Its payload is a Nuxt `__NUXT__`
  JavaScript IIFE rather than JSON — `allfootball_desktop.decode_nuxt()`
  resolves it with the standard library, so no JS runtime is needed.
- Team A in the scraper is assumed to be the home team (the same assumption
  the scraper URL and scores already rely on).
- The statistics page depends on the desktop feed for its momentum chart, which
  is best-effort — the header degrades to a crest strip when it's unavailable.
  The stat rows come from the mobile scrape and are unaffected.
- A carousel group can only be as fast as its slowest match: the post waits for
  the last whistle in the group, then goes out within about a minute. If a
  member never finishes, the group waits four hours past the last kickoff before
  posting without it.
- Grouped matches don't post early and don't get corrected. That's deliberate —
  the group post happens once, after every result is final, so a wrong scoreline
  never goes up in the first place.

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

# render the stats page — enrich() first, or you get the crest-strip fallback
# instead of the momentum chart
venv/bin/python - <<'PY'
import json
from allfootball_desktop import enrich
from stats_card import generate_stats_card
match_id = '54457604'
data = json.load(open(f'data/{match_id}-Liverpool-vs-Leeds-United.json'))
enrich(data, match_id)
print(generate_stats_card(data, match_id_override=match_id,
                          home_name='Liverpool', away_name='Leeds United',
                          competition='Club Friendly'))
PY
```

Runtime artefacts (`output/`, `data/`, `assets/logos/`, `assets/templates/`,
`bot.db`, `state.json`) are gitignored; Cloudinary is the source of truth for
all images.
