# manual_match.py — hand-entered matches, shaped like scraper output.
"""
Build a match the scraper never saw.

Some fixtures are simply not on allfootball — a friendly, a lower division, an
old game worth reposting. This module turns a handful of typed-in details into
the exact `scraper_data` dict that `football_scraper_dom.get_match_data()`
returns, so every downstream piece — the template scorecard, the photo overlay,
the caption writer, the hashtag builder — runs unchanged and unaware.

That "unaware" is the whole point. Nothing here is a second rendering path: if
a manual card looks wrong, the automated one is wrong the same way.

The one field a scraped match never carries is `matchSample['card_date']`, the
pre-formatted date stamped in the card's top-right corner. Live fixtures post
within minutes of the whistle, so a date there is noise; a match typed in weeks
later needs it. Both renderers draw it only when it is present, which is exactly
the matches that came through here.

Entered through the Telegram bot's /card flow — see telegram_bot.py.
"""

import re
from datetime import datetime, timezone

# ── Scorer line grammar ───────────────────────────────────────────────────────
# One event per line, as it would be said out loud:
#
#     23 Saka                 → goal
#     45+2 Havertz (pen)      → penalty, scored in first-half stoppage time
#     67 Rice (og)            → own goal
#     88 Odegaard (red)       → red card
#     90 Martinelli (miss)    → penalty missed
#
# The trailing apostrophe people naturally type ("23' Saka") is optional, and
# the suffix is matched case-insensitively.
SCORER_LINE = re.compile(
    r"""^\s*
        (?P<minute>\d{1,3})              # 23
        (?:\s*\+\s*(?P<extra>\d{1,2}))?  # +2
        \s*'?                            # optional apostrophe
        [\s.:-]+                         # separator before the name
        (?P<player>.+?)                  # Saka
        (?:\s*\(\s*(?P<kind>[^)]+?)\s*\))?   # (pen)
        \s*$""",
    re.VERBOSE,
)

# Every spelling of an event type that a person might reasonably type.
EVENT_ALIASES = {
    'goal': 'goal', 'g': 'goal',
    'pen': 'penalty_goal', 'p': 'penalty_goal', 'penalty': 'penalty_goal',
    'penalty goal': 'penalty_goal', 'pk': 'penalty_goal',
    'og': 'own_goal', 'own': 'own_goal', 'own goal': 'own_goal',
    'red': 'red_card', 'r': 'red_card', 'rc': 'red_card',
    'red card': 'red_card', 'sent off': 'red_card',
    'miss': 'penalty_missed', 'missed': 'penalty_missed',
    'pm': 'penalty_missed', 'penalty missed': 'penalty_missed',
    'missed penalty': 'penalty_missed',
}

# What counts towards the scoreline, for the sanity check on the review screen.
GOAL_TYPES = ('goal', 'penalty_goal', 'own_goal')

# Anything a person is likely to type for a date, most specific first.
# Day-before-month throughout: this is a European football page, and 03/08
# meaning "3 August" is the reading its user intends.
DATE_FORMATS = (
    '%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y',
    '%d %b %Y', '%d %B %Y', '%b %d %Y', '%B %d %Y',
    '%d/%m/%y', '%d-%m-%y', '%d.%m.%y',
)

SCORE_LINE = re.compile(r'^\s*(\d{1,2})\s*[-–—:x]\s*(\d{1,2})\s*$')

# ── Limits ────────────────────────────────────────────────────────────────────
# Every one of these rejects rather than truncates. Silently trimming a team
# name to fit would put a wrong name on a public post; saying "that is too
# long" costs one message and gets the right one.

# A team or competition name has to fit its box on the card, and is looked up
# against the crest and logo tables. Real names are far shorter than this —
# the cap is here to catch a paragraph pasted into the wrong step.
MAX_TEAM_NAME = 40
MAX_COMPETITION = 60
MAX_PLAYER_NAME = 40

# 90 plus extra time plus a generous margin. Anything beyond is a typo, and a
# card is the wrong place to discover one.
MAX_MINUTE = 130
MAX_STOPPAGE = 30

# Football results only go back so far, and a result cannot be entered for a
# match that has not been played. Two days of slack absorbs every timezone.
MIN_YEAR = 1900
FUTURE_GRACE_DAYS = 2

# Words that mean "there weren't any", so an empty column doesn't need an
# empty message — which Telegram will not send anyway.
NOTHING = {'-', '–', 'none', 'no', 'nil', 'n/a', 'na', 'skip', 'nothing', '0'}


class ParseError(ValueError):
    """A typed-in field that could not be read. The message is shown verbatim
    in the chat, so it says what to type instead — never just 'invalid'."""


def _has_letter(text: str) -> bool:
    """Any alphabetic character, in any script. `str.isalpha` per-character so
    Зенит and 上海海港 pass and '2-1' does not."""
    return any(ch.isalpha() for ch in text)


