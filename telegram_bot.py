# telegram_bot.py — match photo intake bot.
"""
Telegram bot for the photos the pipeline posts, and for cards typed in by hand.

Two kinds of photo, and telling them apart is the whole of what the interface
is for. Both start from a fixture in matches.json; what differs is what the
photo becomes.

  📸 Scorecard photo  —  /start, /newphoto, or just send a photo
     The background the half-time or full-time card is drawn on. The renderer
     puts the score, the crests and the scorers over the top of it. One per
     match per moment, replaced by re-sending. No photo means the card still
     posts, on the standard template.

       /start          →  pick match  →  pick HT/FT  →  send photo
       send a photo    →  pick match  →  pick HT/FT  →  uploaded

  🎯 Event photo  —  /event
     Held for one player's moment. Nothing is drawn on it; if that moment
     happens during the match it is posted on its own, exactly as it was sent,
     with a caption written from the moment it fired on. If the moment never
     happens, nothing is posted and the photo is deleted at full time.

       /event  →  pick match  →  pick ⚽ Goal  →  pick team  →  pick player
               →  send photo  →  another player / other team / other event

     The last step is the loop: staging is rarely one picture, and the match,
     the event and the squad behind a photo are the same for every player in
     it. The menu after each upload goes back to a step rather than to the
     start — see _staged_menu. Only the match is never re-asked.

     Most of the events are one timeline entry. Two are not: a brace and a
     hat-trick are counts, so they are derived from the goals in order and
     fire on the goal that completes them.

     The extra question is the player, and it is not optional: the Cloudinary
     name is match + event + player, so the file cannot be stored without it.
     See event_photos.py for how it is stored and main._run_event_photos for
     what posts it.

Those are separate conversations rather than one with a branch. The scorecard
question ("what goes under the half-time card") and the event one ("what goes
up if Messi scores") are different questions with different answers, and
folding seven event buttons into the HT/FT step would make the everyday case
read the rare one's options every time. Each flow's first message names itself
for the same reason — two buttons a row apart lead to two different posts, and
the first message of a flow is the cheapest place to catch a wrong tap.

A photo sent with no conversation is treated as a scorecard background, and
says so. That path exists so a photo is never dropped: the bot restarts often
in production (its watchdog stops it whenever no match worker is active) and
conversation state lives only in memory, so a photo can easily arrive belonging
to no conversation at all — and the only thing worse than asking which match it
is would be silently ignoring it. The same fallback catches a photo that
arrives after the state was lost mid-flow.

A third flow, /card, does something different: it builds a whole match from
typed-in details rather than attaching a photo to a scraped one. It exists for
fixtures the scraper never sees — friendlies, lower divisions, an old game
worth reposting — and it ends by rendering the card, showing it in the chat,
and posting it to Instagram once you say so. See the "Manual match" section
below; the data assembly lives in manual_match.py.

Input is a fixed vocabulary, never free text: the commands in BOT_COMMANDS
(published to Telegram so the ☰ menu and "/" autocomplete list them) and
photos. There is no keyboard under the chat — the ☰ menu is the whole of it.
Every match, HT/FT and event choice is an inline button on the message that
asks for it. Anything else — a typed message, an
unknown command — gets the help text back rather than being parsed or ignored.
Two steps read typed text, and only while they are running, because neither can
be tapped into existence: /card's scorer lists, and the player's name in /event
when the feed hasn't published a squad to offer buttons from.

Cloudinary holds both kinds, under deterministic public_ids, and that name is
the only thing shared with the pipeline — no state passes between the two:

  match_photos/<match_id>_<HT|FT>              cloudinary_utils.photo_public_id
  event_photos/<match_id>_<KEY>_<player-slug>  event_photos.public_id

Both use overwrite=True, so re-sending replaces rather than duplicates.

Setup:
  1. Create a bot with @BotFather, put the token in .env as TELEGRAM_BOT_TOKEN.
  2. Optional: set TELEGRAM_ALLOWED_USER_IDS (comma-separated numeric user ids)
     to restrict who can use the bot. Strongly recommended — anyone who finds
     the bot can otherwise overwrite your match photos.
  3. Run:  python telegram_bot.py
"""

import asyncio
import json
import logging
import os
import tempfile
import traceback
from datetime import datetime

import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv
from PIL import Image
from telegram import (
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import event_photos
import manual_match
from config import IST_TZ, LOCAL_TZ
from cloudinary_utils import photo_public_id
from football_scraper_dom import get_match_data

load_dotenv()

logging.basicConfig(
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    level=logging.INFO,
)
logging.getLogger('httpx').setLevel(logging.WARNING)
log = logging.getLogger('bot')

cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET'),
)

MATCHES_FILE = 'matches.json'

SELECT_MATCH, SELECT_EVENT, WAIT_PHOTO = range(3)

# /event's states — a conversation of its own, not a branch of the one above.
# The two flows answer different questions about the same photo and the
# scorecard one is the everyday case; folding the seven event buttons into its
# HT/FT step would have made the common path pay for the rare one. Numbered
# clear of both the photo flow's and /card's so a stray state value can never
# be read as belonging to another conversation.
(E_MATCH, E_EVENT, E_TEAM, E_PLAYER, E_TYPE_PLAYER,
 E_PHOTO, E_MORE) = range(30, 37)

# /card's states. Numbered clear of the photo flow's so a stray state value can
# never be read as belonging to the other conversation.
(M_HOME, M_AWAY, M_EVENT, M_SCORE, M_COMPETITION, M_DATE,
 M_HOME_SCORERS, M_AWAY_SCORERS, M_BACKGROUND, M_CONFIRM,
 M_BADGE, M_THEME) = range(10, 22)

# Touched on every update the bot handles; read by .github/scripts/bot_watchdog.py.
#
# The watchdog normally kills the bot as soon as the last match worker finishes,
# which is right when the bot only ever attaches photos to live fixtures. /card
# has nothing to do with live fixtures — it is used precisely when nothing is
# running — so the bot has to be able to say "someone is talking to me". The
# file's mtime is that statement, and its staleness is what ends it.
SESSION_FILE = '.bot_session'

# Telegram will not hand a bot any file bigger than this — getFile fails with
# "file is too big", and no amount of retrying changes that. Worth checking
# before the download so the reply can say what actually went wrong.
TELEGRAM_FILE_LIMIT_BYTES = 20 * 1024 * 1024

# Cloudinary rejects oversized images too (10 MB on the free plan), so anything
# above this is downscaled first. Nothing is lost: the overlay renders at the
# photo's own resolution and Instagram serves at 1080px wide, so a long edge
# beyond MAX_EDGE_PX is detail no viewer will ever see.
UPLOAD_SOFT_LIMIT_BYTES = 9 * 1024 * 1024
MAX_EDGE_PX = 2560

# An event photo is posted exactly as it was uploaded — no overlay, no canvas
# to sit on — so its own shape is what Instagram is asked to accept, and
# Instagram refuses anything outside this band. A phone photo shot in portrait
# (9:16 ≈ 0.56) is the common offender. Only a picture outside the band is
# touched; one already in it is passed through untouched, which is the point.
POST_ASPECT_MIN = 3 / 4      # tallest portrait Instagram takes
POST_ASPECT_MAX = 1.91       # widest landscape Instagram takes

# Sending "as a file" keeps full quality but is also how a 25 MB photo arrives.
# Any document, not just image/*: a wrong file should be told so, not ignored.
PHOTO_ENTRY_FILTER = filters.PHOTO | filters.Document.ALL

# ── The fixed input vocabulary ────────────────────────────────────────────────
# Outside /card the bot accepts exactly two things: a photo, or one of these
# commands. Everything else gets pointed back at them rather than being
# interpreted — free text has no meaning there. This list is what the ☰ menu
# shows, so a command missing from it is a command nobody will find.

BOT_COMMANDS = [
    BotCommand('start', 'Send the background photo for a half-time or full-time card'),
    BotCommand('newphoto', 'Same as /start'),
    BotCommand('event', "Stage a photo for a player's moment in a match"),
    BotCommand('staged', 'Photos armed for a moment — tap to take one back'),
    BotCommand('card', 'Build a match card from details you type in'),
    BotCommand('batch', 'Cards waiting to be posted together'),
    BotCommand('list', 'Every fixture in the registry, with what will post'),
    BotCommand('cancel', 'Abandon whatever is in progress'),
    BotCommand('help', 'What this bot accepts'),
]
KNOWN_COMMANDS = tuple(c.command for c in BOT_COMMANDS)

# There is no reply keyboard. Every action the bot has is a command, and the
# ☰ menu next to the message box is where they are read off — one list, always
# the same, instead of five buttons sitting under the chat for a bot that is
# used a handful of times on a matchday. Anything asked mid-flow is an inline
# keyboard on the message that asks it, where the question is.

# Free text where a step asks for it: everything /card wants, and the player's
# name when a squad can't be listed. Commands are excluded so /cancel mid-flow
# cancels instead of being recorded as a team called "/cancel".
MANUAL_TEXT_FILTER = filters.TEXT & ~filters.COMMAND


def _typed_reply(placeholder: str) -> ForceReply:
    """Ask for a typed answer, with the field labelled by what it wants.

    This is as close to "the message box is only live when I want typing" as
    a bot can get. Telegram gives a bot no way to disable, hide or grey out
    the box — there is no such field in the Bot API, and a bot cannot restrict
    what a client lets someone type. ForceReply is the whole of what is
    available: it focuses the box, shows this placeholder inside it, and marks
    the reply as belonging to the question. So the steps that want typing say
    so, the steps that don't carry inline buttons instead, and typing anywhere
    else is answered by stray_message rather than acted on.

    A message may carry one reply_markup, so a step offering a button (the
    scorer lists, the theme) keeps the button — worth more than a hint.
    """
    return ForceReply(input_field_placeholder=placeholder)

# /foo for any foo the bot doesn't implement. Telegram allows @botname suffixes
# in groups, hence the optional tail.
UNKNOWN_COMMAND_FILTER = filters.COMMAND & ~filters.Regex(
    r'^/(' + '|'.join(KNOWN_COMMANDS) + r')(@\S+)?(\s|$)'
)

# The one message that has to teach the whole bot. Its shape is the decision it
# exists to help with: two of the three flows take a photo, and which one you
# want depends entirely on what the photo is meant to *do* — sit under a
# scorecard, or be the post. That question is answered before anything else.
HELP_TEXT = (
    "*What I do*\n"
    "Three jobs. Two of them take a photo — which one you want depends on "
    "what the photo is meant to do.\n"
    "\n"
    "*📸 Scorecard photo*  —  /start\n"
    "The background the half-time or full-time card is drawn on. I put the "
    "score, the crests and the scorers over the top of it. One per match per "
    "moment; sending another replaces it. Send nothing and the card still "
    "posts — on the standard template instead.\n"
    "\n"
    "*🎯 Event photo*  —  /event\n"
    "A photo held for one player's moment: Messi scoring, Ronaldo completing "
    "a hat-trick, Neymar sent off. "
    "Nothing is drawn on it. If that moment happens during the match, the "
    "photo posts on its own with its own caption, exactly as you sent it. If "
    "it never happens, nothing is posted and the photo is deleted at full "
    "time.\n"
    "\n"
    "*🆕 Manual card*  —  /card\n"
    "A whole match card typed in from scratch, for a fixture the scraper "
    "doesn't cover. I render it and send it to you; posting is a separate tap "
    "that never happens on its own. /batch is the pile waiting to go out "
    "together.\n"
    "\n"
    "*Which photo is which*\n"
    "The photo goes _under_ a scorecard  →  /start\n"
    "The photo _is_ the post  →  /event\n"
    "\n"
    "*Staging an event photo*\n"
    "/event → the match → what has to happen → the team → the player → the "
    "photo.\n"
    "After each one you can add another player, switch to the other team or "
    "change the event without picking the match again.\n"
    "Stage them as early as you like, as long as the fixture is in the list. "
    "The squad only appears about an hour before kickoff; before that you type "
    "the name instead, which works just as well — accents and capitals are "
    "folded away, so `messi` finds Messi.\n"
    "One photo per player per event. ⚽ Goal fires on their first goal; for a "
    "second or a third there are ⚽⚽ Brace and ⚽⚽⚽ Hat-trick, which fire the "
    "moment that goal goes in. Stage all three for one player if you want and "
    "all three post — but each is a separate Instagram post, so stage what's "
    "worth posting.\n"
    "🅰️ Assist fires on the goal the player set up — so the scorer's photo and "
    "the assister's photo both go out for the same goal, as two posts. Own "
    "goals never count as assisted.\n"
    "\n"
    "*What's armed*  —  /staged\n"
    "The photos waiting on a moment, fixture by fixture, with a ❌ on each. "
    "Tapping it is the only safe way to take one back down — deleting the "
    "file in Cloudinary by hand is the same thing with nothing checking which "
    "match you're deleting from. Everything still armed at full time is "
    "cleared automatically.\n"
    "\n"
    "*Sending a photo with no command*\n"
    "It becomes a scorecard background — I'll ask which match and whether it's "
    "half time or full time. Use /event if you meant a player's moment.\n"
    "\n"
    "*The fixture list*  —  /list\n"
    "Every fixture in the registry, earliest first: kick-off in German and "
    "Indian time, whether lineups and the half-time card will post, and which "
    "matches share a carousel. Reading only — fixtures are added in "
    "matches.json, not here.\n"
    "\n"
    "*Anything else*\n"
    "/cancel drops whatever is in progress.\n"
    "/help is this message.\n"
    "\n"
    "Everything I do is a command — ☰ next to the message box lists them, and "
    "typing / offers the same list. Anything I ask mid-flow comes as buttons "
    "on the question itself.\n"
    "Typed text only means something where a step asks for it — a player's "
    "name, or anything /card wants. Everywhere else it does nothing, and I'll "
    "say so rather than act on it."
)

