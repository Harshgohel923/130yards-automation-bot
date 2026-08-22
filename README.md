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
7. Set `post_lineups` on a fixture and the worker also posts the **starting
   XIs** before kickoff, as a two-slide carousel — one team per slide, in the
   order you choose with `lineups_first`.
8. Set `post_ft_stats` on a fixture and its FT post becomes a two-slide
   carousel: the scorecard, then a **match statistics page**.
9. Give several fixtures the same `carousel_group` id and they post as **one
   carousel** instead of one post each — one scorecard per match, published by
   the dispatcher once the last of them has finished.
10. `/event` in the bot stages a photo against a player's moment — "Messi,
    goal" — and it posts on its own, exactly as you sent it, if that moment
    happens. If it doesn't, nothing is posted and the picture is deleted when
    the match ends. `/staged` lists what's armed and takes one back down. See
    *In-match event photos*.
11. Any failure anywhere sends you a descriptive Telegram alert.

---

## Architecture

### Where it runs

Production is **GitHub Actions**, not a server:

| Workflow | Trigger | Job |
|---|---|---|
| `.github/workflows/dispatcher.yml` | external cron + `schedule` every 15 min | Runs `.github/scripts/dispatcher.py`; spawns a `match_bot.yml` run per match whose window is open. Detects already-running workers by scanning the run names (`match-<id>`) of every **queued or in-progress** run — no external state. Queued counts: a run it just triggered sits queued for minutes, and two ticks inside one fixture's window would otherwise dispatch a duplicate worker. Also **publishes carousel groups** that are complete and **prunes finished fixtures** from `matches.json`, so it carries the same secrets a worker does, needs `contents: write`, and runs under a `concurrency` group to stay a single writer. |
| `.github/workflows/match_bot.yml` | `workflow_dispatch` (from dispatcher) | Runs `match_worker_runner.py <match_id>` for one match. Job timeout 360 min (GitHub's hard ceiling, measured from job start); the worker's own ceiling is `MAX_MATCH_DURATION` = 290 min, measured from kickoff. |
| `.github/workflows/telegram_bot.yml` | `workflow_dispatch` (from dispatcher) | Runs the Telegram bot under `.github/scripts/bot_watchdog.py` — while matches are active, and while someone is mid-conversation with it. Carries the Instagram and Gemini secrets too, because `/card` posts from here. |
| `.github/workflows/ci.yml` | push to master, PR | Lints for errors, compile-checks the entry-point scripts and runs the test suite. See [Tests and CI](#tests-and-ci). |

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
- **Starting XI post** (opt in with `post_lineups`): from the moment the
  worker starts it watches for the published line-ups and posts them as a
  two-slide carousel, then leaves them alone. It gives up at kickoff + 5 min
  with a 🙋 alert — see *The starting XI post* below.
- **Staged photo names get pinned** once both team sheets are published: every
  picture staged under a typed name is matched against the squads and either
  pinned to the feed's player id silently or asked about with buttons — see
  *In-match event photos* below. Runs before kickoff, because that is the last
  moment a mis-typed name can still be fixed by a person.
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
  kickoff + 290 min, so by six hours nothing can still act on the entry.
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
| `telegram_bot.py` | Photo intake: you send a match photo via Telegram, it lands in Cloudinary keyed by match id, and the pipeline switches to the photo-overlay card style. Also hosts `/event`, which stages a photo against one player's moment (see *In-match event photos*), and `/card` (see *Manual match cards*) — three conversations with disjoint state ranges, so a stray state value can never be read as another flow's. |
| `event_photos.py` | The vocabulary shared by the bot, the worker and the caption writer for staged event photos: the Cloudinary key (`event_photos/<match_id>_<KEY>_<player-slug>`), the event list the bot offers, and the pure matcher that decides which staged pictures the current timeline has fired. No I/O beyond the Cloudinary listing and deletion. |
| `manual_match.py` | Turns typed-in details into the same `scraper_data` dict the scraper returns, so a hand-entered match renders and captions through the ordinary pipeline. Parsers only — no I/O. |
| `card_batch.py` | The pile of hand-built cards waiting to go out as one post, stored on Cloudinary under `manual_cards/<telegram user id>/` so it outlives the bot process. Same idea as `carousel.py`, minus the deadline and the POSTED marker — this one is told when it's complete. |

### Rendering

| File | Role |
|---|---|
| `scorecard.py` | Classic template card: stamps crests, score, scorers (+minutes and event symbols), stage text, stadium onto the HT/FT template. Coordinates live at the top of the file as `*_BOX` tuples (picked with `pick_coords.py`). |
| `overlay_scorebar.py` | Photo card: renders a dark scorebar panel over the bottom of a match photo — crests, big score, team-colored underlines (colors derived from the crest), scorer lines, competition logo on the panel border, penalties strip. |
| `stats_card.py` | The FT statistics page — slide two of a `post_ft_stats` post. Same template as the scorecard (`scorecard.load_template`, seeded by `match_id`, so both slides share a background) but blurred and veiled, since the page is dense with small marks. Header is the **momentum chart**, not the score: slide one already carries that. Body is a priority-ordered stat list as team-coloured split bars. |
| `lineup_card.py` | The pre-match starting XI page — one per team, posted as a two-slide carousel before kickoff. Draws the scraper's position grid as a pitch in perspective, each player a shirt in the team's own colours, with the bench, referee and conditions down the left. Falls back to a listed team sheet when the feed publishes names without positions. |
| `carousel.py` | Carousel groups: the Cloudinary manifest store, the dispatcher nudge, and `publish_group`. See *Carousel groups* above. |
| `caption.py` | Instagram caption via Gemini (2.5-flash → 2.0-flash → plain fallback), fed the score line, events, and the optional hand-written `records` from `matches.json`. The hashtag line comes from `hashtags.py`, not the model. Also writes the caption for a staged event photo, which differs in that the match is still being played and the picture carries no text of its own. |
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

### The starting XI post (`lineup_card.py`)

Opt in per fixture with `"post_lineups": true`. **One page per team, posted as
a two-slide carousel before kickoff** — `"lineups_first": "away"` puts the away
side on slide one, otherwise the home side leads.

The layout is the familiar broadcast team sheet: both crests either side of
**VS** at the top with the opponent's badge dimmed, the bench down the left,
the XI on the pitch.

What it draws:

- **A pitch in perspective** — the team's own goal at the bottom, the
  opposition's at the top. The scraper gives each starter a grid position, not
  coordinates: `position_x` is the band (`GK`, `D1`, `DM`, `M`, `AM`, `A`) and
  `position_y` the side within it (`L`, `CL`, `C`, `CR`, `R`). Bands become
  rows from the near goal up, sides become columns — so any shape the feed
  reports renders without a per-formation template, and the **formation label**
  (`4-2-3-1`) is counted off the rows rather than trusted from the feed. Each
  row is spread across the pitch's width *at its own depth*, so the far rows
  narrow the way they do on television.
- **Columns sit part-way between an even spread and their nominal side.** An
  even spread alone draws two wing-backs as a narrow pair in the middle; the
  nominal sides alone leave a hole in the centre of a back four. The blend
  keeps a flat four evenly spaced *and* pushes a lone wide player out to the
  touchline.
- **A drawn shirt per player**, in the team's own colours — curated in
  `overlay_scorebar.TEAM_COLORS`, sampled from the crest otherwise, exactly as
  the scorer underlines and the stats bars are. Squad number on the front, name
  on a coloured plate underneath. The keeper wears the change colour.
- **The bench** down the left column, surnames only, ending in `+N MORE` when a
  national side names more than the column holds.
- **Coach, referee and conditions** under the bench. Referee and conditions
  (temperature, with the weather beneath it) come from the same feed block the
  line-ups do; the **coach comes from `matches.json`**, because no feed carries
  one — neither the mobile payload's formation block nor the desktop page has a
  manager field. All three are routinely absent, so each is drawn only when it
  exists and whichever remain close the gap.

The header sits on the **pitch's** axis rather than the page's: the bench takes
the left quarter, so crests centred on the canvas would read as off-centre above
the pitch they belong to.

**When it posts.** Line-ups are announced around an hour before kickoff and the
worker starts 30 minutes before, so the card is normally ready on one of its
first polls. The feed publishes the names first and the grid positions a few
minutes later, so:

- positions available → the pitch, immediately;
- names but no positions → **wait**, until 5 minutes before kickoff, then post
  the XI as a listed **team sheet** in place of the pitch, with the rest of the
  page unchanged (some competitions never get positions — the feed reports this
  itself as `formationFlag`);
- nothing published by kickoff + 5 minutes → give up with a 🙋 alert.

Nothing here can affect the rest of the match: the HT and FT posts read their
own state and are never gated on the line-ups. A failed line-up post retries
every minute until the deadline and alerts once (⚠️) if it never succeeds.

> Colours come from the crest when a team isn't in `TEAM_COLORS` — accurate to
> the badge, not always to the kit. Add the team there (`'Man City':
> [(108, 171, 220), (28, 44, 92)]`) to pin its exact shirt colours; every other
> card style picks the change up too.

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

## The Telegram bot, step by step

`telegram_bot.py` runs four flows in three conversations, plus two commands
that belong to no conversation at all — `/batch` and `/staged`, each a list
with buttons on it. Two of the flows attach a photo to a fixture the scraper is
already covering, one stages a photo against a player's moment in it, and the
last builds a whole match from nothing. This
section is the exhaustive reference — every prompt, everything each step
accepts, what it does when it can't read you, and where it goes next. The *why*
behind the manual flow is in [Manual match cards](#manual-match-cards) below,
and behind `/event` in
[In-match event photos](#in-match-event-photos).

### What it accepts at all

The vocabulary is fixed and free text has no meaning except where a step asks
for it — `/card` throughout, and the player's name in `/event` when no squad
can be listed. Everywhere else it is photos, the commands, and the buttons that
mirror them. Anything else gets the
help text back rather than being parsed or ignored — a bot that stays silent is
indistinguishable from a bot that is down.

**Inside `/card` that catch-all steps aside.** Handlers in different groups each
get a turn at the same update, so the group-1 catch-all sees every answer to
every question — and would otherwise reply "I only understand photos and the
buttons below" to a perfectly good team name, immediately after the
conversation had accepted it. `stray_message` returns early while
`user_data['manual']` exists. Nothing is ignored by that: the conversation's own
handlers cover every message while it is live — a valid answer advances, an
invalid one is rejected with a reason, and anything else re-asks the step.

| | Published to Telegram's ☰ menu | Button |
|---|---|---|
| `/start`, `/newphoto` | Send the background photo for a half-time or full-time card | 📸 Scorecard photo |
| `/event` | Stage a photo for a player's moment in a match | 🎯 Event photo |
| `/staged` | Photos armed for a moment — tap to take one back | — |
| `/card` | Build a match card from details you type in | 🆕 Manual card |
| `/batch` | Cards waiting to be posted together | — |
| `/cancel` | Abandon whatever is in progress | 🚫 Cancel |
| `/help` | What this bot accepts | ❓ Help |

### Which photo is which

Two of the flows take a photo and the buttons sit next to each other, so this
is the only thing to decide before starting either:

| | 📸 Scorecard photo (`/start`) | 🎯 Event photo (`/event`) |
|---|---|---|
| **What it becomes** | the background a card is drawn on | the post itself |
| **Drawn on it** | score, crests, scorers, competition logo | nothing — posted exactly as sent |
| **Posts when** | half time / full time, with the card | the moment happens, on its own |
| **If you don't send one** | the card posts on the standard template | nothing is posted, and nothing is missing |
| **Keyed by** | match + HT/FT | match + event + player |
| **How many per match** | one per moment, replaced by re-sending | as many as you stage — each is its own post |
| **Caption** | the card's own HT/FT caption | its own, written from the moment |
| **Kept afterwards** | yes | deleted when the worker finishes |

A photo sent with no command takes the first column — the bot says so in its
reply, so a wrong guess is visible before you answer anything.

`TELEGRAM_ALLOWED_USER_IDS` gates every entry point. Unset means anyone who
finds the bot can use it, which for a bot that overwrites match photos and
posts to Instagram is worth not doing.

**In every state**, `/cancel`, `/help` and `/batch` keep working — they are
registered as fallbacks on all three conversations, so a button tap mid-flow
does what it says instead of being swallowed as an answer. `/card` and `/event`
reach across from anywhere for a different reason: their conversations are
registered ahead of the scorecard one, so their entry points see the command
first. `/help` and `/batch`
answer *without* disturbing the state you were in.

### Flow A — 📸 Scorecard photo (`/start`)

Renders that match's next card in the photo-overlay style instead of on the
template. The photo is stored as `match_photos/<match_id>_<HT|FT>` with
`overwrite=True`, so re-sending simply replaces it.

| Step | Prompt | Accepts | Otherwise | Next |
|---|---|---|---|---|
| 1 | "Select the match:" — one inline button per fixture in `matches.json`, plus Cancel | a tap | an empty registry ends the flow with "No matches found in matches.json." | 2 |
| 2 | "Which scorecard is this photo for?" — Half Time / Full Time / Cancel | a tap | a match id that vanished from the registry mid-flow ends with "Match not found" | 3 |
| 3 | "Now send the photo." — with the tip to send it as a file/document for full quality | a photo or an image document | a non-image document is told so and the step repeats | upload |

At the upload: anything over Telegram's own 20 MB ceiling is refused *before*
the download, with a message saying so rather than a generic failure. Anything
over 9 MB or longer than 2560px on its long edge is downscaled first, because
Cloudinary rejects oversized images and the overlay renders at the photo's own
resolution anyway. A failed upload answers in the chat and leaves you in step 3
to try again — a silent failure here is the worst outcome available, since the
photo looks sent and the card goes out on the plain template hours later with
nobody having been told.

### Flow B — a photo sent with no conversation

Sending a bare photo *starts* a conversation around it: "Got the photo. Which
match is it for?" → match → HT/FT → uploaded immediately, since the photo is
already in hand.

This path exists because the bot restarts often and conversation state lives
only in memory. If it restarted between picking the match and sending the
picture, the photo belongs to no conversation at all — and the only thing worse
than asking which match it is would be dropping it. The same fallback catches a
photo that arrives in step 3 after the state was lost: it re-asks rather than
discarding.

### Flow C — 🎯 Event photo (`/event`)

Its own conversation, entered with `/event` or 🎯 Event photo. It never touches
the scorecard flow: that one answers "what goes under the half-time card", this
one answers "what goes up on its own if Messi scores". Folding seven event
buttons into the HT/FT step would have made the everyday case read the rare
one's options every time.

The picture is stored as `event_photos/<match_id>_<KEY>_<player-slug>`. The
extra question versus Flow A is *who*, and it isn't optional — that is what
names the file.

| Step | Prompt | Accepts | Otherwise | Next |
|---|---|---|---|---|
| 1 | "Which match is the picture for?" — one inline button per fixture in `matches.json`, plus Cancel | a tap | an empty registry ends the flow with "No matches found in matches.json." | 2 |
| 2 | "What has to happen for this picture to post?" — ⚽ Goal, 🅿️ Penalty goal, 🥅 Own goal, 🅰️ Assist, ⚽⚽ Brace, ⚽⚽⚽ Hat-trick, 🟡 Yellow card, 🔴 Red card, ⬆️ Subbed on, ⬇️ Subbed off, plus Cancel | a tap | a match id that vanished from the registry mid-flow ends with "Match not found" | 3 |
| 3 | "Which team is the player in?" — one button per side, plus ✏️ Type the name instead / Cancel | a tap | no team news published yet ⇒ says so and goes straight to 4b | 4a |
| 4a | "&lt;Team&gt; — who is the picture of?" — the starting XI then the bench, two names to a row, plus ✏️ Type the name instead / Cancel | a tap | a squad that expired under a restart falls through to 4b | 5 |
| 4b | "Type the player's name…" | typed text | text with no letters or digits in it is rejected and the step repeats | 5 |
| 5 | "Now send the photo." — with a reminder that it posts exactly as sent | a photo or an image document | a non-image document is told so and the step repeats | upload |

A goal's *type* is its own button at step 2 rather than a follow-up question,
because a penalty and an open-play goal are different moments wanting different
pictures.

The squad list is fetched from the scraper on demand and cached for the
conversation. Tapping a name is strictly better than typing one, and the two do
**not** end up in the same place: a tapped name comes with the feed's own
numeric id for that person, which is written onto the picture as Cloudinary
context metadata (`player_id`, `player`) and is what the worker matches on
afterwards. A typed name carries no id and is matched on spelling until
something pins one — see
[Player ids, and why names are only a fallback](#player-ids-and-why-names-are-only-a-fallback).

The feed publishes team news about an hour before kickoff, so a picture staged
before that has nothing to tap. That is what the typed fallback is for, and it
is not a dead end: the worker asks about it the moment the sheets land.

Steps 3–4 also accept a photo sent early: it is held rather than dropped, and
uploaded the moment the player is settled. There is no equivalent of Flow B
here — three questions have to be answered before a picture can be filed, and
guessing any of them would stage it against the wrong moment, so a photo that
arrives with no conversation goes to Flow B and becomes a scorecard photo.

At the upload, on top of everything Flow A does: the picture is centre-cropped
if its shape falls outside 3:4–1.91:1, and the reply says so. An event photo is
posted as-is, with no card drawn on it, so its own shape is what Instagram is
asked to accept — and a portrait phone photo at 9:16 would simply be refused. A
picture already inside the band keeps every pixel it arrived with.

### Flow D — 🆕 Manual card (`/card`)

Ten steps. Every field is parsed the moment it arrives, so a bad value is
caught on the message that contained it rather than at render time; a rejected
field leaves the state unchanged, so the next message is another attempt at the
same question and nothing typed earlier is lost.

| # | Asks | Expected format | Rejected when |
|---|---|---|---|
| 0 | Opens with what this is, whether the card is joining a pile already in progress, and how to stop | — | — |
| 1 | **Home team** | A name, 1–40 characters, containing at least one letter. `Arsenal`, `Real Betis`, `FC Schalke 04`, `Зенит` | empty; no letters (`1234`); over 40 characters; or it parses as a score or a date, which means the answer belongs to a later step. **Then the crest is checked** — see below |
| 2 | **Away team** | as above | as above |
| 3 | **HT or FT** | a button tap | — (typed text re-asks) |
| 4 | **Score**, home first | `2-1`. Separator may be `-`, `–`, `—`, `:` or `x`; spaces ignored; one or two digits a side | anything else — `3`, `two-one`, `2-1-1` |
| 5 | **Competition** | A name, 1–60 characters, at least one letter. `Premier League`, `Club Friendly` | as team name, with the longer limit. **Then the competition badge is checked** |
| 6 | **Date** | `21/08/2026` (day first), `2026-08-21`, `21-08-2026`, `21.08.2026`, `21 Aug 2026`, `21 August 2026`, `21/08/26` | unparseable; before 1900; or more than two days in the future — a result can't be entered for a match that hasn't been played, so that's a transposed year |
| 7 | **Home scorers** | `minute name (type)`, one per line — see below. Or `none`, or the **Nothing to add** button | any line that doesn't fit, quoted back with what was wrong with it. Then the goals are counted against the score |
| 8 | **Away scorers** | as above | as above |
| 9 | **Background photo** | a photo, an image document, or the *Use the standard template* button | a non-image document; over 20 MB (refused before the download, since Telegram won't serve it to a bot) |
| 10 | The card, then the readback and the buttons | 📤 Post / ➕ Add another card / 🗑 Discard this card | typing anything here re-asks instead of abandoning the card |
| 11 | **Theme** — only when Post was tapped on a pile of two or more | One line, 1–120 characters, at least one letter. `Arsenal's last five` | empty; no letters; over 120 characters. A date-like phrase is *fine* here — `Every match in August 2026` is a good theme — so the wrong-step check doesn't run |

Every check **rejects rather than guesses or truncates**, names the value it
couldn't use, and says what it wanted instead. The state is unchanged, so the
next message is another attempt at the same question and nothing typed earlier
is lost:

```
you  →  2-1
bot  →  "2-1" looks like a score, not the team name — I think that
        answer is meant for a later step.

        Right now I need the team name, like Arsenal or Real Betis.
        The score and date come after this.
```

That last check exists because ten questions in a row is exactly the shape of
interaction where an answer lands one step early or late. Without it a
scoreline typed into "home team" becomes a team called `2-1`, renders, and
posts. Silently trimming an over-long name would be the same class of bug: it
would put a wrong name on a public post, where saying "that's 45 characters and
the limit is 40" costs one message and gets the right one.

**Step 3 before step 4** is deliberate: asking for a *final* score and then
being told it was a half-time card reads as a contradiction, and the two draw
from different fields — a half-time card stores `hts_*`, a full-time one `fs_*`.

**Steps 7–8, the scorer grammar.** One event per line, minute first. Plain
lines are goals:

```
23 Saka                 goal
45+2 Havertz (pen)      penalty, in first-half stoppage time
67 Rice (og)            own goal
88 Odegaard (red)       red card
90 Martinelli (miss)    penalty missed
```

The trailing apostrophe people naturally type (`23' Saka`) is optional, as is
the separator (`23. Saka`, `23 - Saka`). Suffixes are case-insensitive and have
aliases — `(pen)`, `(penalty)`, `(pk)`; `(og)`, `(own goal)`; `(red)`, `(rc)`,
`(sent off)`; `(miss)`, `(missed penalty)`. `none`, `-`, `n/a` and `nil` all
mean the team had none.

Each line is bounded as well as parsed, because the grammar alone accepts
things that aren't facts about a football match:

| Rejected | Because |
|---|---|
| `900 Rice` | there's no 900th minute — minutes run 1–130 |
| `0 Saka` | same |
| `45+99 Saka` | more than 30 minutes of stoppage time |
| `23 123` | `123` isn't a name |
| `23 <41+ characters>` | the name wouldn't fit the column |
| `23 Saka (assist)` | `assist` isn't a type — only `pen`, `og`, `red`, `miss` |

A line that can't be read **rejects the whole block** rather than being
skipped, and the message quotes back exactly which lines failed and why:

```
I couldn't read these lines:

• 900 Rice  ← there's no 900th minute

The format is one event per line: minute, name, then an optional
type in brackets.
…
```

A half-read list is worse than a rejected one: the card would go out looking
complete with a goal quietly missing from it. Own goals appear in whichever
column you enter them under, which is normally the team they counted for.

**Step 9, the background.** Optional. Send one and the card renders in the
photo-overlay style; skip it and it goes on the usual template — whose design
is seeded by the generated match id, so re-entering the same match on the same
date lands on the same one. Oversized photos are downscaled exactly as in
Flow A.

**Step 10, what you get.** The card arrives twice — as a photo to look at, and
as the uncompressed PNG to keep — because the card is the deliverable and
posting is optional. Under it, a readback of what you typed:

```
Arsenal 3–1 Man City
Premier League · Full time · 21 AUG 2026
Background: standard template

⚠️ The scorers add up to 1–1, not 3–1. Worth a look before posting.

This post would be a carousel of 2, in this order:
1. Arsenal 3–1 Man City
2. this one
```

It repeats the input rather than describing the picture: the picture is right
there, so what is worth checking is whether the bot understood you. The
scorers-vs-scoreline mismatch is a **warning, not a block** — a card can
legitimately show fewer scorers than goals.

Rendering runs off the event loop (`asyncio.to_thread`), so a slow crest
download doesn't freeze the bot. If it fails, nothing is posted and you stay in
step 9 to try a different background.

#### The message box

Steps that want typing use `ForceReply` with a placeholder, so the box is
focused and labelled with what it wants — `Arsenal`, `2-1`, `21/08/2026`.

Telegram gives a bot **no way to disable, hide, or grey out the message box**.
There is no such field in the Bot API and a bot cannot restrict what a client
lets someone type, so `ForceReply` is the whole of what's available. The
complement is that steps which *don't* want typing carry inline buttons
instead, and typing at one of them re-asks rather than being swallowed.

A message may carry only one `reply_markup`, so a step that already offers a
button — the scorer lists, the theme — keeps the button. A tappable exit is
worth more than a hint.

#### Counted against the score

The goals entered are checked against the score you already gave, on the step
that entered them:

```
⚠️ The score says Man City scored 2, but you've given me 1, which is
1 short. A goal missing from the list, or a minute that didn't parse?

Goals I counted:
• 23' Haaland

Red cards and missed penalties aren't counted — only goals, penalties
and own goals entered in this column.

Send the list again to replace it, or use the buttons.

[✅ That's right, carry on]
[✏️ Let me redo them]
```

Here rather than only at the preview, because here it can still be acted on
cheaply — the list is the last thing typed and fixing it is one message. At the
preview it's three steps back, and the natural response to a warning that late
is to post anyway. The preview keeps its own copy of the check as a backstop.

It **asks rather than refuses**: a scorer can be genuinely unknown, and a card
can legitimately show fewer names than goals. Only goals count — a red card or
a missed penalty for a team that didn't score is perfectly normal, and counting
either would flag every match that had one. Penalties and own goals do count,
since both put a number on the scoreboard.

#### A team that didn't score

The step is still asked — a 0-2 loss can still have a sending-off — but it says
so, and every scorer step carries a **Nothing to add** button:

```
Arsenal didn't score, so there are no goals to enter.

If anyone was sent off or missed a penalty, add it — otherwise tap
the button.

[Nothing to add]
```

`none` has always worked, but it's a thing you have to have read; a button is a
thing you can see. The button doesn't bypass the count check — tapping it when
the score says two goals is exactly the mistake worth catching.

### Badges — checked while the name can still be fixed

A scraped fixture has its crests guaranteed before kickoff by
`validate_matches.py`. A typed-in name has no such guarantee, and a missing
crest is not cosmetic: it is a blank hole in the middle of the card, which
without this check you would discover at step 10, when the name that caused it
is eight messages back.

So the same check runs the moment a name is entered, against the same
functions — `logo_fetch.normalize_team_name`, then `fetch_logo`, then
`config.get_crest_url` as a fallback. Three outcomes:

| | What you see |
|---|---|
| Crest already on Cloudinary, or `logo_fetch` finds and uploads it | Nothing. The flow moves to the next question |
| The name resolves to a different official spelling | "Found it — filed as **Tottenham Hotspur**, so that's what goes on the card." The card and the crest lookup then both use the official name — the same rewrite `validate_matches.py` applies to `matches.json` |
| Nothing found | The flow stops and asks |

That last case:

```
I can't find a badge for Wingate & Finchley FC, so that side of the
card would have a blank space where the crest goes.

Three ways out:
• Send me a PNG of the badge now and I'll save it — it'll be there
  for every future card too
• Fix the spelling — badges are filed under official names, so try
  Tottenham Hotspur rather than Spurs
• Carry on without it

[✏️ Let me fix the spelling]
[➡️ Carry on without it]
```

Sending a PNG runs `logo_fetch.upload_local_crest`, which writes it to the
deterministic public id every renderer already looks at — so the badge is there
for every future card, and for scraped fixtures with that team too.

**The upload is never forced.** `upload_local_crest` refuses when a badge
already sits at the public id the name resolves to, and that refusal is
honoured rather than overridden: the lookup that sent you to this question
searches a different table from the one deciding where an uploaded file lands,
so a name it could not resolve can still slugify onto a real club's crest.
Forcing it would break every future card for a team nobody was talking about.
When something is already there, it's a better badge than the one being
offered — the bot says so and uses it.

**A competition badge is milder.** `get_competition_logo_url` falls back to the
130 Yards mark, so a missing one is genuinely cosmetic and the question says
so — but it still asks, because a silent fallback looks deliberate.

**None of this fires a Telegram alert.** `get_crest_url(alert=True)` normally
sends one telling you to run `validate_matches.py`; here the bot is already
talking to you about that exact team, so every lookup passes `alert=False`.

### The three buttons at step 10

| Button | What happens |
|---|---|
| **➕ Add another card** | Uploads this card to the pile, confirms "Card *n* saved. Nothing has been posted", and reopens step 1 for the next match |
| **📤 Post** | Uploads this card to the pile, then publishes the whole pile — one card as an ordinary image post, two or more as a carousel — and clears it. The label counts: "Post these 3 cards" |
| **🗑 Discard this card** | Drops the card just built. The pile is untouched |

Both keeping paths **save the card before doing anything else**, so an upload
failure is discovered while the render still exists and can be retried, rather
than after it has been deleted. If the bot restarted between the render and the
tap, the card is gone and it says so plainly — anything already on the pile is
unaffected.

### `/batch` — the pile

Its own command rather than a step, because the case it exists for is the
conversation having ended without you and the cards outliving it.

```
2 cards waiting — this would post as a carousel, in this order:

1. Arsenal 3–1 Man City
2. Arsenal 2–0 Chelsea

[📤 Post all 2 now]
[➕ Add another card]  [🗑 Clear]
```

**Add another card** here is registered as an *entry point* of the card
conversation, not an ordinary callback — only a conversation handler can put
you into a state, so a plain handler could ask the first question and then have
nowhere to put the answer.

### Staying alive

Every update the bot handles touches `.bot_session` (a handler in group −1 that
consumes nothing and exists only for the timestamp). The watchdog keeps the bot
running while that file is under 20 minutes old, so it cannot shut down
mid-scoreline. See [Waking the bot](#waking-the-bot).

---

## In-match event photos

A picture, uploaded before the match, that posts on its own if a particular
player does a particular thing. Nothing about it is automatic in the sense of
guessing: it is posted because somebody decided beforehand that it should be,
and staging it *is* the approval.

```
before kickoff   /event → match → ⚽ Goal → team → Messi → photo
                 → event_photos/54533571_GOAL_messi

23'              Messi scores. The next poll sees it in the timeline,
                 finds the picture, writes a caption, posts it.

no goal          nothing happens, ever. The picture is deleted when
                 the worker finishes.
```

### Using it

**1. The fixture has to be in `matches.json` on `master`.** The bot builds its
match list from its own checkout of that file — there is no free-text match
entry in this flow, by design. Push a fixture while the bot is already running
and it won't appear until the bot restarts.

**2. Wake the bot if it's asleep.** It only exists while an Actions job holds
it. Send it anything; the dispatcher's next 15-minute tick peeks at Telegram's
queue, sees a message waiting, and starts it (`dispatcher.wake_telegram_bot_if_messaged`).
Worst case you wait a quarter of an hour for the first reply. While a match is
running the bot is already up, so this only bites well before kickoff.

**3. Stage the photo.**

```
/event                        or tap 🎯 Event photo
  → Argentina vs Brazil       one button per fixture
  → ⚽ Goal                    what has to happen
  → Argentina                 narrows the name list; not part of the key
  → Messi                     tapped from the XI, or typed
  → send the photo
```

The reply confirms with the `public_id` and the URL, so you can see exactly
what was stored. Repeat from `/event` for each one.

**4. Nothing else.** No flag in `matches.json`, no restart, no push. The
uploading *is* the opt-in, and the worker picks it up on its own.

**5. `/staged` if you want to check, or change your mind.** See below.

### `/staged` — what's armed, and the only safe way to take it back

A staged picture is invisible by design. It lives under a Cloudinary name
nobody sees, does nothing for hours, and then posts on its own, in public. So
there has to be an answer to "what did I arm?" that isn't the Cloudinary
console — and a way to disarm one that isn't deleting an object there by hand,
which is the same operation with none of the checks and a different match's
photo one keystroke away.

```
/staged

🎯 Real Madrid vs Barcelona
2 photos armed:

• Rodrigo — ⚽ Goal
• vinicius — 🔴 Red card   (by name)

Tap one to take it back down.

[ ❌ Rodrigo — ⚽ Goal ]
[ ❌ vinicius — 🔴 Red card ]
```

One message per fixture that has something armed; a fixture with nothing is not
mentioned. `(by name)` marks a picture with no `player_id` on it yet — it still
fires, but only if the scoreboard spells the name that way.

Details that are decisions rather than defaults:

- **It is not a conversation.** It is a list and a tap, and it has to work
  while `/card` or `/event` is half-finished — which is exactly when you notice
  something was armed by mistake. The scorecard flow's callbacks were given
  patterns so a `❌` tap falls through to its own handler instead of being read
  as an answer to the question on screen.
- **The button carries a digest, not a position.** Same reason as the
  clarification buttons, and the same mechanism: the tap is resolved against a
  fresh listing. A stale listing's button removes the picture it was drawn for
  or nothing at all — never whatever has since moved into that slot.
- **One Admin API call**, not one per fixture. `staged_all()` lists the folder
  once and sorts it by the `match_id` in each public_id; ten fixtures in the
  registry would otherwise be ten listings to draw one message.
- **Every failure leaves everything armed.** A Cloudinary outage on the listing
  says "nothing has changed"; one on the delete says the photo is still staged
  and will still post.
- **One tap, no confirmation.** The button names exactly what it removes, and
  re-staging is `/event` again.

### When you can do it

| | |
|---|---|
| **Earliest** | as soon as the fixture is in `matches.json` and the bot is awake. Days ahead is fine — the photo just sits in Cloudinary. |
| **Squad buttons appear** | ~1 hour before kickoff, when the feed publishes team news. Before that the bot asks you to type the name. |
| **Worker starts looking** | 30 minutes before kickoff (75 with `post_lineups`) — `dispatcher.PRE_MATCH_WINDOW_SECS`. |
| **Still works during the match** | yes. The timeline is re-read whole every poll, so a photo staged *after* the goal still posts — within a poll or two of the listing cache refreshing, so about three minutes. |
| **Deadline** | full time. The worker deletes everything staged for the match as it exits, so anything sent after that is gone with no post. |

Typing is not a degraded path — `messi`, `Messi` and `MESSI` all land on
`…_GOAL_messi` — but it is the weaker one, and only because of *which* name you
type. Type it once team news is out and the bot checks it against the squad and
offers corrections; type it before, and there is nothing to check it against.
If you stage early, prefer the name as the scoreboard would print it — and
don't worry too much, because the worker comes back to you about it the moment
the team sheets are published. See *Names, ids, and the four places they can
disagree*.

### The 15-minute tick is not what checks for the photo

Worth separating, because the two clocks are unrelated:

| | Dispatcher | Match worker |
|---|---|---|
| Runs | every 15 min, always | only while a match is live |
| Does | spawn workers, wake the bot, publish carousels, prune the registry | poll the scraper, post cards, **check for staged photos** |
| Looks for event photos | never | every poll, listing cached 120s |

The dispatcher never touches event photos. The check lives entirely inside the
match worker's once-a-minute poll loop.

### Why the player is part of the key

Because a match has several of each moment and they are not interchangeable.
Messi scoring against Brazil and Álvarez scoring against Brazil are two moments
wanting two pictures, and Neymar's red card in the same game is a third. Keying
on the fixture and the event alone would make them one, and the wrong picture
would go up for two of them.

So the Cloudinary name carries all three: `event_photos/<match_id>_<KEY>_<slug>`.
The bot writes it, the worker recomputes it from the scrape, and neither passes
anything to the other — which matters, because the bot restarts constantly and
the worker is a different process on a different Actions run entirely. A name
both can derive is worth more than any state either could hold.

`<slug>` is the player's name with the differences folded out — accents, case,
punctuation, spacing — so `Gerónimo Rulli` typed as `geronimo rulli` still finds
the picture. Slugging handles the mechanical differences; the ones that are
about the name itself (`Rodri` vs `Rodrigo`) are handled separately — see
*Names, ids, and the four places they can disagree* below.

The slug is what the picture is *stored* under. It is no longer what it is
*found* by wherever an id is available — see the next section.

### Player ids, and why names are only a fallback

The feed gives every person in a match a numeric id — `person_id` on a team
sheet, `player_id` on a timeline entry — and it is the same id in both places.
That is the feed's own key for a human being, and it is exact: it cannot be
confused by two players sharing a surname, and it does not move when the feed
changes its mind about how to spell someone halfway through a match.

So wherever an id is available, that is what a staged picture is matched on.
Names are the fallback for a picture staged before any id was knowable, and
everything tolerant and fallible about matching lives on that path alone.

**Where the id comes from.** The public_id can't carry it — it is fixed at
upload and the id often isn't known yet — so it rides on the picture as
Cloudinary **context metadata**, which the same Admin API listing already
returns. Two things write it:

| | |
|---|---|
| Tapping a name in `/event` | the squad list carries `person_id`; the upload writes `{player_id, player}` |
| The worker, when the sheets drop | `set_player_id()` adds it to a picture already uploaded — see the next section |

**Where it is read.** `_hits_for()` is the whole of the matching rule:

```python
if entry['player_id']:                       # exact — the feed's own key
    return [m for m in same_event if m['player_id'] == entry['player_id']]
return [m for m in same_event                # tolerant — spelling, and hope
        if names_match(entry['slug'], m['slug'])]
```

The tallies behind ⚽⚽ Brace and ⚽⚽⚽ Hat-trick key on the id too, so two
players with the same short name can't pool their goals into one hat-trick.

**Where it matters most.** A posted picture records the id it fired on. That is
what lets the liveness check that guards retraction read the *timeline alone* —
see [When VAR takes the goal away](#when-var-takes-the-goal-away), where
depending on anything else once cost every correct post of a match.

`/staged` shows which pictures have an id and which are still going on
spelling; the ones without are marked `(by name)`.

### Names, ids, and the four places they can disagree

One end of this is a person typing a name; the other is a feed printing a team
sheet. They disagree constantly — `Messi` vs `Lionel Messi`, `felix` vs `Joao
Felix`, `Rodri` vs `Rodrigo` — and an unreconciled disagreement is a photo that
never posts and never says why. Four defences, in the order they get a chance.
The first two end the argument by replacing the name with an id; the last two
are what is left when neither could.

**1. At staging time — `event_photos.suggest()`, in the bot.** A typed name is
checked against both squads before it is believed. Exact match, and it is
stored under the feed's spelling. No exact match, and you get the near ones as
buttons:

```
you type:  Rodri
bot:       I can't see "Rodri" in either squad. Did you mean one of these?
           [ Rodrigo ]
           [ Neither — stage "Rodri" as typed ]
```

Deliberately loose — prefixes, typos, initials all suggest — because a wrong
guess costs one tap. This is the only place `Rodri` → `Rodrigo` can be settled,
and it is why picking from the list is worth waiting for team news. Nothing is
*blocked*: a name the squad doesn't know still stages, with a warning. A squad
list can be incomplete, and refusing an upload over a guess would be worse than
the silence it is preventing.

**2. When the team sheets drop — `event_photos.clarify()`, in the worker.**
The moment both XIs and both benches are published, every name in the match is
knowable and there is still an hour before kickoff. That is the only point
where both of those are true, so it is where every picture still going on
spelling gets settled — silently where it can be, with a question where it
can't.

| What the sheets say | What happens |
|---|---|
| exactly one player spells it that way | pinned to their id. Nothing is sent — it was right all along |
| no exact spelling, and exactly one player `names_match()` would have fired for anyway (`felix` ⊂ `Joao Felix`) | pinned. The same decision the worker would have made unattended, made once, against the whole squad instead of a goal at a time |
| several it could be, or none that matches but some that resemble it | asked, with buttons |
| nothing in either squad that resembles it at all | said out loud, with no buttons — there is nothing to offer |

```
🙋 Real Madrid vs Barcelona: the team sheets are out and I can't tell who
   the photo you staged as "rodri" (⚽ Goal) is of.

   [ Rodrigo ]
   [ Leave it as "rodri" ]
```

Both benches have to be published, not just the XIs. The feed fills the sheet
in stages, and asking before the substitutes are listed would report half the
squad as missing — a question that answers itself, wrongly.

The tap is handled by the bot, in a different process on a different machine
that shares nothing with the worker. So the button carries only what both sides
can compute from the picture itself: an 8-character digest of its public_id
(`event_photos.digest` — a public_id plus a player id does not reliably fit
Telegram's 64-byte `callback_data` cap) and the id of the player being offered.
The bot resolves that digest against a *fresh* listing, which is why the button
still works after a restart and can never land on whatever has since taken that
position in a list.

*Leave it as "rodri"* is recorded on the picture itself (`clarified`), not
remembered in the process. A deliberate refusal looks exactly like an
unanswered question, and without the marker the worker would ask again every
hour for the rest of the match.

The pass runs on every poll once the sheets are out, not once at the top, so a
picture staged twenty minutes before kickoff gets the same treatment as one
staged yesterday. Repeating it is cheap: the staged listing is already cached,
a pinned picture drops out of the answer, and the questions carry an hour's
cooldown.

**3. At fire time — `event_photos.names_match()`, in the worker.** Unattended,
against a live match, so strict: exact, or one name's words wholly inside the
other's.

| Staged | Feed says | Fires | |
|---|---|---|---|
| `messi` | Lionel Messi | ✅ | a shorter form of the same name |
| `felix` | Joao Felix | ✅ | |
| `joao-felix` | Felix | ✅ | symmetric |
| `rodri` | Rodrigo | ❌ | a different word, not a shorter name |
| `n-gonzalez` | Nico Gonzalez | ❌ | an initial is not a given name |

`rodri` → `rodrigo` is refused on purpose. Loosening to prefixes would make it
match Rodrigo *and* Rodríguez, and the wrong player's photo on a public page
has no undo — a miss is recoverable, a false positive isn't.

This whole table only applies to a picture with no `player_id` on it. One that
has been pinned — tapped from the squad at staging time, or settled by defence
2 — never reaches `names_match()` at all.

**Ties are reported, never guessed.** A staged `silva` when both Silvas were
booked returns a `conflict` instead of a match: nothing posts, and an alert
names both.

**4. At full time — `event_photos.unfired()`.** Whatever slipped through both,
said out loud while the timeline is still in hand:

```
🙋 Man City vs Chelsea: 1 staged photo never posted because the name
   didn't match what the scoreboard called the player.

   • you staged "rodri" for ⚽ Goal — the scoreboard says Rodrigo

   The moment did happen — the spelling is what missed.
```

Only near misses are reported. A photo staged for a goal that never came is the
feature working as intended, and alerting on it would train you to ignore the
ones that matter.

### What fires it

Every poll, the whole timeline is re-read and checked against what is staged.
Not diffed against the last poll — re-read — so a moment the feed publishes
late, or drops for a poll and republishes, is still caught. What stops a second
post is the posted key, not having seen the event before.

| Button | Fires on |
|---|---|
| ⚽ Goal | `goal` |
| 🅿️ Penalty goal | `penalty_goal` |
| 🥅 Own goal | `own_goal` |
| 🅰️ Assist | the `assister` on a `goal` or `penalty_goal` entry — see below |
| ⚽⚽ Brace | *derived* — the player's 2nd goal of the match |
| ⚽⚽⚽ Hat-trick | *derived* — the player's 3rd |
| 🟡 Yellow card | `yellow_card` |
| 🔴 Red card | `red_card` |
| ⬆️ Subbed on | `substitution_in`, and the "on" half of a paired `substitution` |
| ⬇️ Subbed off | `substitution_out`, and the "off" half of a paired `substitution` |

A goal's *type* is its own button rather than a follow-up question, because a
penalty and an open-play goal are different moments deserving different
pictures. A picture staged for `RED` does not fire when that player scores.

**🅰️ Assist is read off the goal entry.** There is no assist entry in the
ordinary case — the scraper folds the assist into the goal it produced, as
`assister` / `assister_id` — so one timeline entry names two people and
`event_keys_for()` returns both, exactly as it does for a paired substitution.
(A bare `assist` entry does exist, for one the scraper could not pair with a
goal in the same minute; that goes through the normal path.)

Three consequences worth knowing:

- **Own goals never carry one.** Nobody is credited with an assist for an own
  goal, and the scraper pairs goals with assists by their position within the
  minute rather than by asserting a link — so an own goal sitting next to
  somebody else's assist would credit the wrong player on a public page. A
  penalty *can* carry one, since the feed naming a person there is a claim
  worth believing.
- **The scorer's photo and the assister's photo both post.** One entry, two
  people, two moments, two separate Instagram posts. That is the intent, but
  it means one goal can spend two of the day's publishing budget — see
  *Instagram's publishing budget*.
- **Scoring is not assisting.** A player who assists in the 23rd and scores in
  the 41st has two moments. `⚽ Goal` fires on the second, `🅰️ Assist` on the
  first, and neither picture goes anywhere near the other.

### One moment, one post

Enforced in both directions, inside `pending()` rather than left to the
caller.

**A picture never posts twice.** It matches the *earliest* moment it fits and
yields one entry, so a player who scores twice has his goal picture go up on
the first goal and the second is not a fresh occasion for it. Same for a
second booking — two yellows is a sending-off, not two chances at the same
photo. Three guards stack behind that, for the same reason the scorecards
have them: the returned entry's `posted_key`, `state.json`'s
`event_photos_posted` (survives a restart), and `bot.db` (within a run).

**A moment never carries two posts.** This one is only reachable because
names are matched tolerantly: tapping *Joao Felix* from the squad on one pass
and typing *felix* on another leaves two files for one player, and without a
guard both would post for the same goal. `_mark_duplicate_claims()` groups
resolved pictures by the moment they landed on; the exactly-spelled one wins
when there is exactly one — it came off the squad list, so it is the one that
was meant — and the loser is marked `duplicate` and reported instead of
posted. With nothing to choose between them, neither posts.

The guard is about one *moment*, not one player: ⚽ Goal, ⚽⚽ Brace and
⚽⚽⚽ Hat-trick for the same man are three moments and all three post.


### Braces and hat-tricks

Nothing in the feed says "hat-trick". It says goal, goal, goal — and the third
one *is* the moment. So these two are **derived** rather than read off a
timeline entry: `_timeline_moments()` walks the timeline in order keeping a
per-player tally and emits the milestone alongside the goal that completed it.

```
12'  goal          Ronaldo   ->  GOAL
48'  penalty_goal  Ronaldo   ->  PEN   +  BRACE       (2 goals)
55'  own_goal      Ronaldo   ->  OG                   <- does not count
77'  goal          Ronaldo   ->  GOAL  +  HAT_TRICK   (3 goals)
```

Details that matter:

- **Penalties count, own goals don't** (`MILESTONE_GOAL_TYPES`). Nobody has
  ever called two tap-ins and an own goal a hat-trick.
- **The milestone is emitted *alongside* the goal, not instead of it.** Stage
  ⚽ Goal, ⚽⚽ Brace and ⚽⚽⚽ Hat-trick for the same player and all three fire —
  on his 1st, 2nd and 3rd goals, in that order, as three separate posts.
- **A 4th goal doesn't fire it again.** The tally passes 3 once.
- **It fires the moment the third goes in**, not at full time. If he only gets
  two, the hat-trick photo never posts and is deleted with the rest.
- The caption is told it is a brace or a hat-trick and given every goal minute,
  so it leads with the achievement instead of reacting to the last goal as if
  it were an ordinary one.
- Staging is identical to any other event — `/event` → match → ⚽⚽⚽ Hat-trick →
  team → player → photo. Name matching, the ambiguity guard and the full-time
  report all apply unchanged.

### When VAR takes the goal away

The feed shows a goal, the photo posts, and then the goal is ruled out and
disappears from the timeline. The post is now on a public page celebrating
something that didn't happen.

```
23'  Messi scores                  ->  POSTED, IG ig-1
24'  gone from the timeline        ->  missing 1/3, waiting
25'  still gone                    ->  missing 2/3, waiting
26'  still gone                    ->  RETRACTED: ig-1 deleted
                                       photo stays on Cloudinary
70'  Messi scores again, it stands ->  POSTED again, IG ig-2
FT                                 ->  Cloudinary photo deleted
```

**Liveness is read from the timeline alone.** `event_photos.live_posted_keys()`
takes the timeline and the posted records and nothing else — in particular, not
the staged Cloudinary listing. That separation is the whole reason it exists as
its own function. An earlier version asked `pending()`, which needs the listing
as well, so "`pending()` didn't produce it" conflated *the event was withdrawn*
with *we could not read Cloudinary* — and one blipped listing at full time
deleted every correct post of the match. Retraction removes something public.
It may only ever fire on evidence that the event itself is gone.

Two refusals hold that line:

- **An unreadable timeline is not evidence.** `events` has to be a `list`. The
  feed publishes a sentence there before kickoff and can return one again on a
  bad scrape, and that must never read as "everything was disallowed".
- **An empty timeline is not evidence either** — unless something was posted
  from a timeline that had entries, which is exactly the VAR case where the
  only goal of the match is struck off. So an empty list still counts a miss,
  but the confirmation window has to run in full for it.

Matching a posted record back to the timeline uses the `player_id` recorded on
it when it posted, which is what makes this exact rather than another round of
spelling comparison.

**The disappearance is confirmed, not believed.** `_early_card_stale` already
records the house position on this: an event that only vanishes is *usually*
the scraper dropping it for a poll, and acting on that would delete a correct
post and then have to put it back. So the absence has to hold for
`EVENT_PHOTO_VANISHED_POLLS` polls in a row — three, about the length of a
VAR check. An event that reappears resets the counter and costs nothing.

**The picture stays on Cloudinary.** Only the Instagram post and the record
of it go. A goal ruled out is no evidence the player won't score one that
stands, and the photo staged for him has to still be there when he does —
which is why `unmark_event_posted()` exists: the row comes out of
`posted_events` so the same picture is free to go up again.

**A flapping feed is eventually left alone.** Posted and withdrawn
`MAX_EVENT_PHOTO_RETRACTIONS` times and the bot stops answering it, with an
alert. Putting the same photo up and taking it down all afternoon happens in
public.

**The final check reports; it does not delete.** `_clear_event_photos()` runs
one last pass with `final=True` as the worker exits, and anything still missing
becomes a 🙋 question rather than a deletion:

```
🙋 Real Madrid vs Barcelona: worth a look — the GOAL messi this post
   celebrates wasn't in the final timeline.

   https://instagram.com/p/…

   It may have been ruled out late, or the feed may simply have dropped it
   on the last read. The bot has NOT deleted anything.
```

This used to delete, and that was wrong twice over. The last scrape is the same
data the last poll already judged, so treating it as fresh evidence collapsed a
three-poll window down to a single miss. And at the whistle there are no
further polls to confirm with, so a delete there acts on one ambiguous reading
of an irreversible thing. A person can settle in five seconds what no amount of
polling will.

If the Instagram delete fails during a real retraction, you get the same 🙋
manual-deletion alert the early scorecards use, with a permalink.

### Where it sits in the poll

Ahead of the card triggers, deliberately. A goal in the 90th minute and the
final whistle can land in the same poll, and the picture of the moment belongs
on the page before the full-time card does.

A failed post is not marked as posted, so the next poll tries again — the same
retry the scorecards get. An alert goes out either way, and a successful one
sends the usual permalink and music reminder, since Instagram's API cannot
attach audio to anything the bot publishes.

### Instagram's publishing budget

The account may publish a fixed number of posts per rolling 24 hours, and the
next one after that is simply refused. Nothing counted them while the posts per
match were a fixed set — line-ups, half time, full time. Event photos make the
number unbounded: any player, any of nine moments, as many as somebody staged.
And they are evaluated *before* the card triggers in the poll loop, so without
a reservation the thing that runs out of budget is the full-time card — the one
post that always matters.

So `instagram.publishing_limit()` reads `content_publishing_limit` off the
Graph API, and `_instagram_budget_allows()` holds `INSTAGRAM_QUOTA_RESERVE = 5`
back for the cards: enough for a half-time card, a full-time card with its
stats slide, and slack for a correction repost.

- **An unreadable quota means "allowed".** `None` is "don't know", and every
  caller treats that as permission. The check exists to protect the cards from
  event photos, not to add a new way for everything to fail on a Graph blip.
- **A held-back photo is not marked as posted.** It stays staged, and if room
  frees up before the whistle it goes out on its own. The alert says so.
- **Posts are counted down inside the cache window.** The reading is shared by
  every match thread and refreshed every `QUOTA_CACHE_SECS`; `_note_post_spent()`
  decrements it locally so several photos going out in one poll can't all see
  the same stale number and all decide there was room.

### Opting in, and cleaning up

There is no flag in `matches.json` for this. The opt-in is the upload: a match
nobody staged anything for finds nothing and posts nothing. What that costs is
one Cloudinary listing every two minutes per live match — a listing, not a
lookup per event, because a timeline carries thirty-odd entries by full time
and the per-entry version would be thousands of requests a match to answer a
question that is almost always "none of them". The answer is cached between
polls; a Cloudinary failure returns the last known set rather than an empty
one, since "nothing is staged" would skip a picture sitting right there.

When the worker finishes, everything staged for that match is deleted —
the pictures that posted (Instagram holds its own copy; Cloudinary was only
the hand-off) and the ones staged for moments that never came. Best-effort: a
match that ends during a Cloudinary outage leaves its leftovers behind, keyed
to a `match_id` that will never be live again.

### Captions

`caption.generate_event_caption` writes them, through the same Gemini models
and the same hashtag line as everything else. The prompt differs in one way
that matters: the match is still being played, so the score it is given is the
score *right now* and it is told repeatedly not to write it as a result. The
picture carries no text either — unlike a scorecard, which has the score drawn
on it — so the caption has to say who this is and what they just did.

An 🅰️ Assist moment carries the scorer as well, and the prompt is told the post
belongs to the player who made the pass: credit the finish, but the headline is
the creator. A goal moment carries its assister the other way round, as it
always has.

---

## Manual match cards

Some fixtures are simply not on allfootball — a friendly, a lower division, an
old game worth reposting. `/card` in the Telegram bot builds one from details
you type in, renders it, and sends it to you; posting is a separate tap that
never happens on its own.

The steps are documented above in
[The Telegram bot, step by step](#the-telegram-bot-step-by-step). This section
is the reasoning behind them.

### Building a carousel

**➕ Add another card** is how a set of results becomes one post — the last
five matches of a team, a night's fixtures — instead of five separate ones.
Each card goes on a pile; **📤 Post** publishes the whole pile at once: one
card as an ordinary image post, two or more as a carousel, up to Instagram's
ceiling of ten. Carousel order is the order you added them, so it's yours to
choose.

The pile lives on Cloudinary, not in the bot's memory, because the bot shuts
down twenty minutes after your last message and a five-card batch is easily
twenty minutes of typing. So it survives a restart, and `/card` tells you when
a card is joining one already in progress.

| | |
|---|---|
| `/batch` | What's waiting, in order, with **Post**, **Add another** and **Clear**. Posting from here asks for the theme too |
| `🗑 Discard this card` | Drops the card just built. The pile is untouched |
| `/cancel` | Ends the conversation only — saved cards are **not** binned, since that would silently throw away several matches' worth of typing. `/batch` → Clear is the deliberate way |

### Captions, and the theme

A single card gets the ordinary match caption. A carousel gets
`caption.generate_group_caption()` — the same writer the scraped
`carousel_group` posts use, which is explicitly forbidden from stating a
scoreline because the cards already carry every result.

Before a carousel goes out, one last question:

```
Last thing — what is this post?

One line, in your words: Arsenal's last five, Every North London
derby since 2020, Matchweek 3.

The caption writer sees 3 results and nothing else, so without this
it writes a matchday round-up. Skip and you'll get exactly that.

[Skip — just post it]
```

That's the **theme**, and it's asked at post time and nowhere earlier because
it's the one thing you can only answer once the set is complete. A pile of
results tells the model that some matches happened; it can't tell it that these
are one team's last five. The theme is stated twice in the prompt — once
framing the task, once beside the results, since a long prompt otherwise buries
the framing — and it drives the deterministic fallback too:

| | Fallback opening |
|---|---|
| With a theme | `Arsenal's last five.` / `All 5 of them.` |
| Without | `Every result from today, all 5 of them.` |

`theme` is optional on `generate_group_caption`, and that's the point: the
scraped `carousel_group` flow passes nothing and is completely unchanged by
this. A matchday post has its framing by definition, and no human to ask.
Single-card posts are never asked either — the ordinary match caption already
knows what the match was.

### What's different about the output

Exactly one thing: the **date in the top-right corner**, in the same face and
colour as the FULL TIME headline, mirroring the brand mark on the left. Live
fixtures post within minutes of the whistle, where a date is noise; a match
typed in weeks later needs to say when it was played.

That is carried by `matchSample['card_date']`, which only
`manual_match.build_scraper_data()` sets. Both renderers draw it if and only if
it is present, which is how scraped fixtures stay free of one. Everything else
— crests, competition logo, scorer symbols, caption, hashtags — runs through
the ordinary pipeline unchanged and unaware. That is deliberate: if a manual
card looks wrong, the automated one is wrong the same way.

### Waking the bot

The bot only exists while an Actions job holds it, and the watchdog normally
stops it as soon as the last match worker finishes — which is precisely when
`/card` gets used. Two changes make it reachable anyway:

- **The dispatcher listens.** On a tick with no workers running, it peeks at
  the bot's Telegram update queue and starts `telegram_bot.yml` if anything is
  waiting. So: message the bot, wait for the next tick (≤ 15 min), and it wakes
  up and answers. The peek passes no `offset`, which is what makes it
  non-destructive — Telegram only discards updates when `getUpdates` is called
  with an offset above their id, so the message is still there for the bot. It
  only runs when no bot is running: two pollers on one token fight over the
  same queue. Any pending message counts, not just `/card` — a photo sent to a
  stopped bot deserves an answer too.
- **The watchdog waits.** `telegram_bot.py` touches `.bot_session` on every
  update it handles; the watchdog keeps the bot alive while that file is less
  than `SESSION_IDLE` (20 min) old, so it can't shut down mid-scoreline.

Running `python telegram_bot.py` locally works the same way and skips all of
this — the local `.env` already has every credential `/card` needs.

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

**🙋** started as one thing — a superseded post the bot tried and failed to
delete (see *Deleting a superseded post*) — and is now the mark for anything
only a person can settle. In full:

| 🙋 says | What it wants from you |
|---|---|
| a superseded post the delete API refused | delete it in the app; the permalink is in the message |
| "who is this photo of?", when the team sheets can't settle a staged name | tap the player, or tap *leave it as typed* |
| nobody in either squad resembles a staged name | `/staged` removes it, `/event` stages it again — or ignore it if the sheet was just incomplete |
| a staged name that fits two players who both did the thing | nothing posted, and nothing will. Pick from the squad list next time |
| two staged pictures claiming one moment | nothing is broken: one posted, the other never will |
| a photo held back because Instagram's 24-hour budget is nearly spent | nothing, unless you want to free up a post — it stays staged either way |
| a photo posted and withdrawn twice by a flapping feed | nothing is on the page; it won't try again |
| a post whose moment wasn't in the final timeline | look at the match. The bot deleted nothing |
| staged photos that never fired because the name missed | next time, tap the name from the list |

Only the second row is a question. Everything else is told to you because
silence is the one outcome this feature must not have — a photo that quietly
never posts and never says why is the failure the whole design is built
against.

The delete failure should be rare — if you are seeing it regularly, the delete
API has regressed again.

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
| `TELEGRAM_BOT_TOKEN` | Photo bot + alerts + the dispatcher's wake check |
| `TELEGRAM_ALLOWED_USER_IDS` | Who may talk to the bot (also default alert target) |
| `TELEGRAM_ALERT_CHAT_ID` | Optional explicit alert chat |
| `FB_APP_ID` / `FB_APP_SECRET` | **Local only** — used by `setup_token.py` |

Every secret except the Facebook app pair is now needed by `match_bot.yml`,
`dispatcher.yml` **and** `telegram_bot.yml` — the dispatcher publishes carousel
groups, and the bot's `/card` flow renders and posts a whole card on its own.
`dispatcher.yml` installs the full `requirements.txt` for the same reason, and
`match_bot.yml` grants `actions: write` so a worker can nudge it.

If the Instagram or Gemini secrets are missing from `telegram_bot.yml`, the bot
still starts and still takes photos: the posting stack is imported inside the
handler that needs it, so only `/card`'s final step fails, and it says so in
the chat.

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
  "post_lineups": true,
  "lineups_first": "home",
  "coaches": { "home": "A. Slot", "away": "D. Farke" },
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
| `post_lineups` | Post the starting XIs as a two-slide carousel before kickoff. Default false. |
| `lineups_first` | `"home"` (default) or `"away"` — which team's XI is slide one. Only meaningful with `post_lineups`. |
| `coaches` | `{"home": …, "away": …}` — the managers, printed on the line-up cards. Optional, and the only line there no feed provides. |
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
- **A crashed worker is not restarted.** The dispatcher only fires inside a
  fixture's 15-minute pre-kickoff window, so once that has passed nothing will
  pick the match back up. The crash alert says so, and names what had already
  been posted — a manually re-run worker starts with no memory of the first
  attempt, so anything already on the page would go up again.
- **Staged event photos share Instagram's publishing budget.** The Graph API
  allows 25 published posts per account per rolling 24 hours — a match with six
  pictures staged spends six of them on top of its line-ups, its HT card and
  its FT card. The last `INSTAGRAM_QUOTA_RESERVE` posts are now held back for
  the cards, so it is the event photos that get skipped rather than the
  full-time card, and a skipped one is alerted and stays staged in case room
  frees up. That protects the post that always matters; it does not create
  budget. Stage what is worth a post.
- **A staged picture posts on what the feed reported at the time.** A goal
  later disallowed by VAR does post — and is then taken down again once the
  event has been absent from the timeline for three polls, with the picture
  kept on Cloudinary in case a valid goal follows. So the post is public for
  a few minutes before it is retracted; that window is the cost of not
  deleting correct posts over a scraper blip. It is also blind to *which*
  goal: a player who scores twice has one ⚽ Goal picture and it goes up on
  the first (⚽⚽ Brace and ⚽⚽⚽ Hat-trick cover the others).
- **A goal withdrawn right at the whistle is a question, not a takedown.**
  There are no polls left to confirm with, so the final check alerts and leaves
  the post up rather than deleting on one ambiguous reading. Deleting it is
  yours to do, and the alert carries the permalink.
- **The `bot.db` idempotency guard is per-run, not per-match.** It is gitignored
  and every Actions run checks out fresh, so `posted_events` only ever
  deduplicates within one process. Dispatcher-level dedup is what actually
  prevents double posts; the database is an audit trail and a within-run guard.

---

## Local development

```bash
python -m venv venv && venv/bin/pip install -r requirements.txt
# create .env with the variables listed above
venv/bin/python validate_matches.py --check
venv/bin/python main.py          # run the full bot loop locally
```

### Tests and CI

`.github/workflows/ci.yml` runs on every push to master and on pull requests.
Production has no staging step — code reaches a live match by being pushed —
so this is the only gate between a typo and a fixture that fails to post.

```bash
venv/bin/pip install -r requirements-dev.txt
venv/bin/pytest -q                 # the whole suite, no credentials needed
venv/bin/ruff check --select=E9,F63,F7,F82,F601,F811 --exclude=venv .
```

Three things run in CI:

| Step | Catches |
|---|---|
| `ruff` on the error-level rules | undefined names, broken f-strings, syntax errors. Deliberately *not* the full ruleset: this codebase catches exceptions broadly on purpose, and 100-plus style findings would make a red tick meaningless. |
| `py_compile` on the entry-point scripts | `match_worker_runner.py`, `telegram_bot.py` and friends do work in their module body, so they can't be imported — only parsed. |
| `pytest` | the phase and early-posting logic, dispatcher dedup and pruning, scraper event classification, the manual-match parsers (`tests/test_manual_match.py` — the one place data arrives typed rather than scraped), the badge check that stands in for `validate_matches.py` on a typed-in name, the pending-card manifest, the carousel theme (including that the scraped group flow is unaffected by it), the goals-against-score check, and that every other module imports. |

The suite covers pure functions only, so it needs no credentials and touches no
network. `conftest.py` puts placeholder values in the environment before the
first import, because `caption.py` builds a Gemini client in its module body and
`dispatcher.py` reads `GH_TOKEN` at import — both would otherwise fail to import
without secrets.

Richer registry checks — crest coverage, URL shapes, carousel sizes — stay in
`validate_matches.py`, which needs Cloudinary credentials and remains a local
tool. CI only asserts that `matches.json` is structurally sound.

### Rehearsing event photos before trusting them live

The suite covers the decisions. It cannot cover Instagram accepting the
picture, Cloudinary handing it over, or the two Telegram processes agreeing on
what a button means. So the first real exercise of this feature should be **one
match against a throwaway Instagram account**, not the page.

What to swap — nothing else changes:

| | |
|---|---|
| `IG_USER_ID`, `IG_ACCESS_TOKEN` in `.env` | the throwaway account's — see *Auth* above for minting them |
| `TELEGRAM_ALERT_CHAT_ID` | leave it. The alerts are half of what is being tested |
| Cloudinary | leave it. Staged pictures live under `event_photos/<match_id>_…` and are deleted at full time either way |

Then, with a fixture that kicks off shortly already in `matches.json`:

1. **Before the team sheets are out**, stage three pictures, deliberately
   spelled three ways: one exactly as the scoreboard will print it, one
   shortened (`felix` for Joao Felix), and one that is a different word
   (`rodri` for Rodrigo). Those are the three branches of `clarify()`.
2. `/staged` — all three listed, all three marked `(by name)`.
3. **When the sheets land** (~an hour before kickoff), the first two should be
   pinned silently and the third should arrive as a 🙋 question with buttons.
   Answer it. `/staged` again: nothing should say `(by name)` any more.
4. Stage a fourth for a moment that certainly won't happen, then `/staged` → ❌
   to take it back down. That is the abort path, and it is the one thing with
   no other way out.
5. **Let the match run.** Watch for the post itself, its caption, the 🎵 music
   reminder — and, if anything is disallowed, the retraction sequence in the
   worker output (`missing 1/3`, `2/3`, then the delete).
6. **At full time**, check the staged folder is empty and that the 🙋 report
   named anything that never fired.

`venv/bin/python main.py` runs the worker locally against the same registry, so
none of this needs a push. Run it somewhere you can read the output: the worker
prints every decision it makes about a staged picture.

Useful one-offs:

```bash
# render a manual card locally, without the bot or a post
venv/bin/python - <<'PY'
import manual_match as mm
from scorecard import generate_scorecard
data = mm.build_scraper_data(
    home_team='Arsenal', away_team='Man City',
    home_score='3', away_score='1', competition='Premier League',
    when=mm.parse_date('21/08/2026'), event_type='FT',
    home_events=mm.parse_scorers("23 Saka\n45+2 Havertz (pen)", 'Arsenal'),
    away_events=mm.parse_scorers('67 Rice (og)', 'Man City'))
print(generate_scorecard(data, event_type='FT', competition='Premier League'))
PY

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