def _looks_like_another_answer(text: str) -> str | None:
    """Which *other* question this text answers, if it obviously answers one.

    Ten questions in a row is exactly the shape of interaction where an answer
    lands one step early or late. A scoreline typed into "home team" would
    otherwise become a team called "2-1", render, and post.
    """
    if SCORE_LINE.match(text):
        return 'a score'
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(text.strip().replace(',', ''), fmt)
            return 'a date'
        except ValueError:
            continue
    if SCORER_LINE.match(text) and not _has_letter(text.split()[0]):
        return 'a scorer line'
    return None


def _parse_name(text: str, what: str, limit: int, example: str) -> str:
    """Shared validation for the free-text names: team and competition.

    They are the only fields with no grammar of their own, which makes them the
    only ones that would accept an answer meant for a different question.
    """
    name = ' '.join((text or '').split())   # collapse newlines and runs of spaces

    if not name:
        raise ParseError(f"That was empty. I need {what} — {example}.")

    # Before the letters check, not after: "2-1" fails both, and "that looks
    # like a score, which I ask for later" is the half that tells you what to
    # do about it.
    mistaken = _looks_like_another_answer(name)
    if mistaken:
        raise ParseError(
            f"“{name}” looks like {mistaken}, not {what} — I think that answer "
            f"is meant for a later step.\n\n"
            f"Right now I need {what}, like {example}. "
            f"The score and date come after this."
        )

    if not _has_letter(name):
        raise ParseError(
            f"“{name}” has no letters in it, so it can't be {what}.\n\n"
            f"I'm expecting something like {example}."
        )

    if len(name) > limit:
        raise ParseError(
            f"That's {len(name)} characters and the limit is {limit} — it "
            f"wouldn't fit on the card.\n\n"
            f"Send just {what}, like {example}."
        )
    return name


def parse_team(text: str) -> str:
    """A team name as it should appear on the card and be looked up for a crest."""
    return _parse_name(text, 'the team name', MAX_TEAM_NAME, '`Arsenal` or `Real Betis`')


def parse_competition(text: str) -> str:
    """A competition name, as it should read on the card."""
    return _parse_name(text, 'the competition', MAX_COMPETITION,
                       '`Premier League` or `Club Friendly`')


# ── Field parsers ─────────────────────────────────────────────────────────────

def parse_score(text: str) -> tuple[str, str]:
    """'2-1' → ('2', '1'). Also accepts 2:1, 2 – 1, 2x1."""
    m = SCORE_LINE.match(text or '')
    if not m:
        raise ParseError(
            f"I couldn't read “{(text or '').strip()}” as a score.\n\n"
            f"Send it as two numbers with a dash between them — 2-1, or 0-0."
        )
    return m.group(1), m.group(2)


def parse_date(text: str, now: datetime | None = None) -> datetime:
    """A typed date → a UTC datetime at midnight.

    Two-digit years are read by strptime as 20xx for 00–68, which covers every
    year this will ever be used for.

    The range check is the half that matters. `21/08/2062` parses perfectly —
    a transposed year is invisible to the grammar and would be stamped on the
    card exactly as typed.
    """
    raw = (text or '').strip().replace(',', '')
    when = None
    for fmt in DATE_FORMATS:
        try:
            when = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue

    if when is None:
        raise ParseError(
            f"I couldn't read “{raw}” as a date.\n\n"
            f"Use one of these shapes:\n"
            f"• `21/08/2026` — day/month/year\n"
            f"• `2026-08-21` — year-month-day\n"
            f"• `21 Aug 2026` or `21 August 2026`\n\n"
            f"Day comes before month, so `03/08/2026` is 3 August."
        )

    now = now or datetime.now(timezone.utc)
    if when.year < MIN_YEAR:
        raise ParseError(
            f"{when:%d %b %Y} is before {MIN_YEAR} — that looks like a typo in "
            f"the year.\n\nSend the date again, like `21/08/2026`."
        )
    if (when - now).days > FUTURE_GRACE_DAYS:
        raise ParseError(
            f"{when:%d %b %Y} is in the future, and a result can't be entered "
            f"for a match that hasn't been played — most likely the year is a "
            f"typo.\n\nSend the date again, like `21/08/2026`."
        )
    return when


def format_card_date(when: datetime) -> str:
    """The string stamped on the card: '21 AUG 2026'.

    Upper case because both cards are set in Bebas Neue, which has no
    lower case — lower-case input would render as capitals anyway, at the
    smaller x-height metrics, which looks like a mistake.
    """
    return f"{when.day:02d} {when.strftime('%b').upper()} {when.year}"