# What the catch-all below answers: everything the conversation never looks at.
# Handlers in different groups all get a turn at the same update, so this has
# to exclude what group 0 already handles or every photo draws two replies.
STRAY_FILTER = (filters.ALL & ~filters.COMMAND & ~filters.PHOTO
                & ~filters.Document.ALL & ~filters.StatusUpdate.ALL)


def _load_matches() -> list[dict]:
    try:
        with open(MATCHES_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"[bot] Could not read {MATCHES_FILE}: {e}")
        return []


def _touch_session() -> None:
    """Record that a human just interacted with the bot.

    Best-effort by design: a bot that cannot write a file in its own working
    directory must still take photos. The only cost of a failed write is the
    watchdog shutting the bot down at its normal time.
    """
    try:
        with open(SESSION_FILE, 'w') as f:
            f.write(datetime.now().isoformat(timespec='seconds'))
    except Exception as e:
        log.debug("Could not touch %s: %s", SESSION_FILE, e)


async def keep_alive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Group -1: sees every update before anything else, handles none of them.

    Registered in its own group precisely so it does not consume the update —
    handlers in other groups still get their turn. Its whole job is the
    heartbeat file.
    """
    if _allowed(update):
        _touch_session()


def _allowed(update: Update) -> bool:
    raw = os.getenv('TELEGRAM_ALLOWED_USER_IDS', '').strip()
    if not raw:
        return True  # no allowlist configured
    allowed_ids = {s.strip() for s in raw.split(',') if s.strip()}
    return str(update.effective_user.id) in allowed_ids


def _match_keyboard(prefix: str = 'match') -> InlineKeyboardMarkup | None:
    """One button per fixture in matches.json, or None if it's empty.

    Both photo flows offer the same list and answer it in different
    conversations, so the callback prefix is the caller's — /event's buttons
    must not look like the scorecard flow's to the handler that gets them.
    """
    matches = _load_matches()
    if not matches:
        return None
    keyboard = [
        [InlineKeyboardButton(
            f"{m['home_team']} vs {m['away_team']} — {m.get('kickoff_utc', '')[:10]}",
            callback_data=f"{prefix}:{m['match_id']}",
        )]
        for m in matches
    ]
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(keyboard)


def _photo_ref(msg) -> tuple[str, int | None] | None:
    """(file_id, file_size) for the image in a message, or None if it isn't one.

    Only the file_id is kept, never the File object: a File's download URL is
    valid for about an hour, while a file_id can be resolved whenever the
    upload actually happens.
    """
    if msg.document:
        if not (msg.document.mime_type or '').startswith('image/'):
            return None
        return msg.document.file_id, msg.document.file_size
    if msg.photo:
        largest = msg.photo[-1]          # largest size Telegram kept
        return largest.file_id, largest.file_size
    return None


def _shrink_for_upload(path: str) -> bool:
    """Downscale an image in place until Cloudinary will accept it.

    Returns True if the file was rewritten. Best-effort: if Pillow can't read
    the file we leave it alone and let the upload report the real error.
    """
    try:
        size = os.path.getsize(path)
        with Image.open(path) as probe:
            probe.load()
            img = probe.convert('RGB')
            long_edge = max(img.size)
    except Exception as e:
        print(f"[bot] Could not inspect image for downscaling: {e}")
        return False

    if size <= UPLOAD_SOFT_LIMIT_BYTES and long_edge <= MAX_EDGE_PX:
        return False

    for edge, quality in ((MAX_EDGE_PX, 92), (MAX_EDGE_PX, 82), (1920, 82), (1440, 78)):
        scaled = img.copy()
        scaled.thumbnail((edge, edge), Image.LANCZOS)
        scaled.save(path, 'JPEG', quality=quality, optimize=True)
        if os.path.getsize(path) <= UPLOAD_SOFT_LIMIT_BYTES:
            print(f"[bot] Downscaled photo to {scaled.size} q{quality} "
                  f"({size / 1e6:.1f} MB → {os.path.getsize(path) / 1e6:.1f} MB)")
            return True

    print(f"[bot] Photo still {os.path.getsize(path) / 1e6:.1f} MB after downscaling")
    return True


def _fit_for_post(path: str) -> str | None:
    """Centre-crop an image in place until Instagram will take its shape.

    Returns a sentence describing what was cut, or None if nothing was — which
    is the usual answer, and the reason this is a crop rather than a pad: a
    picture already in the band keeps every pixel it arrived with.

    Best-effort, like _shrink_for_upload: an image Pillow can't open is left
    alone and the failure is reported by whatever tries to use it next.
    """
    try:
        with Image.open(path) as probe:
            probe.load()
            img = probe.convert('RGB')
            w, h = img.size
    except Exception as e:
        print(f"[bot] Could not inspect image for cropping: {e}")
        return None

    ratio = w / h if h else 1.0
    if POST_ASPECT_MIN <= ratio <= POST_ASPECT_MAX:
        return None

    if ratio < POST_ASPECT_MIN:          # too tall — trim top and bottom
        new_h = round(w / POST_ASPECT_MIN)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    else:                                # too wide — trim left and right
        new_w = round(h * POST_ASPECT_MAX)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))

    img.save(path, 'JPEG', quality=92, optimize=True)
    print(f"[bot] Cropped {w}x{h} → {img.size} for Instagram.")
    return (f"I cropped it from {w}×{h} to {img.size[0]}×{img.size[1]} — "
            f"Instagram won't accept a picture that shape, so the middle was "
            f"kept. Send a squarer crop of your own if the framing matters.")


async def _upload_photo(msg, context, match: dict, event_type: str,
                        ref: tuple[str, int | None], *,
                        public_id: str | None = None,
                        descriptor: str | None = None,
                        meta: dict | None = None,
                        fit_aspect: bool = False) -> bool:
    """Download from Telegram, downscale if needed, push to Cloudinary.

    Every failure path answers in the chat. A silent failure here is the worst
    outcome available: the photo looks sent, and the card goes out on the
    plain template hours later with nobody having been told.
    """
    file_id, file_size = ref

    if file_size and file_size > TELEGRAM_FILE_LIMIT_BYTES:
        await msg.reply_text(
            f"That image is {file_size / 1e6:.0f} MB, and Telegram won't let "
            f"the bot download anything over 20 MB.\n\n"
            f"Send it as a normal photo instead of a file, or export it a bit "
            f"smaller, and it'll go through."
        )
        return False

    public_id = public_id or photo_public_id(match['match_id'], event_type)
    await msg.reply_text("Uploading to Cloudinary…")

    tmp_path, crop_note = None, None
    try:
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
            tmp_path = tmp.name
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(tmp_path)
        _shrink_for_upload(tmp_path)
        if fit_aspect:
            crop_note = _fit_for_post(tmp_path)
        result = cloudinary.uploader.upload(
            tmp_path,
            public_id=public_id,
            overwrite=True,
            invalidate=True,   # purge CDN cache when replacing a photo
            **({'context': meta} if meta else {}),
        )
    except Exception as e:
        log.exception("Photo upload failed for %s", public_id)
        await msg.reply_text(
            f"Upload failed: {e}\n\nSend the photo again, or /cancel."
        )
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    await msg.reply_text(
        f"Done ✅\n"
        f"{match['home_team']} vs {match['away_team']} — {descriptor or event_type}\n"
        f"public_id: {result['public_id']}\n"
        f"{result['secure_url']}\n\n"
        + (crop_note + "\n\n" if crop_note else "")
        + "The automation will pick it up from here. /start for another photo."
    )
    return True


# ── Conversation handlers ─────────────────────────────────────────────────────

async def _clear_keyboard(msg, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Take down the old button keyboard, once per chat per run.

    Earlier versions left a persistent ReplyKeyboardMarkup under the chat, and
    Telegram keeps one until a bot explicitly removes it — a new build alone
    does not, so without this the buttons stay under every existing chat
    forever. ReplyKeyboardRemove has to ride on a message, so one is sent and
    deleted immediately; the removal outlives the message it arrived on.

    Best-effort throughout: this is cosmetic, and a chat that cannot be tidied
    must still be able to send a photo. `menu_shown` is the flag the old
    keyboard used, reused here so a chat mid-conversation across the upgrade
    is not tidied twice.
    """
    if context.chat_data.get('menu_shown'):
        return
    context.chat_data['menu_shown'] = True
    try:
        sent = await msg.reply_text(
            'The buttons have moved to the ☰ menu.',
            reply_markup=ReplyKeyboardRemove())
        await sent.delete()
    except Exception as e:
        log.debug("Could not remove the old reply keyboard: %s", e)


# Telegram rejects a message over 4096 characters. The fixture list is the one
# reply whose length grows with the registry, so it is split before it can hit
# that — never mid-fixture, which is what the block-by-block build below buys.
MAX_MESSAGE = 3500


