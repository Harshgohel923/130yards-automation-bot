# event_photos.py — pictures staged against a player's in-match moment.
"""
The vocabulary shared by the three sides of the in-match photo feature.

The idea is one sentence long: before a match, someone uploads a picture and
says who it is for and what has to happen — "Messi, goal" — and if that
moment arrives during the game, the picture posts on its own. If it never
arrives, nothing happens and the picture is deleted when the worker finishes.

  telegram_bot.py   stages a picture at  event_photos/<match_id>_<KEY>_<slug>
  main.py           checks the timeline every poll, posts what matches,
                    and takes back down anything the feed withdraws
  caption.py        writes the caption for the moment that fired

Nothing is shared between them except the picture itself: a Cloudinary
public_id derived from the match, the event and the player's name — the same
three things the uploader was asked for — and the context metadata written
onto that object. That is deliberate: the bot restarts often and the worker
runs in a different process entirely, so a name both can compute, and a
property both can read off the asset, are worth more than any amount of state
passed between them.

Names name it; ids find it
──────────────────────────
The public_id can't carry the feed's numeric id for a player, because it is
fixed at upload and the id often isn't knowable yet. So the id rides on the
asset as context metadata — returned by the same Admin API listing that finds
the picture — and once it is there, that is what matching uses. See
_hits_for(). Names are the fallback for a picture nothing has pinned yet.

Why the player is part of the key
─────────────────────────────────
A match has many goals and they are not interchangeable. Messi scoring against
Brazil and Álvarez scoring against Brazil are two moments wanting two pictures,
and Neymar's red card in the same game is a third. Keying on the fixture alone
would make them one.

Names, ids, and the four places they can disagree
────────────────────────────────────────────────
One end of this is a person typing a name; the other is a feed printing a team
sheet. They disagree constantly — "Messi" vs "Lionel Messi", "felix" vs "Joao
Felix", "Rodri" vs "Rodrigo", an accent typed or not — and every disagreement
that isn't reconciled is a photo that never posts and never says why.

Four defences, in the order they get a chance. The first two end the argument
by replacing the name with an id; the last two are what is left when neither
could.

  1. suggest(), in the bot, while the person is still there. Loose on purpose
     — prefixes, typos, initials — because a wrong guess costs one tap. This
     is what turns "Rodri" into Rodrigo, and it is the only place that can:
     nothing downstream may guess between two words that merely start alike.
     A name tapped from the squad also brings its id, and that is the end of
     the matter for that picture.

  2. clarify(), in the worker, the moment both team sheets are published.
     Every name in the match is knowable and there is still an hour before
     kickoff — the only point where both are true. A name beyond doubt is
     pinned silently; anything else becomes a question with buttons.

  3. names_match(), in the worker, unattended, for whatever is still going on
     spelling. Strict on purpose: exact, or one name's words wholly inside the
     other's. A miss costs a photo; a false positive puts the wrong player on
     a public page with no undo. A staged name that fits two players who both
     did the thing is reported, not resolved — see pending()'s 'conflict'.

  4. unfired(), at full time. Whatever slipped through all three, named out
     loud while the timeline is still in hand: "you staged Rodri, the
     scoreboard says Rodrigo". Silence is the one outcome this feature must
     not have.

And one way out at any point: /staged in the bot lists what is armed and
takes one back down. delete_one() is what it calls.
"""

import difflib
import hashlib
import os
import re
import unicodedata

import cloudinary
import cloudinary.api
import cloudinary.uploader
from dotenv import load_dotenv

from config import CLOUD_NAME

load_dotenv()

# Configured here as well as in cloudinary_upload/cloudinary_utils: the
# library keeps one global config, and this module is imported by processes
# (the bot) that may not have imported either of the others.
cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET'),
)

FOLDER = 'event_photos'

# Delivery transformation applied when the picture is handed to Instagram.
# Graph API wants a JPEG it can fetch under 8 MB; whatever was uploaded — a
# PNG sent as a document, a 9 MB photo the bot let through — becomes one here
# rather than failing the post with a message nobody would connect to it.
DELIVERY_TRANSFORM = 'f_jpg,q_auto:good'