def parse_scorers(text: str, team: str) -> list[dict]:
    """Parse a block of scorer lines for one team into scraper-shaped events.

    Every line must parse. A half-read list is worse than a rejected one: the
    card would go out looking complete with a goal quietly missing from it.
    """
    if not text or text.strip().lower() in NOTHING:
        return []

    events, bad = [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        m = SCORER_LINE.match(line)
        if not m:
            bad.append(line.strip())
            continue

        kind_raw = (m.group('kind') or 'goal').strip().lower()
        kind = EVENT_ALIASES.get(kind_raw)
        if kind is None:
            bad.append(f"{line.strip()}  ← “{kind_raw}” isn't a type I know")
            continue

        # The grammar accepts any 1–3 digit minute, which is how '900 Saka'
        # gets through it. A minute is a fact about the match, so an impossible
        # one is a typo, not a style choice.
        minute = int(m.group('minute'))
        if not 1 <= minute <= MAX_MINUTE:
            bad.append(f"{line.strip()}  ← there's no {minute}th minute")
            continue
        extra = int(m.group('extra') or 0)
        if extra > MAX_STOPPAGE:
            bad.append(f"{line.strip()}  ← +{extra} is too much stoppage time")
            continue

        player = m.group('player').strip()
        if not _has_letter(player):
            bad.append(f"{line.strip()}  ← “{player}” isn't a name")
            continue
        if len(player) > MAX_PLAYER_NAME:
            bad.append(f"{line.strip()}  ← that name is too long "
                       f"({len(player)}/{MAX_PLAYER_NAME} characters)")
            continue

        events.append({
            'type':   kind,
            'team':   team,
            'player': player,
            # The renderers call allfootball_desktop.format_minute() on this
            # pair, so store it the way the scraper does rather than folding
            # the stoppage-time offset in here.
            'minute': f"{minute}'",
            'minute_extra': m.group('extra') or '',
        })

    if bad:
        raise ParseError(
            "I couldn't read these lines:\n\n"
            + '\n'.join(f"• {b}" for b in bad)
            + "\n\nThe format is one event per line: minute, name, then an "
              "optional type in brackets.\n\n"
              "23 Saka\n45+2 Havertz (pen)\n67 Rice (og)\n88 Odegaard (red)\n"
              "90 Martinelli (miss)\n\n"
              "No brackets means a goal. The only types are pen, og, red and "
              f"miss. Minutes run 1–{MAX_MINUTE}.\n\n"
              "Send “none” if there aren't any."
        )
    return events


# ── Assembly ──────────────────────────────────────────────────────────────────

def _minute_key(event: dict) -> tuple[int, int]:
    """Sort key: minute, then stoppage-time offset."""
    m = re.match(r'\s*(\d+)', str(event.get('minute') or ''))
    return (int(m.group(1)) if m else 0,
            int(event.get('minute_extra') or 0))


def new_match_id(when: datetime, home: str, away: str) -> str:
    """A stable id for a hand-entered match.

    Deliberately not numeric: a scraped id is an allfootball match number, and
    anything reading one of these should be able to tell at a glance that no
    such match exists upstream. The date and initials keep two manual cards
    from colliding in `output/` or on Cloudinary, and make the same match
    re-entered later land on the same template.
    """
    def initials(name: str) -> str:
        letters = re.sub(r'[^A-Za-z]', '', name).upper()
        return (letters[:3] or 'XXX')
    return f"manual-{when:%Y%m%d}-{initials(home)}{initials(away)}"


def goal_count(events: list[dict], team: str) -> int:
    """Goals in this event list that credit `team` — for the typo check."""
    return sum(1 for e in events
               if e.get('team') == team and e.get('type') in GOAL_TYPES)


def build_scraper_data(*, home_team: str, away_team: str,
                       home_score: str, away_score: str,
                       competition: str, when: datetime, event_type: str,
                       home_events: list[dict], away_events: list[dict],
                       venue: str = '', match_id: str | None = None) -> dict:
    """Assemble the `scraper_data` dict the rest of the pipeline expects.

    `event_type` decides which score fields are filled, mirroring the scraper:
    at half time only hts_*, at full time fs_* (with hts_* left unknown,
    because a hand-entered match rarely comes with a half-time score and a
    guessed one would be shown as fact).
    """
    event_type = event_type.upper()
    sample = {
        'match_id':         match_id or new_match_id(when, home_team, away_team),
        'team_A_name':      home_team,
        'team_B_name':      away_team,
        'competition_name': competition,
        'date_utc':         when.strftime('%Y-%m-%d'),
        'status':           'Half Time' if event_type == 'HT' else 'Finished',
        # Only manual matches carry this; it is what puts the date on the card.
        'card_date':        format_card_date(when),
    }
    if event_type == 'HT':
        sample['hts_A'], sample['hts_B'] = home_score, away_score
    else:
        sample['fs_A'], sample['fs_B'] = home_score, away_score

    return {
        'matchSample':    sample,
        'events':         sorted(home_events + away_events, key=_minute_key),
        'matchFormation': {'venue_name': venue},
        # Present and empty, the way the scraper leaves them when a match has
        # no published stats — every consumer already handles that case.
        'statistics':     {},
        'matchAnalysis':  {},
    }