def _kickoff_times(match: dict) -> tuple[str, str]:
    """(German, Indian) readings of this fixture's kick-off.

    Recomputed from kickoff_utc rather than read out of the file: matches.json
    carries kickoff_local and kickoff_ist, but only validate_matches.py keeps
    them current, and a fixture hand-added since the last run would otherwise
    be listed with a blank or a stale time. Those fields are the fallback for
    the one case this can't handle — a kickoff_utc that won't parse.
    """
    try:
        parsed = datetime.fromisoformat(
            str(match.get('kickoff_utc')).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return (match.get('kickoff_local') or '?',
                match.get('kickoff_ist') or '?')
    fmt = '%a %-d %b %Y, %H:%M %Z'
    return (parsed.astimezone(LOCAL_TZ).strftime(fmt),
            parsed.astimezone(IST_TZ).strftime(fmt))


def _posting_plan(match: dict) -> list[str]:
    """What the worker will post for this fixture, in the dispatcher's terms.

    Every default here mirrors main.py rather than restating it: half time is
    on unless switched off, lineups off unless switched on, and a stats slide
    is dropped for a grouped match because a carousel is one scorecard per
    match. A listing that disagreed with the dispatcher would be worse than no
    listing at all.
    """
    if match.get('post_lineups'):
        first = str(match.get('lineups_first') or 'home').strip().lower()
        first = 'away' if first == 'away' else 'home'
        lineups = f"on ({first} first)"
    else:
        lineups = 'off'

    lines = [
        f"Lineups: {lineups}",
        f"Half time: {'on' if match.get('post_ht', True) else 'off'}",
    ]

    if not match.get('post_ft_stats'):
        lines.append('FT stats: off')
    elif _carousel_group_of(match):
        lines.append('FT stats: off (carousel posts one slide per match)')
    else:
        lines.append('FT stats: on')

    if match.get('knockout_match'):
        lines.append('Knockout: extra time and penalties tracked')
    return lines


def _carousel_group_of(match: dict) -> str | None:
    """The group this fixture posts with, or None when it posts on its own."""
    group = str(match.get('carousel_group') or '').strip()
    return group or None


def _kickoff_key(match: dict) -> tuple[int, str]:
    """Sort key: by kick-off, anything unparseable last rather than crashing."""
    raw = str(match.get('kickoff_utc') or '')
    try:
        datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return (1, raw)
    return (0, raw)


def _fixture_block(index: int, match: dict) -> str:
    """One fixture as it is listed: who, when in both zones, and what posts."""
    local, ist = _kickoff_times(match)
    home = match.get('home_team', '?')
    away = match.get('away_team', '?')
    lines = [
        f"{index}. {home} vs {away}",
        f"   {match.get('competition') or 'competition not set'}",
        f"   🇩🇪 {local}",
        f"   🇮🇳 {ist}",
    ]
    group = _carousel_group_of(match)
    if group:
        lines.append(f"   🎠 Carousel: {group}")
    lines += [f"   {line}" for line in _posting_plan(match)]
    return '\n'.join(lines)


def _carousel_summary(matches: list[dict]) -> str:
    """The groups, spelled out — which fixtures actually post together.

    A carousel_group is the one setting whose effect isn't visible on the
    fixture that carries it: what matters is the set of matches sharing the
    name, and a typo in one of them silently splits the group in two. Listing
    them together is what makes that visible.
    """
    groups: dict[str, list[dict]] = {}
    for match in matches:
        group = _carousel_group_of(match)
        if group:
            groups.setdefault(group, []).append(match)
    if not groups:
        return 'No carousel groups — every fixture posts on its own.'

    lines = ['🎠 Carousel groups']
    for name, members in groups.items():
        lines.append(f"\n{name} — {len(members)} match"
                     f"{'es' if len(members) != 1 else ''}"
                     f"{', posts alone' if len(members) == 1 else ''}:")
        lines += [f"   • {m.get('home_team', '?')} vs {m.get('away_team', '?')}"
                  for m in members]
    return '\n'.join(lines)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/list — every fixture in the registry and what the worker will post.

    Read-only, and deliberately so: matches.json is edited in the repo and
    checked by validate_matches.py, so this answers 'what is armed for today'
    without giving the chat a way to change it.
    """
    if not _allowed(update):
        await update.message.reply_text("Not authorized.")
        return

    matches = [m for m in _load_matches() if isinstance(m, dict)]
    if not matches:
        await update.message.reply_text("No matches found in matches.json.")
        return

    await _clear_keyboard(update.message, context)
    matches.sort(key=_kickoff_key)

    header = (f"📋 {len(matches)} fixture{'s' if len(matches) != 1 else ''} "
              f"in matches.json\nTimes are 🇩🇪 German / 🇮🇳 Indian.")
    blocks = [header] + [_fixture_block(i, m) for i, m in enumerate(matches, 1)]
    blocks.append(_carousel_summary(matches))

    # Packed greedily so a fixture is never split across two messages.
    chunk = ''
    for block in blocks:
        if chunk and len(chunk) + len(block) + 2 > MAX_MESSAGE:
            await update.message.reply_text(chunk)
            chunk = ''
        chunk = f"{chunk}\n\n{block}" if chunk else block
    if chunk:
        await update.message.reply_text(chunk)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — also the answer to anything the bot doesn't understand."""
    if not _allowed(update):
        await update.message.reply_text("Not authorized.")
        return
    await _clear_keyboard(update.message, context)
    await update.message.reply_text(HELP_TEXT, parse_mode='Markdown')


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A /command the bot doesn't have. Say so instead of staying silent."""
    if not _allowed(update) or not update.message:
        return
    await _clear_keyboard(update.message, context)
    await update.message.reply_text(
        f"I don't have that command.\n\n{HELP_TEXT}",
        parse_mode='Markdown',
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _allowed(update):
        await update.message.reply_text("Not authorized.")
        return ConversationHandler.END

    keyboard = _match_keyboard()
    if keyboard is None:
        await update.message.reply_text("No matches found in matches.json.")
        return ConversationHandler.END

    context.user_data.clear()
    await _clear_keyboard(update.message, context)
    await update.message.reply_text(
        "📸 *Scorecard photo* — the background the half-time or full-time card "
        "is drawn on.\n"
        "_Wanted a photo that posts on its own when a player scores? That's "
        "/event._\n\n"
        "Select the match:",
        parse_mode='Markdown',
        reply_markup=keyboard,
    )
    return SELECT_MATCH


async def photo_first(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A photo arriving outside any conversation starts one around it.

    This is the path that catches a restart mid-flow, so it must never be the
    one that stays quiet.
    """
    if not _allowed(update):
        await update.message.reply_text("Not authorized.")
        return ConversationHandler.END

    ref = _photo_ref(update.message)
    if ref is None:
        await update.message.reply_text("That file isn't an image — send a JPG/PNG.")
        return ConversationHandler.END

    keyboard = _match_keyboard()
    if keyboard is None:
        await update.message.reply_text("No matches found in matches.json.")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data['pending'] = ref
    await _clear_keyboard(update.message, context)
    await update.message.reply_text(
        "Got the photo — I'll use it as a *scorecard background*.\n"
        "_For a photo that posts on its own when a player scores, /cancel and "
        "use /event._\n\n"
        "Which match is it for?",
        parse_mode='Markdown',
        reply_markup=keyboard,
    )
    return SELECT_MATCH


async def match_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    match_id = query.data.split(':', 1)[1]
    match = next((m for m in _load_matches() if str(m['match_id']) == match_id), None)
    if not match:
        await query.edit_message_text("Match not found — matches.json may have changed. /start to retry.")
        return ConversationHandler.END

    context.user_data['match'] = match
    keyboard = [[
        InlineKeyboardButton("Half Time", callback_data="event:HT"),
        InlineKeyboardButton("Full Time", callback_data="event:FT"),
    ], [InlineKeyboardButton("Cancel", callback_data="cancel")]]
    await query.edit_message_text(
        f"{match['home_team']} vs {match['away_team']}\nWhich scorecard is this photo for?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_EVENT


async def event_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    match = context.user_data.get('match')
    if match is None:
        # Only reachable if the process restarted between the two keyboards.
        await query.edit_message_text("That selection expired — /start to try again.")
        return ConversationHandler.END

    event_type = query.data.split(':', 1)[1]
    context.user_data['event_type'] = event_type

    await query.edit_message_text(
        f"{match['home_team']} vs {match['away_team']} — {event_type}"
    )

    # Photo-first: it's already in hand, so upload rather than ask for it again.
    pending = context.user_data.get('pending')
    if pending:
        ok = await _upload_photo(query.message, context, match, event_type, pending)
        return ConversationHandler.END if ok else WAIT_PHOTO

    await query.message.reply_text(
        "Now send the photo.\n"
        "Tip: send it as a *file/document* to keep full quality "
        "(Telegram compresses regular photo messages), but keep it under 20 MB "
        "— that's Telegram's own ceiling for what a bot can download.",
        parse_mode='Markdown',
    )
    return WAIT_PHOTO


# ── /event — a photo staged against a player's moment ────────────────────────
# Its own flow, not a branch of the scorecard one. A scorecard photo answers
# "what goes under the card at half time"; this answers "what goes up on its
# own if Messi scores" — different question, different post, and mixing them
# into one keyboard would have made the everyday case read the rare one's
# options every time.
#
# Same five questions in the same order as /start's two, with one more in the
# middle: match → event → team → player → photo. The player is the addition,
# and it is not optional — the Cloudinary name is match + event + player, so
# the picture cannot be stored until it is known.

BTN_TYPE_NAME = "✏️ Type the name instead"


async def event_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/event — stage a picture against a moment that hasn't happened yet."""
    if not _allowed(update):
        await update.message.reply_text("Not authorized.")
        return ConversationHandler.END

    keyboard = _match_keyboard('ev:match')
    if keyboard is None:
        await update.message.reply_text("No matches found in matches.json.")
        return ConversationHandler.END

    context.user_data.clear()
    await _clear_keyboard(update.message, context)
    await update.message.reply_text(
        "🎯 *Event photo* — held for one player's moment, and posted on its "
        "own, exactly as you send it, if that moment happens.\n"
        "_Wanted the background for a scorecard instead? That's /start._\n\n"
        "Which match is it for?",
        parse_mode='Markdown',
        reply_markup=keyboard,
    )
    return E_MATCH


async def event_match_chosen(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    match_id = query.data.split(':', 2)[2]
    match = next((m for m in _load_matches() if str(m['match_id']) == match_id), None)
    if not match:
        await query.edit_message_text(
            "Match not found — matches.json may have changed. /event to retry.")
        return ConversationHandler.END

    context.user_data['match'] = match
    await query.edit_message_text(
        f"{match['home_team']} vs {match['away_team']}\n"
        f"What has to happen for this picture to post?",
        reply_markup=_event_keyboard(),
    )
    return E_EVENT


def _event_keyboard() -> InlineKeyboardMarkup:
    """One button per event a picture can be staged against, laid out by kind —
    see EVENT_KEYBOARD_ROWS. A goal's type is its own button rather than a
    follow-up question: a penalty and an open-play goal are different moments
    wanting different pictures.

    Built here rather than inline because the "different event" button on the
    staged menu asks the same question again, and two copies would drift.
    """
    rows = [[InlineKeyboardButton(event_photos.EVENT_LABELS[key],
                                  callback_data=f"ev:evt:{key}")
             for key in row]
            for row in event_photos.EVENT_KEYBOARD_ROWS]
    rows.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


async def event_event_chosen(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    match = context.user_data.get('match')
    if match is None:
        # Only reachable if the process restarted between the two keyboards.
        await query.edit_message_text("That selection expired — /event to try again.")
        return ConversationHandler.END

    event_key = query.data.split(':', 2)[2]
    context.user_data['event_key'] = event_key
    await query.edit_message_text(
        f"{match['home_team']} vs {match['away_team']} — "
        f"{event_photos.EVENT_LABELS[event_key]}"
    )
    return await _ask_squad_side(query.message, context)


# ── Which player ─────────────────────────────────────────────────────────────
# Two ways, and the order matters: a tap on a name the feed itself published
# can't be misspelled, so the squad is offered whenever there is one, and
# typing is the fallback — for a picture staged before the team news drops, or
# a name the feed spells differently from everyone else.

async def _squad(context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    """The two squads for the chosen match, or None if none are published yet.

    {'home': [names], 'away': [names]} — starting XI first, then the bench,
    each in the order the feed lists them. Fetched once per conversation and
    kept in user_data: it is a real HTTP request, and the answer does not
    change between two taps a second apart.

    Run off the event loop because the scraper is synchronous requests and a
    slow fetch would otherwise stall every other chat the bot is handling.
    """
    if 'squad' in context.user_data:
        return context.user_data['squad']

    match = context.user_data.get('match') or {}
    context.user_data['squad'] = None      # cache the miss too
    try:
        data = await asyncio.to_thread(get_match_data, match.get('scraper_url'))
    except Exception as e:
        log.warning("Squad fetch failed for %s: %s", match.get('match_id'), e)
        return None
    if not isinstance(data, dict):
        return None

    formation = data.get('matchFormation')
    if not isinstance(formation, dict):
        return None

    squad, ids = {}, {}
    for side, key in (('home', 'team_A'), ('away', 'team_B')):
        block = formation.get(key)
        if not isinstance(block, dict):
            continue
        names = []
        for group in ('lineups', 'sub'):
            for p in (block.get(group) or []):
                if not isinstance(p, dict):
                    continue
                name = str(p.get('person') or '').strip()
                if not name:
                    continue
                names.append(name)
                # The feed's own key for this person. Tapping a name is the
                # one moment it is knowable for certain, so it is recorded
                # here and stored on the picture — after which nothing
                # downstream has to match on spelling at all.
                ident = str(p.get('person_id') or '').strip()
                if ident:
                    # Keyed by side as well as name: the two squads can share a
                    # surname, and one flat map would pin the wrong person_id
                    # onto the picture — see _player_id_for.
                    ids.setdefault(side, {}).setdefault(name, ident)
        if names:
            squad[side] = names

    context.user_data['squad_ids'] = ids
    context.user_data['squad'] = squad or None
    return context.user_data['squad']


async def _ask_squad_side(msg, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask which team the player is in — or skip straight to typing.

    The side is only ever a way to cut forty-odd names down to a list worth
    scrolling. It is not part of the key: a picture is found by match, event
    and player, and which shirt they were wearing never comes into it.
    """
    match = context.user_data['match']
    await msg.reply_text("Looking up the squads…")
    squad = await _squad(context)

    if not squad:
        await msg.reply_text(
            "No team news has been published for this match yet, so I can't "
            "list the players. That's normal more than an hour before kickoff."
        )
        return await _ask_typed_player(msg, context)

    rows = [[InlineKeyboardButton(match[f'{side}_team'],
                                  callback_data=f"ev:side:{side}")]
            for side in ('home', 'away') if squad.get(side)]
    rows.append([InlineKeyboardButton(BTN_TYPE_NAME, callback_data="ev:side:type")])
    rows.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    await msg.reply_text("Which team is the player in?",
                         reply_markup=InlineKeyboardMarkup(rows))
    return E_TEAM


def _player_id_for(context: ContextTypes.DEFAULT_TYPE, player: str) -> str:
    """
    The feed's own id for a name, or '' when it cannot be known for certain.

    With a side chosen the answer is unambiguous. A typed name has no side, so
    both squads are searched — and a name they both answer to is left unpinned
    rather than guessed. An empty id means the worker settles the picture on
    spelling later, which is the designed fallback; a wrong id is wrong for good.
    """
    ids = context.user_data.get('squad_ids') or {}
    side = context.user_data.get('side')
    if side:
        return (ids.get(side) or {}).get(player, '')
    found = {(ids.get(s) or {}).get(player, '') for s in ('home', 'away')}
    found.discard('')
    return found.pop() if len(found) == 1 else ''


async def _ask_typed_player(msg, context: ContextTypes.DEFAULT_TYPE) -> int:
    event_key = context.user_data.get('event_key')
    # A typed name belongs to no squad list. Left over from an earlier loop the
    # side would have the staged menu offering the wrong team's players back.
    context.user_data.pop('side', None)
    await msg.reply_text(
        f"Type the player's name for the "
        f"*{event_photos.EVENT_LABELS.get(event_key, 'event')}* picture.\n\n"
        "Spell it the way the scoreboard does — surname alone is usually "
        "right. Accents and capitals don't matter; I fold them away before "
        "matching.",
        parse_mode='Markdown',
        reply_markup=_typed_reply('Player name'),
    )
    # Read by stray_message in group 1, which would otherwise answer the name
    # with "typed messages don't do anything here" the moment it is accepted.
    context.user_data['awaiting_player'] = True
    return E_TYPE_PLAYER


async def event_side_chosen(update: Update,
                            context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    if query.data == "ev:side:type":
        await query.edit_message_text("Typing the name.")
        return await _ask_typed_player(query.message, context)

    side = query.data.split(':', 2)[2]
    return await _ask_player_list(query.message, context, side, query=query)


async def _ask_player_list(msg, context: ContextTypes.DEFAULT_TYPE, side: str,
                           query=None) -> int:
    """The squad keyboard for one side.

    Asked twice now: once on the way down /event, and again from the staged
    menu's "another player" / "other team" buttons — which is why the message
    to edit is a parameter rather than an assumption. With `query` the keyboard
    replaces what is on screen; without it, it arrives as a new message.
    """
    squad = (await _squad(context) or {}).get(side) or []
    if not squad:
        await msg.reply_text("I don't have that squad any more.")
        return await _ask_typed_player(msg, context)

    # Indexes, not names: callback_data is capped at 64 bytes and a name can
    # carry anything, colons included.
    context.user_data['squad_side'] = squad
    # Which side those indexes belong to. Not part of a photo's identity — see
    # _ask_squad_side — but the staged menu has to know whose list to re-open
    # and whose to offer as the other one.
    context.user_data['side'] = side
    rows = [[InlineKeyboardButton(name, callback_data=f"ev:player:{i}")
             for i, name in pair]
            for pair in _pairs(list(enumerate(squad)))]
    rows.append([InlineKeyboardButton(BTN_TYPE_NAME, callback_data="ev:player:type")])
    rows.append([InlineKeyboardButton("Cancel", callback_data="cancel")])

    match = context.user_data['match']
    text = f"{match[f'{side}_team']} — who is the picture of?"
    markup = InlineKeyboardMarkup(rows)
    if query is not None:
        await query.edit_message_text(text, reply_markup=markup)
    else:
        await msg.reply_text(text, reply_markup=markup)
    return E_PLAYER


def _pairs(items: list) -> list[list]:
    """[a, b, c] → [[a, b], [c]] — two buttons to a row."""
    return [items[i:i + 2] for i in range(0, len(items), 2)]


async def event_player_chosen(update: Update,
                              context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    if query.data == "ev:player:type":
        await query.edit_message_text("Typing the name.")
        return await _ask_typed_player(query.message, context)

    if query.data == "ev:player:asis":
        typed = context.user_data.get('typed_player')
        if not typed:
            await query.edit_message_text("That expired — /event to try again.")
            return ConversationHandler.END
        await query.edit_message_text(f"{typed} — as typed.")
        return await _player_settled(query.message, context, typed)

    squad = context.user_data.get('squad_side') or []
    try:
        player = squad[int(query.data.split(':', 2)[2])]
    except (ValueError, IndexError):
        await query.edit_message_text("That name expired — /event to try again.")
        return ConversationHandler.END

    await query.edit_message_text(player)
    return await _player_settled(query.message, context, player)


async def event_player_typed(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> int:
    """A typed name, checked against the squad before it is believed.

    This is where "Rodri" and "Rodrigo" are reconciled, and it is the only
    place they can be: the worker matches names unattended against a live
    match, so it has to be strict, and a name typed the way people say it out
    loud rather than the way the team sheet prints it would simply never fire.
    Here the person who typed it is still in the conversation and one tap
    settles it.

    Nothing is *blocked* by this. A name the squad doesn't know still stages,
    with a warning saying so — a squad list can be incomplete, a name can be
    right when this thinks it isn't, and refusing the upload over a guess
    would be worse than the silence it is trying to prevent.
    """
    typed = (update.message.text or '').strip()
    if not event_photos.player_slug(typed):
        await update.message.reply_text(
            "I can't make a name out of that — letters or numbers, please.",
            reply_markup=_typed_reply('Player name'),
        )
        return E_TYPE_PLAYER

    squad = await _squad(context) or {}
    everyone = [n for side in ('home', 'away') for n in squad.get(side, [])]
    if not everyone:
        # No team news yet, so there is nothing to check against. Stage it and
        # say what has to be true for it to work.
        await update.message.reply_text(
            f"No squads published yet, so I can't check \"{typed}\" against "
            f"anything.\n\n"
            f"It'll post as long as the scoreboard spells the name the same "
            f"way. If you're not sure, run /event again once the team news is "
            f"out — about an hour before kickoff — and pick the name from the "
            f"list instead."
        )
        return await _player_settled(update.message, context, typed)

    typed_slug = event_photos.player_slug(typed)
    matches = [n for n in everyone
               if event_photos.player_slug(n) == typed_slug]
    if matches:
        return await _player_settled(update.message, context, matches[0])

    options = event_photos.suggest(typed, everyone)
    if not options:
        await update.message.reply_text(
            f"⚠️ Nobody called \"{typed}\" is in either squad.\n\n"
            f"I'll stage it anyway, but it will only ever post if the "
            f"scoreboard spells the name exactly that way — so if this was a "
            f"typo, /cancel and start again."
        )
        return await _player_settled(update.message, context, typed)

    # Indexes into this list, same as the squad keyboard — see event_side_chosen.
    context.user_data['squad_side'] = options
    rows = [[InlineKeyboardButton(name, callback_data=f"ev:player:{i}")]
            for i, name in enumerate(options)]
    rows.append([InlineKeyboardButton(f"Neither — stage \"{typed}\" as typed",
                                      callback_data="ev:player:asis")])
    rows.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    context.user_data['typed_player'] = typed

    await update.message.reply_text(
        f"I can't see \"{typed}\" in either squad. Did you mean one of these?\n\n"
        f"The scoreboard's spelling is the one that has to match, so picking "
        f"from this list is what makes the photo actually fire.",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return E_PLAYER


async def _player_settled(msg, context: ContextTypes.DEFAULT_TYPE,
                          player: str) -> int:
    """Everything is known — ask for the photo, or upload the one already held."""
    match = context.user_data['match']
    event_key = context.user_data['event_key']
    context.user_data['player'] = player
    # Only a name that came from the squad has one. A typed name that the
    # squad didn't recognise stays un-pinned and is matched on spelling until
    # the worker's clarification settles it — see main._clarify_staged_photos.
    context.user_data['player_id'] = _player_id_for(context, player)
    context.user_data.pop('awaiting_player', None)

    # Popped, not read: a held photo belongs to the name being settled right
    # now and to no other. Left in place it is uploaded again for whoever is
    # chosen next, which the staged menu makes an ordinary thing to do.
    pending = context.user_data.pop('pending', None)
    if pending:
        ok = await _upload_event_photo(msg, context, pending)
        return await _staged_menu(msg, context) if ok else E_PHOTO

    await msg.reply_text(
        f"Staging {player} — {event_photos.EVENT_LABELS[event_key]} — for "
        f"{match['home_team']} vs {match['away_team']}.\n\n"
        "Now send the photo. It posts exactly as you send it, with no "
        "scoreboard drawn on top, so send the picture you want on the page.\n\n"
        "If the event never happens, nothing is posted and the picture is "
        "deleted when the match ends."
        # Deliberately not Markdown: the name is whatever was typed, and one
        # stray underscore in it would have Telegram reject the whole message.
    )
    return E_PHOTO


async def event_photo_early(update: Update,
                            context: ContextTypes.DEFAULT_TYPE) -> None:
    """A photo that arrived while the bot still doesn't know who it is for.

    Held rather than dropped. Returning None keeps the conversation in the
    step it was in, so the question that was asked is still the question in
    front of you — and _player_settled uploads what is waiting the moment it
    is answered.
    """
    ref = _photo_ref(update.message)
    if ref is None:
        await update.message.reply_text("That file isn't an image — send a JPG/PNG.")
        return
    context.user_data['pending'] = ref
    await update.message.reply_text(
        "Got the photo — I'll upload it as soon as you've told me which "
        "player it's for."
    )


async def event_photo_received(update: Update,
                               context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message

    ref = _photo_ref(msg)
    if ref is None:
        await msg.reply_text(
            "That file isn't an image — send a JPG/PNG."
            if msg.document else "Please send a photo (or an image file)."
        )
        return E_PHOTO

    if not (context.user_data.get('match')
            and context.user_data.get('event_key')
            and context.user_data.get('player')):
        # State lost under us. Unlike the scorecard flow there is nothing to
        # fall back on — which match, which event and which player are three
        # questions, and guessing any of them would stage the picture against
        # the wrong moment.
        await msg.reply_text(
            "I've lost track of what that photo is for — /event to start again."
        )
        return ConversationHandler.END

    ok = await _upload_event_photo(msg, context, ref)
    return await _staged_menu(msg, context) if ok else E_PHOTO


async def _upload_event_photo(msg, context: ContextTypes.DEFAULT_TYPE,
                              ref: tuple[str, int | None]) -> bool:
    """Stage a picture against one player's moment."""
    match = context.user_data['match']
    event_key = context.user_data['event_key']
    player = context.user_data['player']
    player_id = context.user_data.get('player_id') or ''
    label = event_photos.EVENT_LABELS[event_key]
    public_id = event_photos.public_id(match['match_id'], event_key, player)
    # The id is (match, event, player), so the same pair twice overwrites rather
    # than adds — and the menu offering the same list again makes that an easy
    # mis-tap. Silence would leave you believing two pictures are armed.
    seen = context.user_data.setdefault('staged_this_run', set())
    replaced = public_id in seen

    ok = await _upload_photo(
        msg, context, match, event_key, ref,
        public_id=public_id,
        descriptor=f"{player}, {label}",
        # Written onto the asset, not kept here: the worker is a different
        # process on a different machine, and the picture is the only thing
        # the two of them share.
        meta={'player_id': player_id, 'player': player} if player_id else None,
        # This one is posted as-is rather than drawn onto a card, so its own
        # shape has to be one Instagram will take.
        fit_aspect=True,
    )
    if not ok:
        return False
    seen.add(public_id)
    if replaced:
        # Deliberately not Markdown: the name is whatever was typed.
        await msg.reply_text(
            f"⚠️ That replaced the picture already staged for {player} — "
            f"{label}. One picture per player per moment, so the earlier one "
            f"is gone."
        )
    return True


# ── After one photo: the next one, without starting over ─────────────────────
# Staging is rarely a single picture. A goal keyboard for Bayern is three or
# four players, and the match, the event and the squad behind them are the same
# for every one of them. Ending the conversation after each photo charged four
# already-answered questions for every extra player.
#
# So the flow returns to a *step* rather than to the start: another player in
# the same squad, the other squad, or a different event for the same match. The
# match itself is never re-asked — /event is still how you change that.

async def _staged_menu(msg, context: ContextTypes.DEFAULT_TYPE) -> int:
    """What to offer after a picture is staged, given what is already known."""
    match     = context.user_data['match']
    event_key = context.user_data['event_key']
    squad     = context.user_data.get('squad') or {}
    side      = context.user_data.get('side')
    other     = {'home': 'away', 'away': 'home'}.get(side or '')

    rows = []
    if side and squad.get(side):
        rows.append([InlineKeyboardButton(
            f"➕ Another {match[f'{side}_team']} player",
            callback_data="ev:more:player")])
    if other and squad.get(other):
        rows.append([InlineKeyboardButton(
            f"🔁 {match[f'{other}_team']} instead",
            callback_data="ev:more:team")])
    elif not side and (squad.get('home') or squad.get('away')):
        # The name was typed, so there is no list to return to — but a squad
        # exists, so the team question is still worth offering.
        rows.append([InlineKeyboardButton("➕ Another player",
                                          callback_data="ev:more:team")])
    rows.append([InlineKeyboardButton("🎯 Different event",
                                      callback_data="ev:more:event")])
    rows.append([InlineKeyboardButton("✅ Done", callback_data="ev:more:done")])

    # Retire the previous menu so only the newest one has live buttons. Every
    # staged photo leaves one behind otherwise, and a tap on a stale one matches
    # no handler — which in Telegram is not an error, just a spinner that never
    # stops.
    previous = context.user_data.pop('menu_msg', None)
    if previous is not None:
        try:
            await previous.edit_reply_markup(reply_markup=None)
        except Exception as e:
            log.debug("Could not retire the previous staged menu: %s", e)

    context.user_data['menu_msg'] = await msg.reply_text(
        f"{match['home_team']} vs {match['away_team']} — "
        f"{event_photos.EVENT_LABELS[event_key]}.\n"
        f"Anything else for this match?",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return E_MORE


async def event_more_chosen(update: Update,
                            context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data in ("cancel", "ev:more:done"):
        await query.edit_message_text("Done. /staged lists everything armed.")
        return ConversationHandler.END

    match = context.user_data.get('match')
    if match is None:
        await query.edit_message_text("That expired — /event to try again.")
        return ConversationHandler.END

    # This menu is about to become the next question, so it is no longer a
    # menu to retire.
    context.user_data.pop('menu_msg', None)

    # Everything about the picture just staged goes; everything above the step
    # being returned to stays. `pending` is deliberately NOT cleared here: at
    # this point it can only be a photo sent *at* the menu, meant for the
    # player about to be chosen — _player_settled pops the one it consumes.
    for key in ('player', 'player_id', 'awaiting_player'):
        context.user_data.pop(key, None)

    # A squad miss is cached for the whole conversation, and this conversation
    # now outlives the team news it was told didn't exist. Dropping the cached
    # None makes the next step look again; a real squad is left alone.
    if context.user_data.get('squad', False) is None:
        context.user_data.pop('squad', None)
        context.user_data.pop('squad_ids', None)

    choice = query.data.split(':', 2)[2]

    if choice == 'event':
        context.user_data.pop('event_key', None)
        await query.edit_message_text(
            f"{match['home_team']} vs {match['away_team']}\n"
            f"What has to happen for this picture to post?",
            reply_markup=_event_keyboard(),
        )
        return E_EVENT

    side = context.user_data.get('side')

    if choice == 'team':
        other = {'home': 'away', 'away': 'home'}.get(side or '')
        if other:
            return await _ask_player_list(query.message, context, other, query=query)
        # No side was ever chosen — the name was typed. Ask the team question
        # properly rather than guessing which squad was meant.
        await query.edit_message_text("Which team?")
        return await _ask_squad_side(query.message, context)

    # 'player' — the same squad again.
    if side:
        return await _ask_player_list(query.message, context, side, query=query)
    return await _ask_typed_player(query.message, context)


async def photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message

    ref = _photo_ref(msg)
    if ref is None:
        await msg.reply_text(
            "That file isn't an image — send a JPG/PNG."
            if msg.document else "Please send a photo (or an image file)."
        )
        return WAIT_PHOTO

    match = context.user_data.get('match')
    event_type = context.user_data.get('event_type')
    if not match or not event_type:
        # State lost under us — fall back to asking rather than dropping it.
        context.user_data.clear()
        context.user_data['pending'] = ref
        keyboard = _match_keyboard()
        if keyboard is None:
            await msg.reply_text("No matches found in matches.json.")
            return ConversationHandler.END
        await msg.reply_text(
            "Got the photo — I'll use it as a *scorecard background*.\n\n"
            "Which match is it for?",
            parse_mode='Markdown', reply_markup=keyboard)
        return SELECT_MATCH

    ok = await _upload_photo(msg, context, match, event_type, ref)
    return ConversationHandler.END if ok else WAIT_PHOTO


# ── /staged — what is armed, and the only safe way to take it back ───────────
# A staged picture is invisible by design: it lives under a Cloudinary name
# nobody sees, does nothing for hours, and then posts on its own, in public.
# So there has to be an answer to "what did I arm?" that isn't the Cloudinary
# console — and a way to disarm one that isn't deleting an object there by
# hand, which is the same operation with none of the checks and a different
# match's photo one keystroke away.
#
# Not a conversation. It is a list and a tap, and it has to work while /card
# or /event is half-finished — which is also why the tap carries a digest of
# the public_id rather than an index into a list the bot would have to
# remember: see event_photos.digest.

async def staged_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/staged — one message per fixture that has something armed."""
    if not _allowed(update):
        await update.message.reply_text("Not authorized.")
        return

    matches = _load_matches()
    if not matches:
        await update.message.reply_text("No matches found in matches.json.")
        return

    await _clear_keyboard(update.message, context)
    try:
        # One Admin API call for the whole folder, off the event loop — it is
        # a real HTTP request and every other chat is waiting on this thread.
        found = await asyncio.to_thread(
            event_photos.staged_all, [m['match_id'] for m in matches])
    except Exception as e:
        log.warning("Could not list staged photos: %s", e)
        await update.message.reply_text(
            f"I couldn't reach Cloudinary to check what's staged: {e}\n\n"
            f"Nothing has changed — try /staged again in a moment."
        )
        return

    armed = [m for m in matches if found.get(str(m['match_id']))]
    if not armed:
        await update.message.reply_text(
            "Nothing is staged.\n\n"
            "/event arms a photo for one player's moment. Anything armed is "
            "listed here until it posts or the match ends."
        )
        return

    for match in armed:
        await update.message.reply_text(
            **_staged_message(match, found[str(match['match_id'])]))


def _staged_message(match: dict, staged_map: dict, note: str = '') -> dict:
    """The listing for one fixture, as kwargs for reply_text/edit_message_text.

    Deliberately not Markdown: a player's name is whatever was typed, and one
    stray underscore in it would have Telegram reject the whole message.
    """
    fixture = f"{match['home_team']} vs {match['away_team']}"
    if not staged_map:
        return {'text': f"{note}Nothing is staged for {fixture} any more."}

    lines, rows, unpinned = [], [], False
    for pid in sorted(staged_map,
                      key=lambda p: event_photos.describe(p, staged_map[p]) or p):
        ctx = staged_map[pid] or {}
        label = event_photos.describe(pid, ctx) or pid
        if str(ctx.get('player_id') or '').strip():
            lines.append(f"• {label}")
        else:
            unpinned = True
            lines.append(f"• {label}   (by name)")
        rows.append([InlineKeyboardButton(
            f"❌ {label}", callback_data=f"sd:{event_photos.digest(pid)}")])

    count = len(staged_map)
    text = (f"{note}🎯 {fixture}\n"
            f"{count} photo{'s' if count > 1 else ''} armed:\n\n"
            + '\n'.join(lines) +
            "\n\nTap one to take it back down. Anything left armed posts on "
            "its own if the moment happens, and is deleted at full time either "
            "way.")
    if unpinned:
        text += ("\n\n(by name) — not pinned to a player id yet, so it only "
                 "fires if the scoreboard spells the name that way.")
    return {'text': text, 'reply_markup': InlineKeyboardMarkup(rows)}


async def staged_drop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A tap on ❌ — disarm one staged picture.

    The digest is resolved against a *fresh* listing rather than something
    remembered from when the message was drawn. The bot restarts often, and a
    button that stops working after a restart is no use in the one flow that
    exists to undo a mistake — nor is one that deletes whatever now happens to
    sit at the position the tap was aimed at.
    """
    query = update.callback_query
    if not _allowed(update):
        # Answered before anything is looked up: a tap from outside the
        # allowlist should not cost a Cloudinary listing.
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    wanted = query.data.split(':', 1)[1]
    matches = _load_matches()
    try:
        found = await asyncio.to_thread(
            event_photos.staged_all, [m['match_id'] for m in matches])
    except Exception as e:
        log.warning("Could not list staged photos: %s", e)
        await query.message.reply_text(
            f"I couldn't reach Cloudinary: {e}\n\n"
            f"Nothing was removed — try /staged again in a moment."
        )
        return

    everything = {pid: ctx
                  for per_match in found.values()
                  for pid, ctx in per_match.items()}
    public_id = event_photos.find_by_digest(everything, wanted)
    if public_id is None:
        await query.edit_message_text(
            "That photo isn't staged any more — it has already been removed, "
            "or the match has finished and everything was cleared.\n\n"
            "/staged for the current list."
        )
        return

    label = event_photos.describe(public_id, everything[public_id]) or public_id
    try:
        await asyncio.to_thread(event_photos.delete_one, public_id)
    except Exception as e:
        log.warning("Could not delete %s: %s", public_id, e)
        await query.message.reply_text(
            f"I couldn't remove \"{label}\": {e}\n\n"
            f"It is still staged and will still post. /staged to try again."
        )
        return

    match_id = event_photos.match_id_of(public_id)
    match = next((m for m in matches if str(m['match_id']) == match_id), None)
    remaining = {pid: ctx for pid, ctx in found.get(match_id, {}).items()
                 if pid != public_id}
    note = f"❌ Removed {label} — it won't post.\n\n"
    if match is None:
        await query.edit_message_text(note.strip())
        return
    await query.edit_message_text(**_staged_message(match, remaining, note))


# ── The worker's question, answered here ─────────────────────────────────────
# When the team sheets are published the match worker pins every staged photo
# it can to a player id and asks about the rest — "is rodri → Rodrigo?" — as a
# message with buttons. The worker runs in a different process on a different
# machine and the two share nothing, so the button carries only what both can
# compute from the picture itself: a digest of its public_id, and the id of
# the player being offered. See event_photos.digest and main._ask_who_it_is.

async def staged_clarify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A tap on one of the worker's "who is this?" buttons."""
    query = update.callback_query
    if not _allowed(update):
        # Answered before anything is looked up: a tap from outside the
        # allowlist should not cost a Cloudinary listing.
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    parts = query.data.split(':', 2)
    if len(parts) != 3:
        return
    _prefix, handle, player_id = parts

    matches = _load_matches()
    try:
        found = await asyncio.to_thread(
            event_photos.staged_all, [m['match_id'] for m in matches])
    except Exception as e:
        log.warning("Could not list staged photos: %s", e)
        await query.message.reply_text(
            f"I couldn't reach Cloudinary: {e}\n\n"
            f"Nothing changed — tap it again in a moment."
        )
        return

    everything = {pid: ctx
                  for per_match in found.values()
                  for pid, ctx in per_match.items()}
    public_id = event_photos.find_by_digest(everything, handle)
    if public_id is None:
        await query.edit_message_text(
            "That photo isn't staged any more — it was removed, or the match "
            "has finished and everything was cleared."
        )
        return

    staged_as = event_photos.describe(public_id, everything[public_id]) or public_id

    if player_id == '-':
        # "Leave it as typed." Recorded on the picture rather than remembered
        # here: the worker is what asks, and it has to stop asking.
        try:
            await asyncio.to_thread(event_photos.set_clarified, public_id)
        except Exception as e:
            log.warning("Could not mark %s clarified: %s", public_id, e)
        await query.edit_message_text(
            f"Left as staged: {staged_as}.\n\n"
            f"It still posts — but only if the scoreboard spells the name that "
            f"way. /staged takes it down if you'd rather it didn't."
        )
        return

    match_id = event_photos.match_id_of(public_id)
    match = next((m for m in matches if str(m['match_id']) == match_id), None)
    name = await _named_in_squad(match, player_id)

    try:
        await asyncio.to_thread(event_photos.set_player_id,
                                public_id, player_id, name or '')
    except Exception as e:
        log.warning("Could not pin %s to %s: %s", public_id, player_id, e)
        await query.message.reply_text(
            f"I couldn't pin it: {e}\n\n"
            f"Nothing is lost — the photo is still staged as {staged_as} and "
            f"still posts if the spelling matches. Tap again to retry."
        )
        return

    parsed = event_photos.parse_public_id(public_id, match_id)
    label = event_photos.EVENT_LABELS.get(parsed[0], '') if parsed else ''
    await query.edit_message_text(
        f"✅ Pinned to {name or f'player {player_id}'}"
        f"{f' — {label}' if label else ''}.\n\n"
        f"It now posts on their moment however the scoreboard spells the name."
    )


async def _named_in_squad(match: dict | None, player_id: str) -> str | None:
    """The feed's spelling for a player id, or None if it can't be looked up.

    Best-effort on purpose. The id is what makes the photo fire; the name is
    only what the confirmation says back and what /staged shows later, so a
    failed lookup costs a nicer sentence and nothing else.
    """
    if not match or not match.get('scraper_url'):
        return None
    try:
        data = await asyncio.to_thread(get_match_data, match['scraper_url'])
    except Exception as e:
        log.warning("Squad lookup failed for %s: %s", match.get('match_id'), e)
        return None
    for name, ident in event_photos.squad(data if isinstance(data, dict) else {}):
        if ident == str(player_id):
            return name
    return None


# ── Manual match: /card ───────────────────────────────────────────────────────
# One field per message, because the alternative — one big blob to be parsed —
# fails as a whole and gives no clue which line was wrong. Everything typed is
# parsed the moment it arrives, so a bad date is caught on the message that
# contained it rather than eight steps later at render time.
#
# The flow keeps its own corner of user_data. The photo conversation shares the
# same dict, and the two can be interleaved by a mistimed tap.

SCORER_HELP = (
    "*Format:* one event per line, `minute name (type)`.\n\n"
    "`23 Saka`\n"
    "`45+2 Havertz (pen)`\n"
    "`67 Rice (og)`\n"
    "`88 Odegaard (red)`\n"
    "`90 Martinelli (miss)`\n\n"
    "No `(type)` means a goal. The types are `pen`, `og`, `red` and `miss`.\n"
    "Send *none* if there aren't any.\n\n"
    "_I'll reject the whole block if any line doesn't fit, and say which — a "
    "list that's half read would make a card that looks complete with a goal "
    "missing._"
)

# Always a way out of a scorer step without typing. A team that didn't score
# and had nobody sent off has nothing to enter, and "send the word none" is a
# thing you have to have read — a button is a thing you can see.
SCORERS_NONE_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("Nothing to add", callback_data="scorers:none")],
])

# Shown when the goals entered don't match the score already given.
SCORERS_MISMATCH_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ That's right, carry on", callback_data="scorers:ok")],
    [InlineKeyboardButton("✏️ Let me redo them", callback_data="scorers:redo")],
])

SCORER_STATE = {'home': M_HOME_SCORERS, 'away': M_AWAY_SCORERS}


def _confirm_keyboard(count: int, room: bool) -> InlineKeyboardMarkup:
    """What to do with the card just built.

    Posting is never the only way out and never automatic: the card itself is
    the deliverable, and it has already been sent. `count` is how many cards
    the post will carry if you tap Post now — one is an ordinary image post,
    more is a carousel.
    """
    rows = [[InlineKeyboardButton(
        f"📤 Post {'these ' + str(count) + ' cards' if count > 1 else 'this card'}",
        callback_data="manual:post")]]
    second = []
    if room:
        second.append(InlineKeyboardButton("➕ Add another card",
                                           callback_data="manual:add"))
    second.append(InlineKeyboardButton("🗑 Discard this card",
                                       callback_data="manual:discard"))
    rows.append(second)
    return InlineKeyboardMarkup(rows)


def _batch_keyboard(count: int) -> InlineKeyboardMarkup:
    """For /batch: act on a pile left over from an earlier conversation."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"📤 Post {'all ' + str(count) if count > 1 else 'it'} now",
            callback_data="batch:post")],
        [InlineKeyboardButton("➕ Add another card", callback_data="batch:add"),
         InlineKeyboardButton("🗑 Clear", callback_data="batch:clear")],
    ])


def _owner(update: Update) -> str:
    """Whose pile this is. Two people on one bot build separate posts."""
    return str(update.effective_user.id)


def _manual(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """This user's in-progress manual match, created on first use."""
    return context.user_data.setdefault('manual', {})


def _discard_render(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete the rendered PNG this flow left in output/, if any."""
    path = _manual(context).pop('render_path', None)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            log.debug("Could not remove %s: %s", path, e)


async def _reject(msg, error: manual_match.ParseError) -> None:
    """Answer a field that couldn't be read, leaving the state unchanged so the
    next message is another attempt at the same field."""
    await msg.reply_text(str(error))


async def _card_intro(msg, context: ContextTypes.DEFAULT_TYPE,
                      owner: str) -> int:
    """Clear the decks and ask for the first field.

    Takes a message rather than an Update because both "Add another card" and
    the /batch button re-enter here from a callback, where the only message in
    hand was sent by the bot — and an Update built around that would carry the
    bot as its user.
    """
    _discard_render(context)
    # The photo flow's leftovers, if a tap landed here mid-upload: its state is
    # unreachable from now on, and a stale match would silently attach the next
    # photo to the wrong fixture.
    for key in ('manual', 'match', 'event_type', 'pending'):
        context.user_data.pop(key, None)
    _manual(context)['owner'] = owner

    await _clear_keyboard(msg, context)

    # Say up front if this card is joining others — the pile outlives the
    # conversation, so it is entirely possible to have forgotten about it.
    waiting = await asyncio.to_thread(_card_batch().pending, owner)
    joining = (f"\n\nThis will be card {len(waiting) + 1} of a post that "
               f"already has {len(waiting)} — /batch to see them."
               if waiting else "")

    await msg.reply_text(
        "*Manual match card* — for a game the scraper doesn't cover.\n\n"
        "I'll ask for one thing at a time, then send you the card. Nothing is "
        f"posted unless you tap Post at the end.{joining}\n\n"
        "/cancel stops at any point.\n\n"
        "First: what's the *home team*?\n"
        f"_Just the name, as it should read on the card — up to "
        f"{manual_match.MAX_TEAM_NAME} characters. e.g. Arsenal_",
        parse_mode='Markdown',
        reply_markup=_typed_reply('Arsenal'),
    )
    return M_HOME


async def card_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _allowed(update):
        await update.message.reply_text("Not authorized.")
        return ConversationHandler.END
    return await _card_intro(update.message, context, _owner(update))


async def card_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        home = manual_match.parse_team(update.message.text)
    except manual_match.ParseError as e:
        await _reject(update.message, e)
        return M_HOME

    _manual(context)['home_team'] = home
    # Before the next question, not at render time: this is the last moment the
    # name that caused a missing crest is still the thing being talked about.
    asked = await _resolve_badge(update.message, context, 'home', home)
    return asked if asked is not None else await _after_badge(
        update.message, context, 'home')


async def card_away(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        away = manual_match.parse_team(update.message.text)
    except manual_match.ParseError as e:
        await _reject(update.message, e)
        return M_AWAY

    data = _manual(context)
    data['away_team'] = away
    asked = await _resolve_badge(update.message, context, 'away', away)
    return asked if asked is not None else await _after_badge(
        update.message, context, 'away')


async def _ask_moment(msg, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 3. Its own function because the badge question can land in front of
    it, and the answer to that has to arrive here rather than at step 2."""
    data = _manual(context)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Half Time", callback_data="manual:evt:HT"),
        InlineKeyboardButton("Full Time", callback_data="manual:evt:FT"),
    ]])
    await msg.reply_text(
        f"{data['home_team']} vs {data['away_team']}.\n\n"
        f"Is this a half-time or a full-time card?",
        reply_markup=keyboard,
    )
    return M_EVENT


async def card_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _manual(context)
    data['event_type'] = query.data.rsplit(':', 1)[1]
    moment = 'Half time' if data['event_type'] == 'HT' else 'Full time'

    await query.edit_message_text(f"{moment} it is.")
    await query.message.reply_text(
        f"*{moment} score* — {data['home_team']} first.\n\n"
        f"_Two numbers with a dash between them: `2-1`, `0-0`. "
        f"`2:1` and `2 – 1` work too._",
        parse_mode='Markdown',
        reply_markup=_typed_reply('2-1'),
    )
    return M_SCORE


async def card_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        home, away = manual_match.parse_score(update.message.text)
    except manual_match.ParseError as e:
        await _reject(update.message, e)
        return M_SCORE

    data = _manual(context)
    data['home_score'], data['away_score'] = home, away
    await update.message.reply_text(
        "Which *competition*?\n\n"
        "_The name as it should read on the card: `Premier League`, "
        "`Champions League`, `Club Friendly`._",
        parse_mode='Markdown',
        reply_markup=_typed_reply('Premier League'),
    )
    return M_COMPETITION


async def card_competition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        competition = manual_match.parse_competition(update.message.text)
    except manual_match.ParseError as e:
        await _reject(update.message, e)
        return M_COMPETITION

    _manual(context)['competition'] = competition
    asked = await _resolve_badge(update.message, context, 'competition',
                                 competition)
    return asked if asked is not None else await _after_badge(
        update.message, context, 'competition')


async def _ask_date(msg) -> int:
    """Step 6, split out for the same reason as _ask_moment."""
    await msg.reply_text(
        "What *date* was it played?\n\n"
        "_`21/08/2026` (day/month/year), `2026-08-21` or `21 Aug 2026`. "
        "Day comes before month. This is what goes in the card's top-right "
        "corner._",
        parse_mode='Markdown',
        reply_markup=_typed_reply('21/08/2026'),
    )
    return M_DATE


async def card_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        when = manual_match.parse_date(update.message.text)
    except manual_match.ParseError as e:
        await _reject(update.message, e)
        return M_DATE

    data = _manual(context)
    data['when'] = when
    await update.message.reply_text(f"{manual_match.format_card_date(when)}.")
    return await _ask_scorers(update.message, context, 'home')


async def _ask_background(msg) -> int:
    """Step 9, split out because the scorer steps can reach it from a button
    as well as from a typed list."""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Use the standard template",
                             callback_data="manual:nobg")
    ]])
    await msg.reply_text(
        "Last thing: send the *background photo*.\n\n"
        "As a file/document keeps full quality; under 20 MB either way. "
        "Or skip it and the card goes on the usual template.",
        parse_mode='Markdown',
        reply_markup=keyboard,
    )
    return M_BACKGROUND