# ── The event vocabulary ─────────────────────────────────────────────────────
# (key, label, the scraper event types it fires on).
#
# The keys are ours and appear in Cloudinary public_ids, so they never change
# once a picture has been staged under one. The scraper's type strings are its
# own — see football_scraper_dom.parse_event_type, which derives them from an
# opaque substring of the event icon's filename.
#
# A goal's *type* is a separate choice rather than a detail of one: a penalty
# and an open-play goal are different moments and deserve different pictures.
EVENT_CHOICES: list[tuple[str, str, tuple[str, ...]]] = [
    ('GOAL',      '⚽ Goal',          ('goal',)),
    ('PEN',       '🅿️ Penalty goal',  ('penalty_goal',)),
    ('OG',        '🥅 Own goal',      ('own_goal',)),
    # Derived, not read off a timeline entry — see GOAL_MILESTONES.
    ('BRACE',     '⚽⚽ Brace',        ()),
    ('HAT_TRICK', '⚽⚽⚽ Hat-trick',   ()),
    ('YELLOW',    '🟡 Yellow card',   ('yellow_card',)),
    ('RED',       '🔴 Red card',      ('red_card',)),
    ('SUB_IN',    '⬆️ Subbed on',     ('substitution_in',)),
    ('SUB_OUT',   '⬇️ Subbed off',    ('substitution_out',)),
]

# Moments that are a count rather than an entry. Nothing in the feed says
# "hat-trick" — it says goal, goal, goal, and the third one is the moment. So
# these are derived by walking the timeline in order and counting, which is
# also why they can't live in TYPE_TO_KEY with the rest.
#
#   {goals by one player in this match: the key that fires on that goal}
GOAL_MILESTONES = {2: 'BRACE', 3: 'HAT_TRICK'}

# What counts towards one. A penalty is a goal he scored; an own goal is a goal
# he scored past his own keeper, and nobody has ever called two of those and a
# tap-in a hat-trick.
MILESTONE_GOAL_TYPES = ('goal', 'penalty_goal')

# How the buttons are laid out in the bot. Grouped by kind rather than chunked
# two at a time, because "🥅 Own goal | ⚽⚽ Brace" sitting on one row reads as
# though they were a pair. Every key in EVENT_CHOICES appears here exactly once
# — pinned by a test, since a key missing from this is a moment you can never
# stage a picture for.
EVENT_KEYBOARD_ROWS: tuple[tuple[str, ...], ...] = (
    ('GOAL', 'PEN', 'OG'),
    ('BRACE', 'HAT_TRICK'),
    ('YELLOW', 'RED'),
    ('SUB_IN', 'SUB_OUT'),
)

EVENT_KEYS = tuple(key for key, _label, _types in EVENT_CHOICES)
EVENT_LABELS = {key: label for key, label, _types in EVENT_CHOICES}

# scraper event type → our key. Built from EVENT_CHOICES so the two can't drift.
TYPE_TO_KEY = {
    scraper_type: key
    for key, _label, types in EVENT_CHOICES
    for scraper_type in types
}

# How the caption prompt refers to each moment.
EVENT_DESCRIPTION = {
    'GOAL':      'scored a goal',
    'PEN':       'scored from the penalty spot',
    'OG':        'scored an own goal',
    'BRACE':     'scored his second of the match',
    'HAT_TRICK': 'completed a hat-trick',
    'YELLOW':    'was booked',
    'RED':       'was sent off',
    'SUB_IN':    'came off the bench',
    'SUB_OUT':   'was substituted off',
}


def player_id_of(ev: dict, role: str = 'player') -> str:
    """The feed's numeric id for the person in a timeline entry, as a string.

    `role` is 'player', 'player_in' or 'player_out' — the paired
    substitution entry names two people and they have separate ids.

    This is the source of truth for who a moment is about. Names are a
    fallback for a picture staged before the team news made an id knowable.
    """
    key = 'player_id' if role == 'player' else f'{role}_id'
    return str(ev.get(key) or '').strip()