# ── Scorers: asked with an exit, and counted against the score ────────────────
# The count check runs here rather than only at the preview because here is
# where it can still be acted on cheaply: the list is the last thing typed, and
# fixing it is one message. At the preview it is three steps back and the
# natural response to a warning is to post anyway.
#
# Red cards and missed penalties are deliberately not counted — they are not
# goals, and a card shown to a team that didn't score is perfectly normal.

async def _ask_scorers(msg, context: ContextTypes.DEFAULT_TYPE, side: str) -> int:
    """Ask one team's goals and cards, tailored to whether they scored."""
    data = _manual(context)
    data['scorer_side'] = side
    team = data[f'{side}_team']
    scored = int(data[f'{side}_score'])

    if scored == 0:
        lead = (f"*{team}* didn't score, so there are no goals to enter.\n\n"
                f"If anyone was sent off or missed a penalty, add it — "
                f"otherwise tap the button.")
    else:
        lead = (f"*{team}* — the {scored} goal{'s' if scored != 1 else ''}, "
                f"plus any red cards or missed penalties.")
    if side == 'away':
        lead += ("\n\nOwn goals go in the column you want them to *appear* "
                 "in, which is normally the team they counted for.")

    await msg.reply_text(f"{lead}\n\n{SCORER_HELP}", parse_mode='Markdown',
                         reply_markup=SCORERS_NONE_KEYBOARD)
    return SCORER_STATE[side]