def player_slug(name: str) -> str:
    """'Gerónimo Rulli' → 'geronimo-rulli'.

    Everything that varies between the feed's spelling and a person's typing
    is folded away: case, accents, dots in initials, stray whitespace. What
    survives is the part both are certain to agree on.
    """
    folded = unicodedata.normalize('NFKD', str(name or ''))
    folded = ''.join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '-', folded.lower()).strip('-')


def public_id(match_id: str, event_key: str, player: str) -> str:
    """event_photos/54483541_GOAL_messi — where a staged picture lives."""
    return f"{FOLDER}/{match_id}_{event_key}_{player_slug(player)}"


def parse_public_id(pid: str, match_id: str) -> tuple[str, str] | None:
    """(event_key, player_slug) back out of a public_id, or None if it isn't one.

    The reverse of public_id(). Needed because a staged picture is no longer
    found by exact name — see names_match — so the stored slug has to be read
    back and compared rather than just tested for membership.

    Keys are matched against the known vocabulary rather than split on the
    underscore, since SUB_IN and SUB_OUT contain one themselves.
    """
    prefix = f"{FOLDER}/{match_id}_"
    if not pid.startswith(prefix):
        return None
    rest = pid[len(prefix):]
    for key in EVENT_KEYS:
        if rest.startswith(f"{key}_"):
            slug = rest[len(key) + 1:]
            return (key, slug) if slug else None
    return None


def names_match(staged: str, actual: str) -> bool:
    """Do a staged slug and the feed's slug refer to the same player?

    Exact, or one name's words wholly contained in the other's — which is the
    common shape of the disagreement, because a person types the name they say
    out loud and the feed prints the one on the team sheet:

        felix   ⊂  joao-felix          ✓
        messi   ⊂  lionel-messi        ✓
        rodri   vs rodrigo             ✗  — a different word, not a subset

    That last one is deliberate. Loosening this to prefixes would make "rodri"
    match Rodrigo *and* Rodríguez, and firing the wrong player's picture is a
    worse outcome than firing none — the page is public and there is no undo.
    Near-misses like it are caught where a human is present to settle them:
    suggest() at staging time, and the report at full time for anything that
    slipped through both.
    """
    if staged == actual:
        return True
    if not staged or not actual:
        return False
    a, b = set(staged.split('-')), set(actual.split('-'))
    return a < b or b < a


def suggest(typed: str, names: list[str], limit: int = 5) -> list[str]:
    """Squad names a typed name might have meant, best first.

    Deliberately looser than names_match: this runs in the bot, with the person
    who typed it still there to say yes or no, so a wrong guess costs one tap.
    names_match runs unattended against a live match, where a wrong guess costs
    a post.
    """
    target = player_slug(typed)
    if not target:
        return []
    target_words = set(target.split('-'))

    scored = []
    for name in names:
        slug = player_slug(name)
        if not slug:
            continue
        words = set(slug.split('-'))
        if slug == target:
            score = 100
        elif target_words < words or words < target_words:
            score = 90
        elif any(w.startswith(target) or target.startswith(w)
                 for w in words) or slug.startswith(target):
            # "rodri" → "rodrigo", "gonzalez" → "n-gonzalez"
            score = 80
        else:
            # Against each word as well as the whole name: a misspelt surname
            # ("Halland") scores badly against "erling-haaland" as a string,
            # and perfectly well against the word it was aiming at.
            ratio = max(difflib.SequenceMatcher(None, target, cand).ratio()
                        for cand in (slug, *words))
            if ratio < 0.8:
                continue
            score = int(ratio * 70)
        scored.append((score, name))

    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [name for _score, name in scored[:limit]]


def delivery_url(pid: str) -> str:
    """The public URL Instagram is given for a staged picture."""
    return (f"https://res.cloudinary.com/{CLOUD_NAME}/image/upload/"
            f"{DELIVERY_TRANSFORM}/{pid}.jpg")


def event_keys_for(ev: dict) -> list[tuple[str, str, str]]:
    """(event_key, player, player_id) a timeline entry could have a picture for.

    Usually one. A paired substitution is two — one player came on and another
    went off in the same entry — and either can be the one somebody staged a
    picture for, so both are offered.

    An entry the feature has no opinion about (an assist, a VAR check, the
    half-time marker) yields nothing, which is how it stays ignored.
    """
    ev_type = ev.get('type')

    if ev_type == 'substitution':
        # The feed merges an on/off pair into one entry when it can match them
        # up; when it can't, it emits substitution_in / substitution_out
        # separately and the branch below handles those.
        pairs = [('SUB_IN', 'player_in'), ('SUB_OUT', 'player_out')]
        return [(key, ev.get(role), player_id_of(ev, role))
                for key, role in pairs if _is_named(ev.get(role))]

    key = TYPE_TO_KEY.get(ev_type)
    player = ev.get('player')
    if key and _is_named(player):
        return [(key, player, player_id_of(ev))]
    return []


def _is_named(player) -> bool:
    """The feed writes 'N/A' where it has no name — that is not a player."""
    name = str(player or '').strip()
    return bool(name) and name != 'N/A' and bool(player_slug(name))


def staged(match_id: str) -> dict[str, dict]:
    """Everything staged for this match, as {public_id: context}.

    The context is Cloudinary's custom metadata on the asset. It carries
    `player_id` once the player has been pinned to one — from the squad
    list at staging time, or from the clarification the worker asks for
    when the team news lands — and `player`, the feed's own spelling. A
    picture staged before any of that is knowable has an empty context and
    is matched by name until it gets resolved.

    One Admin API call rather than a HEAD per candidate: a timeline carries
    thirty-odd entries by full time and this is asked on a poll loop, so the
    per-candidate version would be thousands of requests a match to answer a
    question that is almost always "none of them".

    Raises on failure — the caller decides whether a Cloudinary outage is
    worth an alert or a quiet retry on the next poll.
    """
    return _list(f"{FOLDER}/{match_id}_")


def _list(prefix: str) -> dict[str, dict]:
    """{public_id: context} for everything Cloudinary holds under a prefix."""
    found: dict[str, dict] = {}
    cursor = None
    while True:
        result = cloudinary.api.resources(
            type='upload',
            prefix=prefix,
            context=True,
            max_results=100,
            **({'next_cursor': cursor} if cursor else {}),
        )
        for res in result.get('resources', []):
            ctx = (res.get('context') or {}).get('custom') or {}
            found[res['public_id']] = ctx
        cursor = result.get('next_cursor')
        if not cursor:
            break
    return found


def match_id_of(pid: str) -> str:
    """The fixture a staged public_id belongs to, or '' if it isn't one.

    The complement of parse_public_id(), which needs the match_id told to
    it. Reading it back out is what lets one listing of the whole folder be
    sorted into fixtures — see staged_all(). Safe because a match_id is the
    feed's numeric key and never contains the underscore the parts are
    joined with.
    """
    if not pid.startswith(f"{FOLDER}/"):
        return ''
    return pid[len(FOLDER) + 1:].split('_', 1)[0]


def staged_all(match_ids) -> dict[str, dict[str, dict]]:
    """{match_id: {public_id: context}} for every fixture in `match_ids`.

    One Admin API call for the whole folder rather than one per fixture:
    this answers "what is armed right now" for a person who just typed
    /staged with ten fixtures in the list, and ten listings to draw one
    message would be ten times the quota for the same answer.

    A fixture with nothing staged is present with an empty map, so the
    caller can tell "nothing armed" from "not asked about".
    """
    wanted = {str(m) for m in match_ids}
    found: dict[str, dict[str, dict]] = {mid: {} for mid in wanted}
    for pid, ctx in _list(f"{FOLDER}/").items():
        mid = match_id_of(pid)
        if mid in wanted:
            found[mid][pid] = ctx
    return found


def set_player_id(public_id: str, player_id: str, player: str = '') -> None:
    """Pin a staged picture to a player id, without re-uploading it.

    Used by the clarification flow: a picture staged as "rodri" before the
    team news, then confirmed as Rodrigo once the squads are published, is
    the same file — only what we know about it changed.
    """
    context = {'player_id': str(player_id)}
    if player:
        context['player'] = player
    cloudinary.uploader.add_context(context, [public_id])