async def _after_scorers(msg, context: ContextTypes.DEFAULT_TYPE, side: str) -> int:
    """Whatever comes after this team's list is settled."""
    if side == 'home':
        return await _ask_scorers(msg, context, 'away')
    return await _ask_background(msg)


async def _scorers_entered(msg, context: ContextTypes.DEFAULT_TYPE,
                           side: str, events: list[dict]) -> int:
    """Store one team's events, then count the goals against the score."""
    data = _manual(context)
    data[f'{side}_events'] = events
    team = data[f'{side}_team']
    goals = manual_match.goal_count(events, team)
    expected = int(data[f'{side}_score'])

    if goals == expected:
        return await _after_scorers(msg, context, side)

    # A mismatch is nearly always a typo, but not always: a scorer can be
    # genuinely unknown, and a card can legitimately show fewer names than
    # goals. So it asks rather than refuses.
    if goals < expected:
        gap = expected - goals
        detail = (f"you've given me {goals}, which is {gap} short. A goal "
                  f"missing from the list, or a minute that didn't parse?")
    else:
        detail = (f"you've given me {goals}, which is more than that. An extra "
                  f"line, or something marked as a goal that wasn't?")

    counted = '\n'.join(
        f"• {e['minute']} {e['player']}" for e in events
        if e['type'] in manual_match.GOAL_TYPES) or '• nothing'

    await msg.reply_text(
        f"⚠️ The score says *{team}* scored {expected}, but {detail}\n\n"
        f"Goals I counted:\n{counted}\n\n"
        f"_Red cards and missed penalties aren't counted — only goals, "
        f"penalties and own goals entered in this column._\n\n"
        f"Send the list again to replace it, or use the buttons.",
        parse_mode='Markdown',
        reply_markup=SCORERS_MISMATCH_KEYBOARD,
    )
    return SCORER_STATE[side]