def set_clarified(public_id: str, answer: str = 'as-typed') -> None:
    """Record that a person has already settled this picture's name.

    Only written when the answer was "leave it as I typed it". A picture
    pinned to an id needs no marker — it has one — but a deliberate refusal
    looks exactly like an unanswered question, and without this the worker
    would ask again every hour for the rest of the match.
    """
    cloudinary.uploader.add_context({'clarified': str(answer)}, [public_id])


def squad(scraper_data) -> list[tuple[str, str]]:
    """[(name, player_id)] for everyone the published team sheets name.

    Both sides, starting XI and bench together. Which shirt someone is
    wearing has never been part of finding a staged picture, and the bench
    is exactly what a picture staged for ⬆️ Subbed on needs.
    """
    formation = (scraper_data or {}).get('matchFormation')
    if not isinstance(formation, dict):
        return []
    out = []
    for side in ('team_A', 'team_B'):
        block = formation.get(side)
        if not isinstance(block, dict):
            continue
        for group in ('lineups', 'sub'):
            for person in (block.get(group) or []):
                if not isinstance(person, dict):
                    continue
                name = str(person.get('person') or '').strip()
                if name:
                    out.append((name, str(person.get('person_id') or '').strip()))
    return out


def benches_named(scraper_data) -> bool:
    """Have both sides published a bench, not just an XI?

    The feed fills the sheet in stages. Asking about a name before the
    substitutes are listed would report half the squad as missing, which is
    a question that answers itself wrongly.
    """
    formation = (scraper_data or {}).get('matchFormation')
    if not isinstance(formation, dict):
        return False
    return all(isinstance(formation.get(side), dict)
               and bool(formation[side].get('sub'))
               for side in ('team_A', 'team_B'))


def clarify(staged_map, match_id: str, squad_list) -> list[dict]:
    """What the team sheets have to say about each un-pinned staged picture.

    This is the second and last chance to turn a typed name into the feed's
    own key for a person. The first is suggest(), in the bot, and it is
    unavailable for anything staged before the team news drops — which is
    most of them, since the whole point of the feature is arming a picture
    early. Once the sheets are published every name in the match is knowable,
    and this is the only moment where that is true and a person is still
    around to be asked.

    One entry per picture that is neither pinned nor already settled:

      'match'    (name, id) — one squad member, beyond doubt. Pin it and say
                 nothing: it is the same decision the worker would make
                 unattended anyway, made once, against the whole squad.
      'options'  who it might be, best first, when doubt remains. Empty means
                 nobody in either squad resembles the name at all — worth
                 saying out loud now, while it can still be fixed.

    Beyond doubt means: exactly one squad member spells it that way, or —
    with no exact spelling anywhere — exactly one that names_match() would
    have fired for regardless. Anything looser is a question, not an answer.
    """
    by_name: dict[str, str] = {}
    for name, ident in (squad_list or ()):
        ident = str(ident or '').strip()
        if ident and name not in by_name:
            by_name[name] = ident
    if not by_name:
        return []

    out = []
    staged = _as_staged_map(staged_map)
    for pid in sorted(staged):
        ctx = staged[pid] or {}
        if str(ctx.get('player_id') or '').strip():
            continue                       # already pinned to an id
        if str(ctx.get('clarified') or '').strip():
            continue                       # asked, and answered "as typed"
        parsed = parse_public_id(pid, match_id)
        if not parsed:
            continue
        event_key, slug = parsed

        exact = [(n, i) for n, i in by_name.items() if player_slug(n) == slug]
        subset = [(n, i) for n, i in by_name.items()
                  if names_match(slug, player_slug(n))]

        settled = None
        if len(exact) == 1:
            settled = exact[0]
        elif not exact and len(subset) == 1:
            settled = subset[0]

        options: list[tuple[str, str]] = []
        if settled is None:
            options = exact or subset or [
                (n, by_name[n]) for n in suggest(slug, list(by_name))]

        out.append({'public_id': pid, 'event_key': event_key, 'staged': slug,
                    'match': settled, 'options': options})
    return out