async def _scorers_text(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        side: str) -> int:
    data = _manual(context)
    try:
        events = manual_match.parse_scorers(update.message.text,
                                            data[f'{side}_team'])
    except manual_match.ParseError as e:
        await _reject(update.message, e)
        return SCORER_STATE[side]
    return await _scorers_entered(update.message, context, side, events)


async def card_home_scorers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _scorers_text(update, context, 'home')


async def card_away_scorers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _scorers_text(update, context, 'away')


async def scorers_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The buttons on a scorer step: nothing to add, accept, or redo."""
    query = update.callback_query
    await query.answer()
    data = _manual(context)
    side = data.get('scorer_side', 'home')
    action = query.data.rsplit(':', 1)[1]

    if action == 'none':
        await query.edit_message_text("Nothing to add.")
        # Still counted: tapping this when the score says two goals is exactly
        # the mistake worth catching.
        return await _scorers_entered(query.message, context, side, [])

    if action == 'redo':
        await query.edit_message_text("Send the list again.")
        return await _ask_scorers(query.message, context, side)

    await query.edit_message_text("Taking your word for it.")
    return await _after_scorers(query.message, context, side)


# ── Badges: the check that has to happen while the name can still be fixed ────
# A scraped fixture gets its crests guaranteed before kickoff by
# validate_matches.py. A typed-in name has no such guarantee, and a missing
# crest is not a small thing — it is a blank hole in the middle of the card,
# discovered at step 10 when the name that caused it is eight messages back.
#
# So the same check runs here, against the same functions, the moment a name is
# entered. Nearly always it is silent: the crest is already on Cloudinary, or
# logo_fetch finds and uploads it. It only becomes a question when the name
# genuinely can't be resolved, which is exactly when a human is needed.

# Which step to go back to when the spelling turns out to be the problem.
BADGE_RETRY = {
    'home':        M_HOME,
    'away':        M_AWAY,
    'competition': M_COMPETITION,
}

BADGE_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("✏️ Let me fix the spelling",
                          callback_data="badge:respell")],
    [InlineKeyboardButton("➡️ Carry on without it",
                          callback_data="badge:skip")],
])


def _check_crest(name: str) -> tuple[str, str | None, str | None]:
    """(official name, crest url, problem). Blocking — call in a thread.

    The same three steps validate_matches.py takes for a fixture, in the same
    order: resolve the spelling against the badge index, fetch and upload the
    crest if it isn't on Cloudinary yet, and fall back to whatever is already
    there for a team the index has never heard of but someone uploaded by hand.
    """
    from config import get_crest_url
    from logo_fetch import fetch_logo, normalize_team_name

    try:
        official = normalize_team_name(name)
    except LookupError as e:
        # Not in the index. A hand-uploaded crest still counts as a crest.
        url = get_crest_url(name, alert=False)
        return name, url, None if url else str(e)

    try:
        # Idempotent: returns immediately when the crest is already up.
        return official, fetch_logo(official), None
    except Exception as e:
        url = get_crest_url(official, alert=False)
        return official, url, None if url else str(e)


def _check_competition_logo(name: str) -> tuple[str | None, str | None]:
    """(logo url, problem). Blocking — call in a thread.

    Milder than a crest: a competition with no badge falls back to the 130
    Yards mark, so the card is never holed. Still worth saying, because the
    fallback is silent and looks deliberate.
    """
    from config import get_brand_logo_url, get_competition_logo_url
    from logo_fetch import fetch_competition_logo, resolve_competition

    url = get_competition_logo_url(name, alert=False)
    if url and url != get_brand_logo_url():
        return url, None

    if resolve_competition(name):
        try:
            fetched = fetch_competition_logo(name)
            if fetched:
                return fetched, None
        except Exception as e:
            return None, str(e)
    return None, 'no badge saved for this competition'


async def _ask_about_badge(msg, context: ContextTypes.DEFAULT_TYPE,
                           target: str, name: str, problem: str) -> int:
    """Stop and ask, rather than rendering a card with a hole in it."""
    data = _manual(context)
    data['badge_target'] = target

    if target == 'competition':
        detail = (f"I don't have a badge for *{name}*, so the card will show "
                  f"the 130 Yards mark where the competition logo goes. That's "
                  f"cosmetic — nothing else changes.")
        spelling = "`Premier League` rather than `EPL`"
    else:
        detail = (f"I can't find a badge for *{name}*, so that side of the card "
                  f"would have a blank space where the crest goes.")
        spelling = "`Tottenham Hotspur` rather than `Spurs`"

    await msg.reply_text(
        f"{detail}\n\n"
        f"Three ways out:\n"
        f"• *Send me a PNG* of the badge now and I'll save it — it'll be there "
        f"for every future card too\n"
        f"• *Fix the spelling* — badges are filed under official names, so try "
        f"{spelling}\n"
        f"• *Carry on without it*\n\n"
        f"_Technical detail: {problem}_",
        parse_mode='Markdown',
        reply_markup=BADGE_KEYBOARD,
    )
    return M_BADGE


async def _after_badge(msg, context: ContextTypes.DEFAULT_TYPE,
                       target: str) -> int:
    """Ask whatever comes after this badge was settled.

    One place, because there are five ways to arrive here — the badge was
    already fine, it was fetched, it was uploaded, it was skipped, or the name
    was respelled and passed the second time — and all five owe the same
    question next.
    """
    if target == 'home':
        await msg.reply_text(
            "And the *away team*?\n_Same again — the name only._",
            parse_mode='Markdown', reply_markup=_typed_reply('Manchester City'))
        return M_AWAY
    if target == 'away':
        return await _ask_moment(msg, context)
    return await _ask_date(msg)


async def _resolve_badge(msg, context: ContextTypes.DEFAULT_TYPE,
                         target: str, name: str) -> int | None:
    """Check one badge. Returns M_BADGE if it had to ask, None if it's settled
    and the caller should carry on."""
    note = await msg.reply_text("Checking the badge…")

    if target == 'competition':
        _url, problem = await asyncio.to_thread(_check_competition_logo, name)
        official = name
    else:
        official, _url, problem = await asyncio.to_thread(_check_crest, name)

    if problem:
        return await _ask_about_badge(msg, context, target, name, problem)

    # Badges are filed under official names, so resolution doubles as a
    # spellchecker — and the official spelling is what should go on the card,
    # exactly as validate_matches.py rewrites matches.json.
    data = _manual(context)
    if target != 'competition' and official != name:
        data[f'{target}_team'] = official
        await note.reply_text(
            f"Found it — filed as *{official}*, so that's what goes on the "
            f"card.", parse_mode='Markdown')
    return None


async def badge_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The two buttons on the badge question."""
    query = update.callback_query
    await query.answer()
    data = _manual(context)
    target = data.get('badge_target', 'home')

    if query.data.endswith('respell'):
        await query.edit_message_text("Send it again, spelled differently.")
        prompt = {'home': 'The *home team*, then.',
                  'away': 'The *away team*, then.',
                  'competition': 'The *competition*, then.'}[target]
        await query.message.reply_text(
            f"{prompt}\n_Official names work best — `Tottenham Hotspur` "
            f"rather than `Spurs`._", parse_mode='Markdown',
            reply_markup=_typed_reply('Tottenham Hotspur'))
        return BADGE_RETRY[target]

    await query.edit_message_text(
        "Carrying on without it."
        if target == 'competition' else
        "Carrying on — that crest will be a blank space.")
    return await _after_badge(query.message, context, target)


async def badge_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A badge sent as a photo or file, saved where every renderer looks."""
    msg = update.message
    data = _manual(context)
    target = data.get('badge_target', 'home')
    name = (data['competition'] if target == 'competition'
            else data[f'{target}_team'])

    ref = _photo_ref(msg)
    if ref is None:
        await msg.reply_text(
            "That file isn't an image — send the badge as a PNG, or use the "
            "buttons above.")
        return M_BADGE

    file_id, file_size = ref
    if file_size and file_size > TELEGRAM_FILE_LIMIT_BYTES:
        await msg.reply_text("That's over Telegram's 20 MB limit for a bot — "
                             "a badge should be a few hundred KB.")
        return M_BADGE

    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
        tmp_path = tmp.name
    try:
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(tmp_path)
        url, saved = await asyncio.to_thread(_save_badge, tmp_path, name, target)
    except Exception as e:
        log.exception("Badge upload failed for %s", name)
        await msg.reply_text(
            f"I couldn't save that badge: {e}\n\n"
            f"Send a different file, or use the buttons above.")
        return M_BADGE
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    await msg.reply_text(
        (f"Saved ✅ *{name}* has a badge now — this card and every future one."
         if saved else
         f"There was already a badge filed under *{name}*, so I've kept that "
         f"one rather than overwriting it — the card won't be missing anything.")
        + f"\n{url}", parse_mode='Markdown')
    return await _after_badge(msg, context, target)


def _save_badge(path: str, name: str, target: str) -> tuple[str, bool]:
    """Upload a badge to the public_id every renderer already looks at.

    Returns (url, replaced_nothing). Blocking — call in a thread.

    Never forced. upload_local_crest refuses when a badge already sits at the
    public_id the name resolves to, and that refusal is worth honouring even
    here: the name lookup that sent us to this question searches a different
    table from the one that decides where an uploaded file lands, so a name it
    could not resolve can still slugify onto a real club's crest. Overwriting
    that would break every future card for a team nobody was even talking
    about. If something is already there, it is a better badge than the one
    being offered — say so and use it.
    """
    from logo_fetch import upload_local_crest
    kind = 'competition' if target == 'competition' else None
    try:
        return upload_local_crest(path, name, kind=kind), True
    except RuntimeError:
        from config import get_competition_logo_url, get_crest_url
        existing = (get_competition_logo_url(name, alert=False)
                    if kind == 'competition' else get_crest_url(name, alert=False))
        if existing:
            return existing, False
        raise


# ── Rendering ─────────────────────────────────────────────────────────────────

def _render_manual(data: dict, photo_path: str | None) -> tuple[str, dict]:
    """Build the scraper_data dict and render the card. Blocking — call in a
    thread. Returns (image path, scraper_data).

    Both renderers are the pipeline's own, called exactly as a match worker
    calls them; the only difference is where the dict came from. home_name and
    away_name are deliberately left unset so TEAM_NAME_ALIASES still normalises
    the typed-in spelling for the display name and the crest lookup, while the
    events keep matching on what was actually typed.
    """
    from scorecard import generate_scorecard

    scraper_data = manual_match.build_scraper_data(
        home_team=data['home_team'], away_team=data['away_team'],
        home_score=data['home_score'], away_score=data['away_score'],
        competition=data['competition'], when=data['when'],
        event_type=data['event_type'],
        home_events=data.get('home_events', []),
        away_events=data.get('away_events', []),
    )
    match_id = scraper_data['matchSample']['match_id']

    if photo_path:
        from overlay_scorebar import generate_overlay_scorecard
        path = generate_overlay_scorecard(
            scraper_data, photo_path, event_type=data['event_type'],
            match_id_override=match_id, competition=data['competition'])
    else:
        path = generate_scorecard(
            scraper_data, event_type=data['event_type'],
            match_id_override=match_id, competition=data['competition'])
    return path, scraper_data


def _summary(data: dict, scraper_data: dict, styled: bool,
             waiting: list[dict]) -> str:
    """The line-by-line readback that sits under the preview.

    It repeats what was typed rather than describing the picture: the picture
    is right there, and what is worth double-checking before this goes to a
    public account is whether the bot understood the input.
    """
    events = scraper_data['events']
    goals_home = manual_match.goal_count(events, data['home_team'])
    goals_away = manual_match.goal_count(events, data['away_team'])

    lines = [
        f"*{data['home_team']} {data['home_score']}–{data['away_score']} {data['away_team']}*",
        f"{data['competition']} · "
        f"{'Half time' if data['event_type'] == 'HT' else 'Full time'} · "
        f"{manual_match.format_card_date(data['when'])}",
        f"Background: {'your photo' if styled else 'standard template'}",
    ]

    # A goal count that disagrees with the scoreline is nearly always a typo in
    # one or the other. It is not an error — a card can legitimately show fewer
    # scorers than goals — so it warns and lets the card through.
    if (goals_home, goals_away) != (int(data['home_score']), int(data['away_score'])):
        lines.append(
            f"\n⚠️ The scorers add up to {goals_home}–{goals_away}, not "
            f"{data['home_score']}–{data['away_score']}. Worth a look before posting."
        )

    if waiting:
        lines.append(
            f"\nThis post would be a *carousel of {len(waiting) + 1}*, "
            f"in this order:\n" + _card_batch().describe(waiting)
            + f"\n{len(waiting) + 1}. this one"
        )
    else:
        lines.append("\nNothing is posted unless you tap Post.")
    return '\n'.join(lines)


async def _preview(msg, context: ContextTypes.DEFAULT_TYPE,
                   photo_path: str | None) -> int:
    """Render, show the card, and ask what to do with it."""
    data = _manual(context)
    await msg.reply_text("Building the card…")

    try:
        path, scraper_data = await asyncio.to_thread(_render_manual, data, photo_path)
    except Exception as e:
        log.exception("Manual card render failed")
        await msg.reply_text(
            f"The card couldn't be rendered: {e}\n\n"
            f"Nothing has been posted. Send a background photo to try again, "
            f"/card to start over, or /cancel."
        )
        return M_BACKGROUND
    finally:
        if photo_path and os.path.exists(photo_path):
            os.remove(photo_path)

    data['render_path'] = path
    data['scraper_data'] = scraper_data

    # Both, on purpose. The photo is what you look at; the document is the
    # actual PNG, uncompressed, because the card is the deliverable here and
    # posting is optional — this flow has to be useful to someone who never
    # taps Post at all.
    with open(path, 'rb') as fh:
        await msg.reply_photo(fh)
    with open(path, 'rb') as fh:
        await msg.reply_document(fh, filename=os.path.basename(path))

    waiting = await asyncio.to_thread(_pending_cards, context)
    count = len(waiting) + 1
    await msg.reply_text(
        _summary(data, scraper_data, styled=bool(photo_path), waiting=waiting),
        parse_mode='Markdown',
        reply_markup=_confirm_keyboard(count, room=count < _MAX_CARDS),
    )
    return M_CONFIRM


async def card_background(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    ref = _photo_ref(msg)
    if ref is None:
        await msg.reply_text(
            "That file isn't an image — send a JPG/PNG, or tap the button to "
            "use the standard template."
        )
        return M_BACKGROUND

    file_id, file_size = ref
    if file_size and file_size > TELEGRAM_FILE_LIMIT_BYTES:
        await msg.reply_text(
            f"That image is {file_size / 1e6:.0f} MB, and Telegram won't let "
            f"the bot download anything over 20 MB.\n\n"
            f"Send it as a normal photo instead of a file, or export it a bit "
            f"smaller."
        )
        return M_BACKGROUND

    with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
        tmp_path = tmp.name
    try:
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(tmp_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        log.exception("Background download failed")
        await msg.reply_text(f"I couldn't download that: {e}\n\nTry again, or /cancel.")
        return M_BACKGROUND

    # Same ceiling as an uploaded match photo — the overlay renders at the
    # photo's own resolution, and Instagram serves it at 1080px wide anyway.
    await asyncio.to_thread(_shrink_for_upload, tmp_path)
    return await _preview(msg, context, tmp_path)


async def card_no_background(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Using the standard template.")
    return await _preview(query.message, context, None)


# ── The batch ─────────────────────────────────────────────────────────────────
# A card is never posted on its own initiative. It goes on a pile, and the pile
# is published when you say it is complete — one card as an ordinary post,
# several as a carousel. See card_batch.py for why the pile lives on Cloudinary
# rather than in this process.
#
# card_batch is imported lazily, like the posting stack below it, so the bot
# still starts on a runner without the Instagram or Gemini credentials.

# Read at import time only for the ceiling, which is Instagram's and fixed.
_MAX_CARDS = 10


def _card_batch():
    import card_batch
    return card_batch


def _pending_cards(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    """This owner's waiting cards. Blocking — call in a thread."""
    owner = _manual(context).get('owner')
    return _card_batch().pending(owner) if owner else []