def _as_staged_map(staged) -> dict[str, dict]:
    """Accept either shape: a {public_id: context} map, or a bare set of
    public_ids meaning "nothing is known about any of them".
    """
    if isinstance(staged, dict):
        return staged
    return {pid: {} for pid in (staged or ())}


def digest(public_id: str) -> str:
    """A short stable handle for a staged picture.

    Telegram caps callback_data at 64 bytes and a public_id plus a player id
    does not reliably fit. The worker sends the clarification and the bot
    handles the tap — two processes sharing nothing — so the button has to
    carry something both can compute from the picture itself.
    """
    return hashlib.sha1(public_id.encode()).hexdigest()[:8]


def _timeline_moments(events, match_id: str) -> list[dict]:
    """Every moment the timeline offers, in order, with its slug and id.

    Two kinds. Most are one timeline entry — a goal, a booking — read
    straight off it by event_keys_for(). The rest are counts: nothing in the
    feed says "hat-trick", it says goal, goal, goal, and the third one *is*
    the moment. Those are derived here, in the same pass, because the count
    only means anything in timeline order.

    A milestone is emitted alongside the goal that completed it, not instead
    of it — so a photo staged for GOAL and one staged for HAT_TRICK both
    belong to the same player and both fire, on his first goal and his third.
    """
    if not isinstance(events, list):
        return []

    out = []
    goals_so_far: dict[str, list[str]] = {}

    for ev in events:
        if not isinstance(ev, dict):
            continue

        for event_key, player, pid in event_keys_for(ev):
            out.append({
                'index':     len(out),
                'event_key': event_key,
                'player':    str(player).strip(),
                'slug':      player_slug(player),
                'player_id': pid,
                'team':      ev.get('team'),
                'minute':    str(ev.get('minute') or '').strip(),
                'assister':  ev.get('assister'),
            })

        if ev.get('type') not in MILESTONE_GOAL_TYPES:
            continue
        slug = player_slug(ev.get('player'))
        if not slug:
            continue
        # Tally by id where the feed gives one: two players with the same
        # short name would otherwise share a hat-trick between them.
        tally = goals_so_far.setdefault(player_id_of(ev) or slug, [])
        tally.append(str(ev.get('minute') or '').strip())

        milestone = GOAL_MILESTONES.get(len(tally))
        if milestone:
            out.append({
                'index':        len(out),
                'event_key':    milestone,
                'player':       str(ev.get('player') or '').strip(),
                'slug':         slug,
                'player_id':    player_id_of(ev),
                'team':         ev.get('team'),
                'minute':       tally[-1],
                'assister':     ev.get('assister'),
                # Every goal that got him here, for the caption to draw on.
                'goal_minutes': list(tally),
            })
    return out


def _hits_for(entry: dict, moments: list[dict]) -> list[dict]:
    """Timeline moments a staged picture belongs to.

    By player id when the picture has one — that is the feed's own key for
    a person, so it is exact, and it cannot be confused by two players
    sharing a surname or by the feed changing how it spells someone
    mid-match. Names are the fallback for a picture staged before any id
    was knowable, and everything tolerant and fallible about matching
    lives on that path alone.
    """
    same_event = [m for m in moments if m['event_key'] == entry['event_key']]
    if entry.get('player_id'):
        return [m for m in same_event if m['player_id'] == entry['player_id']]
    return [m for m in same_event if names_match(entry['slug'], m['slug'])]


def live_posted_keys(events, match_id: str, records: dict) -> set[str]:
    """Which already-posted pictures still have a moment in the timeline.

    Read from the timeline ALONE. That is the whole point of it existing
    separately from pending(): pending() needs the staged Cloudinary
    listing as well, so "pending() didn't produce it" conflates *the event
    was withdrawn* with *we couldn't read Cloudinary*. Retraction deletes a
    public post, and it must never fire on the second of those.

    `records` is {posted_key: {'player_id': ..., ...}} as main.py keeps it.
    """
    moments = _timeline_moments(events, match_id)
    live = set()
    for posted_key, record in (records or {}).items():
        parts = posted_key.split(':', 2)
        if len(parts) != 3 or parts[0] != 'EVENT':
            continue
        entry = {'event_key': parts[1], 'slug': parts[2],
                 'player_id': str((record or {}).get('player_id') or '')}
        if _hits_for(entry, moments):
            live.add(posted_key)
    return live