def _keep_card(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Move the rendered card onto the pile. Blocking — call in a thread."""
    data = _manual(context)
    return _card_batch().add(
        data['owner'], data['render_path'], data['scraper_data'],
        data['event_type'], data['competition'])


# ── Posting ───────────────────────────────────────────────────────────────────

def _post_batch(cards: list[dict], theme: str | None = None) -> tuple[str, str | None]:
    """Publish the pile. Blocking — call in a thread.

    One card is an ordinary image post captioned like any other match; two or
    more is a carousel captioned by the multi-match writer, which is told not
    to state a scoreline precisely because the cards already carry them.

    Imported here rather than at module scope so the bot still starts, and
    still takes photos, on a runner that has no Instagram or Gemini
    credentials — which is exactly how telegram_bot.yml was configured before
    this flow existed.
    """
    from caption import generate_caption, generate_group_caption
    from instagram import (get_post_permalink, post_carousel_to_instagram,
                           post_to_instagram)

    urls = [c['image_url'] for c in cards]
    if len(cards) == 1:
        # A single card is an ordinary match post, and the ordinary caption
        # already knows what the match was. A theme has nothing to add.
        card = cards[0]
        caption = generate_caption(card['scraper_data'],
                                   event_type=card.get('event_type', 'FT'),
                                   competition=card.get('competition'))
        media_id = post_to_instagram(urls[0], caption)
    else:
        media_id = post_carousel_to_instagram(
            urls, generate_group_caption(cards, theme=theme))
    return media_id, get_post_permalink(media_id)


async def _publish(msg, context: ContextTypes.DEFAULT_TYPE,
                   cards: list[dict], owner: str,
                   theme: str | None = None) -> None:
    """Post the pile and clear it. Raises nothing — it answers in the chat."""
    await msg.reply_text(
        f"Posting {'a carousel of ' + str(len(cards)) if len(cards) > 1 else 'it'}…")
    try:
        media_id, permalink = await asyncio.to_thread(_post_batch, cards, theme)
    except Exception as e:
        log.exception("Manual card post failed")
        await msg.reply_text(
            f"The post failed: {e}\n\n"
            f"Every card is still saved — /batch to see them and try again.",
        )
        return

    await asyncio.to_thread(_card_batch().clear, owner)
    await msg.reply_text(
        f"Posted ✅\n{permalink or f'media id {media_id}'}\n\n"
        f"/card to start another post."
    )


# ── The theme: what a carousel is *about* ─────────────────────────────────────
# Asked at post time and nowhere earlier, because it is the one thing you can
# only answer once the set is complete. A pile of results tells the caption
# writer that some matches happened; it cannot tell it that these are Arsenal's
# last five. Without that the model writes the matchday post the scraped
# carousel_group flow wants, which is the wrong post for a retrospective.
#
# Single-card posts never ask: the ordinary match caption already knows what
# the match was.

SKIP_THEME_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("Skip — just post it", callback_data="theme:skip")],
])


async def _ask_theme(msg, context: ContextTypes.DEFAULT_TYPE,
                     cards: list[dict], owner: str) -> int:
    """Last question before a carousel goes out."""
    data = _manual(context)
    data['pending_post'] = {'owner': owner, 'count': len(cards)}
    await msg.reply_text(
        f"Last thing — *what is this post?*\n\n"
        f"One line, in your words: `Arsenal's last five`, "
        f"`Every North London derby since 2020`, `Matchweek 3`.\n\n"
        f"_The caption writer sees {len(cards)} results and nothing else, so "
        f"without this it writes a matchday round-up. Skip and you'll get "
        f"exactly that._",
        parse_mode='Markdown',
        reply_markup=SKIP_THEME_KEYBOARD,
    )
    return M_THEME


async def _publish_pending(msg, context: ContextTypes.DEFAULT_TYPE,
                           theme: str | None) -> int:
    """Post the pile the theme question was asked about.

    The cards are re-read rather than carried through the question: it is a
    round trip to a human, and the pile is the authority on what is in the post.
    """
    pending = _manual(context).pop('pending_post', None)
    if not pending:
        await msg.reply_text("That post is gone — /batch to see what's waiting.")
        return ConversationHandler.END

    owner = pending['owner']
    cards = await asyncio.to_thread(_card_batch().pending, owner)
    if not cards:
        await msg.reply_text("That pile is already empty — nothing was posted.")
        return ConversationHandler.END

    context.user_data.pop('manual', None)
    await _publish(msg, context, cards, owner, theme)
    return ConversationHandler.END


async def card_theme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The typed answer to the theme question."""
    try:
        theme = manual_match.parse_theme(update.message.text)
    except manual_match.ParseError as e:
        await _reject(update.message, e)
        return M_THEME
    return await _publish_pending(update.message, context, theme)


async def theme_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("No framing, then — a straight round-up.")
    return await _publish_pending(query.message, context, None)


async def card_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data.rsplit(':', 1)[1]
    data = _manual(context)

    if action == 'discard':
        _discard_render(context)
        context.user_data.pop('manual', None)
        await query.edit_message_text(
            "Dropped that card. Anything already on the pile is untouched — "
            "/batch to see it.")
        return ConversationHandler.END

    path = data.get('render_path')
    if not data.get('scraper_data') or not path or not os.path.exists(path):
        # Only reachable if the process restarted between preview and tap.
        await query.edit_message_text(
            "That card is gone — the bot restarted since it was built. "
            "/card to do it again; anything already saved is still on the pile.")
        context.user_data.pop('manual', None)
        return ConversationHandler.END

    # Both remaining paths keep the card, so it is saved before either acts:
    # a failure here must not be discovered after the render has been deleted.
    await query.edit_message_text("Saving this card…")
    try:
        manifest = await asyncio.to_thread(_keep_card, context)
    except Exception as e:
        log.exception("Could not save a manual card to the batch")
        await query.message.reply_text(
            f"I couldn't save that card: {e}\n\n"
            f"It hasn't been added and nothing was posted. Tap again to retry.",
            reply_markup=_confirm_keyboard(1, room=True),
        )
        return M_CONFIRM

    _discard_render(context)
    owner = data['owner']

    if action == 'add':
        context.user_data.pop('manual', None)
        await query.message.reply_text(
            f"Card {manifest['seq']} saved. Nothing has been posted.")
        return await _card_intro(query.message, context, owner)

    cards = await asyncio.to_thread(_card_batch().pending, owner)
    if len(cards) > 1:
        return await _ask_theme(query.message, context, cards, owner)
    context.user_data.pop('manual', None)
    await _publish(query.message, context, cards, owner)
    return ConversationHandler.END


# ── /batch — the pile, outside any conversation ───────────────────────────────

async def batch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """What is waiting to be posted.

    Its own command rather than a step in the flow, because the case it exists
    for is the conversation having ended without you — the bot shut down, or
    you walked away — and the cards outliving it.
    """
    if not _allowed(update):
        await update.message.reply_text("Not authorized.")
        return
    await _clear_keyboard(update.message, context)

    owner = _owner(update)
    cards = await asyncio.to_thread(_card_batch().pending, owner)
    if not cards:
        await update.message.reply_text("Nothing waiting. /card builds one.")
        return

    await update.message.reply_text(
        f"*{len(cards)} card{'s' if len(cards) != 1 else ''} waiting* — this "
        f"would post as {'a carousel' if len(cards) > 1 else 'a single image'}, "
        f"in this order:\n\n" + _card_batch().describe(cards),
        parse_mode='Markdown',
        reply_markup=_batch_keyboard(len(cards)),
    )


async def batch_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/batch's "Add another card" — an entry point of the card conversation.

    It has to be one: only a ConversationHandler can put someone into a state,
    so a plain callback handler could ask the first question and then have
    nowhere to put the answer.
    """
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return ConversationHandler.END
    await query.edit_message_text("Adding another card.")
    return await _card_intro(query.message, context, _owner(update))


async def batch_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/batch's Clear button. The only one that needs no conversation."""
    query = update.callback_query
    await query.answer()
    owner = _owner(update)
    removed = await asyncio.to_thread(_card_batch().clear, owner)
    await query.edit_message_text(
        f"Cleared {removed} card{'s' if removed != 1 else ''}. "
        f"Nothing was posted.")


async def batch_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/batch's Post button — an entry point of the card conversation.

    It has to be one for the same reason "Add another card" does: a carousel is
    asked for its theme first, and only a conversation handler can hold the
    answer.
    """
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return ConversationHandler.END

    owner = _owner(update)
    cards = await asyncio.to_thread(_card_batch().pending, owner)
    if not cards:
        await query.edit_message_text("That pile is already empty.")
        return ConversationHandler.END

    _manual(context)['owner'] = owner
    if len(cards) > 1:
        await query.edit_message_text(f"{len(cards)} cards, then.")
        return await _ask_theme(query.message, context, cards, owner)

    await query.edit_message_text("Posting it…")
    context.user_data.pop('manual', None)
    await _publish(query.message, context, cards, owner)
    return ConversationHandler.END


async def card_wrong_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Something that isn't what the current step asked for.

    Returns None, which leaves the conversation in the state it was in — the
    step simply asks again rather than being abandoned halfway through eight
    fields of typing.
    """
    if update.message:
        await update.message.reply_text(
            "That isn't what this step needs — send the answer as a normal "
            "message, or /cancel to stop."
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Only what is in progress. Cards already saved to the pile are not part of
    # "whatever is in progress", and silently binning several matches' worth of
    # typing would be the worst possible reading of a Cancel button — /batch
    # clears them, deliberately and on its own.
    _discard_render(context)
    context.user_data.clear()
    await _clear_keyboard(update.message, context)
    await update.message.reply_text("Cancelled. /start to begin again.")
    return ConversationHandler.END


async def stray_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Anything that reached no other handler. Answer, don't ignore.

    Except while a step is asking for typed text — /card throughout, and the
    player's name for an event photo. Handlers in different groups every get a
    turn at the same update, so while either is running this one sees every
    answer to every question — and used to reply "typed messages don't do
    anything here" to a perfectly good team name, immediately after the
    conversation had accepted it. That message is true everywhere else and
    wrong here, and being told you are typing nonsense while correctly
    answering a question is worse than not being answered at all.

    The conversation's own handlers cover every message while it is live: a
    valid answer advances, an invalid one is rejected with a reason, and
    anything else re-asks the step (card_wrong_input). Nothing gets ignored by
    stepping aside.
    """
    if not _allowed(update) or not update.message:
        return
    if context.user_data.get('manual') is not None:
        return
    if context.user_data.get('awaiting_player'):
        return          # same reason: the step asked for exactly this text
    await _clear_keyboard(update.message, context)
    await update.message.reply_text(
        "Typed messages don't do anything here — I only act on a photo or a "
        "command.\n\nSend a match photo, or open ☰ next to the message box "
        "for everything I do. /help explains each one."
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last line of defence: a crashed handler must still say something.

    Without this, python-telegram-bot logs the traceback into the Actions log
    and replies nothing — which looks exactly like a bot that ignored you.
    """
    log.error("Handler raised", exc_info=context.error)
    traceback.print_exception(type(context.error), context.error,
                              context.error.__traceback__)
    msg = getattr(update, 'effective_message', None)
    if msg is None:
        return
    try:
        await msg.reply_text(
            f"Something went wrong handling that: {context.error}\n\n"
            f"Try again, or /start to begin from the match list."
        )
    except Exception:
        pass   # the reply itself failing must not re-enter the error handler


async def post_init(app: Application) -> None:
    """Publish the command list to Telegram so the client offers it.

    This is what turns the commands into a menu: the ☰ button next to the
    message box lists them, and typing "/" autocompletes from the same list.
    Best-effort — a bot that can't register its menu should still run.
    """
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as e:
        log.warning("Could not publish the command menu: %s", e)


def main() -> None:
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing from .env — create a bot with @BotFather first.")

    app = ApplicationBuilder().token(token).post_init(post_init).build()

    # Fallbacks both conversations share: /cancel ends whatever is running,
    # /help answers without disturbing the state it was in (returns None), and
    # /batch reaches the pile from anywhere.
    common_fallbacks = [
        CommandHandler('cancel', cancel),
        CommandHandler('help', help_cmd),
        CommandHandler('batch', batch_cmd),
    ]

    # Registered before the photo conversation so /card and the background
    # photo it is waiting for reach this one first. Its entry points only match
    # /card, so a photo sent outside the flow still lands in photo_first.
    card_conv = ConversationHandler(
        entry_points=[
            CommandHandler('card', card_start),
            CallbackQueryHandler(batch_add, pattern=r'^batch:add$'),
            CallbackQueryHandler(batch_post, pattern=r'^batch:post$'),
        ],
        states={
            M_HOME:         [MessageHandler(MANUAL_TEXT_FILTER, card_home)],
            M_AWAY:         [MessageHandler(MANUAL_TEXT_FILTER, card_away)],
            # A badge can be answered with a file as well as a button, so this
            # state takes both — the same shape as the background step.
            M_BADGE:        [
                MessageHandler(PHOTO_ENTRY_FILTER, badge_upload),
                CallbackQueryHandler(badge_choice,
                                     pattern=r'^badge:(respell|skip)$'),
            ],
            M_EVENT:        [CallbackQueryHandler(card_event, pattern=r'^manual:evt:')],
            M_SCORE:        [MessageHandler(MANUAL_TEXT_FILTER, card_score)],
            M_COMPETITION:  [MessageHandler(MANUAL_TEXT_FILTER, card_competition)],
            M_DATE:         [MessageHandler(MANUAL_TEXT_FILTER, card_date)],
            M_HOME_SCORERS: [
                MessageHandler(MANUAL_TEXT_FILTER, card_home_scorers),
                CallbackQueryHandler(scorers_choice,
                                     pattern=r'^scorers:(none|ok|redo)$'),
            ],
            M_AWAY_SCORERS: [
                MessageHandler(MANUAL_TEXT_FILTER, card_away_scorers),
                CallbackQueryHandler(scorers_choice,
                                     pattern=r'^scorers:(none|ok|redo)$'),
            ],
            M_BACKGROUND:   [
                MessageHandler(PHOTO_ENTRY_FILTER, card_background),
                CallbackQueryHandler(card_no_background, pattern=r'^manual:nobg$'),
            ],
            M_CONFIRM:      [CallbackQueryHandler(
                card_confirm, pattern=r'^manual:(post|add|discard)$')],
            M_THEME:        [
                MessageHandler(MANUAL_TEXT_FILTER, card_theme),
                CallbackQueryHandler(theme_skip, pattern=r'^theme:skip$'),
            ],
        },
        fallbacks=common_fallbacks + [
            CommandHandler('card', card_start),
            # Last: anything else mid-flow re-asks the current step instead of
            # throwing away everything typed so far.
            MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, card_wrong_input),
        ],
    )

    # Registered before the scorecard conversation for the same reason /card is:
    # this one ends by waiting for a photo, and a bare photo is an entry point
    # of the flow below. Its own entry points only match /event, so a photo
    # sent outside it still lands in photo_first.
    event_conv = ConversationHandler(
        entry_points=[
            CommandHandler('event', event_start),
        ],
        states={
            E_MATCH:  [CallbackQueryHandler(event_match_chosen,
                                            pattern=r'^(ev:match:|cancel$)')],
            E_EVENT:  [CallbackQueryHandler(event_event_chosen,
                                            pattern=r'^(ev:evt:|cancel$)')],
            # The three player steps also take a photo sent early rather than
            # dropping it — see event_photo_early.
            E_TEAM:   [CallbackQueryHandler(event_side_chosen,
                                            pattern=r'^(ev:side:|cancel$)'),
                       MessageHandler(PHOTO_ENTRY_FILTER, event_photo_early)],
            E_PLAYER: [CallbackQueryHandler(event_player_chosen,
                                            pattern=r'^(ev:player:|cancel$)'),
                       MessageHandler(PHOTO_ENTRY_FILTER, event_photo_early)],
            E_TYPE_PLAYER: [MessageHandler(MANUAL_TEXT_FILTER, event_player_typed),
                            MessageHandler(PHOTO_ENTRY_FILTER, event_photo_early)],
            E_PHOTO:  [MessageHandler(PHOTO_ENTRY_FILTER, event_photo_received)],
            # A photo sent at the menu is held, not dropped — the next player
            # picked settles it, exactly as it does at the player steps.
            E_MORE:   [CallbackQueryHandler(event_more_chosen,
                                            pattern=r'^(ev:more:|cancel$)'),
                       MessageHandler(PHOTO_ENTRY_FILTER, event_photo_early)],
        },
        fallbacks=common_fallbacks + [
            CommandHandler('event', event_start),
        ],
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CommandHandler('newphoto', start),
            # A bare photo is a valid way to begin — see photo_first.
            MessageHandler(PHOTO_ENTRY_FILTER, photo_first),
        ],
        states={
            # Patterned, so a tap belonging to something else — a ❌ on a
            # /staged listing — falls through to its own handler instead of
            # being read as an answer to the question on screen.
            SELECT_MATCH: [CallbackQueryHandler(match_chosen,
                                                pattern=r'^(match:|cancel$)')],
            SELECT_EVENT: [CallbackQueryHandler(event_chosen,
                                                pattern=r'^(event:|cancel$)')],
            WAIT_PHOTO: [MessageHandler(filters.PHOTO | filters.Document.ALL, photo_received)],
        },
        # Fallbacks apply in every state, so the commands keep working
        # mid-flow: /start restarts from the match list, and the shared ones
        # above cover /cancel and /help.
        fallbacks=common_fallbacks + [
            CommandHandler('start', start),
            CommandHandler('newphoto', start),
        ],
    )
    app.add_handler(card_conv)
    app.add_handler(event_conv)
    app.add_handler(conv)
    # Outside any conversation these still have to answer.
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CommandHandler('cancel', cancel))
    app.add_handler(CommandHandler('batch', batch_cmd))
    app.add_handler(CommandHandler('list', list_cmd))
    app.add_handler(CallbackQueryHandler(batch_clear, pattern=r'^batch:clear$'))
    # /staged answers outside any conversation and mid-way through one:
    # it is what you reach for when a flow has left something armed by
    # mistake, which is precisely when a flow is in progress.
    app.add_handler(CommandHandler('staged', staged_cmd))
    app.add_handler(CallbackQueryHandler(staged_drop, pattern=r'^sd:'))
    app.add_handler(CallbackQueryHandler(staged_clarify, pattern=r'^ec:'))
    # Group -1 runs first and consumes nothing — see keep_alive.
    app.add_handler(MessageHandler(filters.ALL, keep_alive), group=-1)
    app.add_handler(CallbackQueryHandler(keep_alive), group=-1)
    # Lower-priority group: only sees what the conversation didn't take.
    app.add_handler(MessageHandler(UNKNOWN_COMMAND_FILTER, unknown_command), group=1)
    app.add_handler(MessageHandler(STRAY_FILTER, stray_message), group=1)
    app.add_error_handler(on_error)
    print("[bot] Running. Ctrl+C to stop.")
    # Pending updates are deliberately NOT dropped: the bot is restarted often,
    # and a photo sent while it was down is exactly the one worth keeping.
    app.run_polling()


if __name__ == '__main__':
    main()