def pending(events, staged_ids, match_id: str) -> list[dict]:
    """The staged pictures whose moment has now happened, in timeline order.

    Pure: it reads a scrape and a set of public_ids and decides nothing about
    posting. A moment nobody staged a picture for produces no entry here at
    all — which is the whole of what makes the feature optional per match.

    One moment, one post, and one post per picture. Both directions are
    enforced here rather than left to the caller:

      * A picture matches the *earliest* moment it fits and yields one entry,
        so a player who scores twice does not get his goal picture posted
        twice — it goes up on the first, and the second is not a fresh
        occasion for it. Same for a second booking.
      * Two pictures cannot both claim one moment. That is reachable because
        matching is tolerant (see names_match): "felix" and "joao-felix" are
        two files and one player. The exact spelling wins if there is one,
        and the loser is marked 'duplicate' rather than posted.

    Anything this cannot decide is reported instead of guessed — a
    'conflict' or 'duplicate' key means "do not post, tell someone". Firing
    the wrong picture is worse than firing none: the page is public and
    there is no undo.
    """
    staged_map = _as_staged_map(staged_ids)
    staged = []
    for pid in sorted(staged_map):
        parsed = parse_public_id(pid, match_id)
        if parsed:
            ctx = staged_map[pid] or {}
            staged.append({'public_id': pid,
                           'event_key': parsed[0],
                           'slug': parsed[1],
                           'player_id': str(ctx.get('player_id') or '')})
    if not staged:
        return []

    moments = _timeline_moments(events, match_id)
    out = []

    for entry in staged:
        hits = _hits_for(entry, moments)
        if not hits:
            continue

        # Several timeline players answering to one staged name — two Silvas,
        # say. Which of them the picture is of cannot be worked out from here.
        distinct = {m['slug'] for m in hits}
        if len(distinct) > 1:
            out.append({
                'public_id':  entry['public_id'],
                'event_key':  entry['event_key'],
                'player':     entry['slug'],
                'posted_key': f"EVENT:{entry['event_key']}:{entry['slug']}",
                'conflict':   sorted({m['player'] for m in hits}),
                '_index':     len(moments),
            })
            continue

        # The earliest moment it fits. A second goal by the same player is
        # not a second occasion for the same picture — it is the same
        # picture, already spent on the first.
        moment = hits[0]
        out.append({
            'public_id':  entry['public_id'],
            'event_key':  entry['event_key'],
            'player':     moment['player'],
            'team':       moment['team'],
            'minute':     moment['minute'],
            'assister':   moment['assister'],
            # Only a milestone carries this: every goal that got him there.
            'goal_minutes': moment.get('goal_minutes'),
            # Keyed on what was staged, not on what the feed called them: the
            # feed can revise a name mid-match ("Messi" → "Lionel Messi"), and
            # a key that moved with it would repost the same picture.
            'posted_key': f"EVENT:{entry['event_key']}:{entry['slug']}",
            # What the feed calls this person. Recorded on the post so a
            # later liveness check needs nothing but the timeline.
            'player_id':  moment['player_id'],
            '_index':     moment['index'],
            '_exact':     entry['slug'] == moment['slug'],
        })

    _mark_duplicate_claims(out)

    # Timeline order, so a 12th-minute goal posts before the hat-trick it
    # eventually became. Sorting on the matched moment's own position rather
    # than a per-name lookup matters: a player who scores three times has
    # three GOAL entries, and only the one that actually fired is the one this
    # picture belongs to. Conflicts have no position and sort last; they are
    # not going anywhere near Instagram anyway.
    out.sort(key=lambda m: m['_index'])
    return [{k: v for k, v in m.items() if not k.startswith('_')}
            for m in out]


def _mark_duplicate_claims(resolved: list[dict]) -> None:
    """Stop two pictures going up for one moment, in place.

    Tolerant matching makes this reachable: a picture staged by tapping
    "Joao Felix" and another typed as "felix" are two files under two names
    and one player, and without this both would post for the same goal.

    The exactly-spelled one wins when there is exactly one — it is the one
    picked from the squad list, so it is the one that was meant. When
    nothing distinguishes them none of them wins, and the caller reports
    the collision rather than choosing at random.
    """
    by_moment: dict[int, list[dict]] = {}
    for entry in resolved:
        if 'conflict' not in entry:
            by_moment.setdefault(entry['_index'], []).append(entry)

    for group in by_moment.values():
        if len(group) < 2:
            continue
        exact = [e for e in group if e['_exact']]
        winner = exact[0] if len(exact) == 1 else None
        for entry in group:
            if entry is not winner:
                entry['duplicate'] = sorted(
                    other['public_id'] for other in group
                    if other is not entry)


def unfired(events, staged_ids: set[str], match_id: str,
            posted_keys) -> list[dict]:
    """Staged pictures that never posted, and the nearest thing that happened.

    The point is to turn a silence into a sentence. A picture staged as "Rodri"
    when the feed says "Rodrigo" simply never fires, and without this nobody
    would ever learn why — the match just ends with a photo that didn't go up
    and no indication that anything went wrong.

    'near' is the timeline entry for the same event whose name is closest to
    the one staged, when there is one worth naming. Empty means the moment
    genuinely never happened, which needs no explaining.
    """
    posted = set(posted_keys or ())
    moments = _timeline_moments(events, match_id)
    out = []

    for pid in sorted(staged_ids):
        parsed = parse_public_id(pid, match_id)
        if not parsed:
            continue
        event_key, slug = parsed
        if f"EVENT:{event_key}:{slug}" in posted:
            continue

        same_event = [m['player'] for m in moments if m['event_key'] == event_key]
        out.append({
            'public_id': pid,
            'event_key': event_key,
            'player':    slug,
            'near':      suggest(slug, same_event, limit=3),
        })
    return out


def delete_all(match_id: str) -> int:
    """Remove every staged picture for a match. Returns how many went.

    Called when the worker is done, so what is deleted is both the pictures
    that posted — they are on Instagram now, Cloudinary was only the hand-off
    — and the ones staged for moments that never came. Neither has any use
    once the match is over, and the match_id will never be live again.
    """
    result = cloudinary.api.delete_resources_by_prefix(f"{FOLDER}/{match_id}_")
    return len(result.get('deleted', {}))


def delete_one(public_id: str) -> bool:
    """Disarm a single staged picture. True if it was there and is now gone.

    The counterpart to staging, and the only safe way to undo one: the
    file is what the worker looks for, so removing it by hand in the
    Cloudinary console is the same operation done without the name being
    checked — one character wrong and a different match's photo goes.
    """
    result = cloudinary.api.delete_resources([public_id])
    return (result.get('deleted') or {}).get(public_id) == 'deleted'


def find_by_digest(staged_map, wanted: str) -> str | None:
    """The staged public_id whose digest() is `wanted`, or None.

    A button can only carry the digest — see digest() for why — so the tap
    has to be resolved back against a fresh listing. Fresh rather than
    remembered on purpose: the bot restarts often, and a handle that stops
    working after a restart is no use in the one flow that exists to undo
    a mistake.
    """
    for pid in _as_staged_map(staged_map):
        if digest(pid) == wanted:
            return pid
    return None


def describe(public_id: str, context: dict | None = None) -> str:
    """'Messi — ⚽ Goal', for a staged picture. Empty if it isn't one.

    The name is the feed's own spelling once something has pinned it (the
    squad list at staging time, or the clarification when the team news
    lands); until then it is the slug, shown as typed, because that is
    exactly the string that has to match and seeing it is the point.
    """
    mid = match_id_of(public_id)
    parsed = parse_public_id(public_id, mid) if mid else None
    if not parsed:
        return ''
    event_key, slug = parsed
    name = str((context or {}).get('player') or '').strip() or slug
    return f"{name} — {EVENT_LABELS.get(event_key, event_key)}"
