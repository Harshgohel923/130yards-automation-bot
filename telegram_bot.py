# telegram_bot.py — match photo intake bot.
"""
Telegram bot for uploading the base photo used by the scorecard overlay.

Two ways in, because the bot restarts often in production (its watchdog stops
it whenever no match worker is active) and conversation state lives only in
memory:

  /start (or /newphoto)          →  pick match  →  pick HT/FT  →  send photo
  just send the photo            →  pick match  →  pick HT/FT  →  uploaded

A third flow, /card, does something different: it builds a whole match from
typed-in details rather than attaching a photo to a scraped one. It exists for
fixtures the scraper never sees — friendlies, lower divisions, an old game
worth reposting — and it ends by rendering the card, showing it in the chat,
and posting it to Instagram once you say so. See the "Manual match" section
below; the data assembly lives in manual_match.py.

Outside /card, input is a fixed vocabulary, never free text: the commands in
BOT_COMMANDS (published to Telegram so the ☰ menu and "/" autocomplete list
them), the buttons on the persistent keyboard that mirror them, and photos.
Every match and HT/FT choice is an inline button. Anything else — a typed
message, an unknown command — gets the help text back rather than being parsed
or ignored. /card is the sole exception, and only while it is running: it has
to read typed text because nobody can tap a scorer's name into existence.

The second path exists so a photo is never dropped. If the bot restarted
between picking the match and sending the picture, the conversation it was
holding is gone — the photo then belongs to no conversation at all, and the
only thing worse than asking which match it is would be silently ignoring it.

The photo is uploaded to Cloudinary as  match_photos/<match_id>_<HT|FT>
(overwrite=True, so re-sending replaces the previous photo). The public_id is
deterministic, so the pipeline can fetch the photo for a match/event with
fetch_match_photo() — no state is shared beyond that.

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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    ReplyKeyboardMarkup,
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

import manual_match
from cloudinary_utils import photo_public_id

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

# Sending "as a file" keeps full quality but is also how a 25 MB photo arrives.
# Any document, not just image/*: a wrong file should be told so, not ignored.
PHOTO_ENTRY_FILTER = filters.PHOTO | filters.Document.ALL

# ── The fixed input vocabulary ────────────────────────────────────────────────
# Outside /card the bot accepts exactly three things: a photo, one of these
# commands, or one of the buttons below. Everything else gets pointed back at
# them rather than being interpreted — free text has no meaning there.

BOT_COMMANDS = [
    BotCommand('start', 'Pick a match and upload a photo'),
    BotCommand('newphoto', 'Same as /start'),
    BotCommand('card', 'Build a match card from details you type in'),
    BotCommand('batch', 'Cards waiting to be posted together'),
    BotCommand('cancel', 'Abandon whatever is in progress'),
    BotCommand('help', 'What this bot accepts'),
]
KNOWN_COMMANDS = tuple(c.command for c in BOT_COMMANDS)

# Buttons carry the same actions as a persistent keyboard, so the usual case is
# a tap and nothing is ever typed. Their text is matched exactly.
BTN_NEW = '📸 New photo'
BTN_CARD = '🆕 Manual card'
BTN_CANCEL = '🚫 Cancel'
BTN_HELP = '❓ Help'
BUTTON_TEXTS = [BTN_NEW, BTN_CARD, BTN_HELP, BTN_CANCEL]

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_NEW, BTN_CARD], [BTN_HELP, BTN_CANCEL]],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder='Use the buttons below',
)

BTN_NEW_FILTER = filters.Text([BTN_NEW])
BTN_CARD_FILTER = filters.Text([BTN_CARD])
BTN_CANCEL_FILTER = filters.Text([BTN_CANCEL])
BTN_HELP_FILTER = filters.Text([BTN_HELP])
BUTTON_FILTER = filters.Text(BUTTON_TEXTS)

# Free text inside /card. Buttons and commands are excluded so tapping Cancel
# mid-flow cancels instead of being recorded as a team called "🚫 Cancel".
MANUAL_TEXT_FILTER = filters.TEXT & ~filters.COMMAND & ~BUTTON_FILTER

# /foo for any foo the bot doesn't implement. Telegram allows @botname suffixes
# in groups, hence the optional tail.
UNKNOWN_COMMAND_FILTER = filters.COMMAND & ~filters.Regex(
    r'^/(' + '|'.join(KNOWN_COMMANDS) + r')(@\S+)?(\s|$)'
)

HELP_TEXT = (
    "I do two things: take the photo that goes under an automated scorecard, "
    "and build a whole card for a match the scraper doesn't cover.\n\n"
    "*What I accept*\n"
    "• Send a photo — I'll ask which match and whether it's HT or FT\n"
    f"• {BTN_NEW} / /start — pick the match first, then send the photo\n"
    f"• {BTN_CARD} / /card — type in a match yourself: score, scorers, date, "
    "competition, background. I render it and send it to you; posting is a "
    "separate tap that never happens on its own\n"
    "• /batch — the cards waiting to go out as one post, and the button that "
    "posts them\n"
    f"• {BTN_CANCEL} / /cancel — drop whatever is in progress\n"
    f"• {BTN_HELP} / /help — this message\n\n"
    "Outside /card I ignore typed text on purpose. Use the buttons below or "
    "the ☰ menu next to the message box."
)

# What the catch-all below answers: everything the conversation never looks at.
# Handlers in different groups all get a turn at the same update, so this has
# to exclude what group 0 already handles or every photo draws two replies.
# Buttons are excluded for the same reason — group 0 owns them.
STRAY_FILTER = (filters.ALL & ~filters.COMMAND & ~filters.PHOTO
                & ~filters.Document.ALL & ~filters.StatusUpdate.ALL
                & ~BUTTON_FILTER)


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


def _match_keyboard() -> InlineKeyboardMarkup | None:
    matches = _load_matches()
    if not matches:
        return None
    keyboard = [
        [InlineKeyboardButton(
            f"{m['home_team']} vs {m['away_team']} — {m.get('kickoff_utc', '')[:10]}",
            callback_data=f"match:{m['match_id']}",
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


async def _upload_photo(msg, context, match: dict, event_type: str,
                        ref: tuple[str, int | None]) -> bool:
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

    public_id = photo_public_id(match['match_id'], event_type)
    await msg.reply_text("Uploading to Cloudinary…")

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
            tmp_path = tmp.name
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(tmp_path)
        _shrink_for_upload(tmp_path)
        result = cloudinary.uploader.upload(
            tmp_path,
            public_id=public_id,
            overwrite=True,
            invalidate=True,   # purge CDN cache when replacing a photo
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
        f"{match['home_team']} vs {match['away_team']} — {event_type}\n"
        f"public_id: {result['public_id']}\n"
        f"{result['secure_url']}\n\n"
        "The automation will pick it up from here. /start for another photo."
    )
    return True


# ── Conversation handlers ─────────────────────────────────────────────────────

async def _ensure_menu(msg, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Put the button keyboard in front of the user, once per chat per run.

    A reply keyboard can't ride along with an inline one, and /start's first
    message is the inline match list — so it gets its own message. Once shown,
    Telegram keeps it (is_persistent), so this is at most one extra line per
    chat for as long as the process lives.
    """
    if context.chat_data.get('menu_shown'):
        return
    context.chat_data['menu_shown'] = True
    await msg.reply_text("Buttons are below ⌨️", reply_markup=MAIN_KEYBOARD)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — also the answer to anything the bot doesn't understand."""
    if not _allowed(update):
        await update.message.reply_text("Not authorized.")
        return
    context.chat_data['menu_shown'] = True
    await update.message.reply_text(
        HELP_TEXT, parse_mode='Markdown', reply_markup=MAIN_KEYBOARD
    )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A /command the bot doesn't have. Say so instead of staying silent."""
    if not _allowed(update) or not update.message:
        return
    context.chat_data['menu_shown'] = True
    await update.message.reply_text(
        f"I don't have that command.\n\n{HELP_TEXT}",
        parse_mode='Markdown',
        reply_markup=MAIN_KEYBOARD,
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
    await _ensure_menu(update.message, context)
    await update.message.reply_text("Select the match:", reply_markup=keyboard)
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
    await _ensure_menu(update.message, context)
    await update.message.reply_text(
        "Got the photo. Which match is it for?", reply_markup=keyboard
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

    event_type = query.data.split(':', 1)[1]
    context.user_data['event_type'] = event_type

    match = context.user_data.get('match')
    if match is None:
        # Only reachable if the process restarted between the two keyboards.
        await query.edit_message_text("That selection expired — /start to try again.")
        return ConversationHandler.END

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
        await msg.reply_text("Got the photo. Which match is it for?",
                             reply_markup=keyboard)
        return SELECT_MATCH

    ok = await _upload_photo(msg, context, match, event_type, ref)
    return ConversationHandler.END if ok else WAIT_PHOTO


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

    await _ensure_menu(msg, context)

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
        f"{BTN_CANCEL} or /cancel stops at any point.\n\n"
        "First: what's the *home team*?\n"
        f"_Just the name, as it should read on the card — up to "
        f"{manual_match.MAX_TEAM_NAME} characters. e.g. Arsenal_",
        parse_mode='Markdown',
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
            parse_mode='Markdown')
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
            f"rather than `Spurs`._", parse_mode='Markdown')
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
        f"/card to start another post.",
        reply_markup=MAIN_KEYBOARD,
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
    context.chat_data['menu_shown'] = True

    owner = _owner(update)
    cards = await asyncio.to_thread(_card_batch().pending, owner)
    if not cards:
        await update.message.reply_text(
            "Nothing waiting. /card builds one.", reply_markup=MAIN_KEYBOARD)
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
    context.chat_data['menu_shown'] = True
    await update.message.reply_text(
        f"Cancelled. {BTN_NEW} (or /start) to begin again.",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def stray_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Anything that reached no other handler. Answer, don't ignore."""
    if not _allowed(update) or not update.message:
        return
    context.chat_data['menu_shown'] = True
    await update.message.reply_text(
        "I only understand photos and the buttons below — typed messages "
        f"don't do anything here.\n\nSend a match photo, or tap {BTN_NEW}.",
        reply_markup=MAIN_KEYBOARD,
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

    # Fallbacks both conversations share: Cancel ends whatever is running,
    # Help answers without disturbing the state it was in (returns None), and
    # /card jumps to the manual flow from anywhere.
    common_fallbacks = [
        CommandHandler('cancel', cancel),
        MessageHandler(BTN_CANCEL_FILTER, cancel),
        CommandHandler('help', help_cmd),
        MessageHandler(BTN_HELP_FILTER, help_cmd),
        CommandHandler('batch', batch_cmd),
    ]

    # Registered before the photo conversation so /card and the background
    # photo it is waiting for reach this one first. Its entry points only match
    # /card, so a photo sent outside the flow still lands in photo_first.
    card_conv = ConversationHandler(
        entry_points=[
            CommandHandler('card', card_start),
            MessageHandler(BTN_CARD_FILTER, card_start),
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
            MessageHandler(BTN_CARD_FILTER, card_start),
            # Last: anything else mid-flow re-asks the current step instead of
            # throwing away everything typed so far.
            MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, card_wrong_input),
        ],
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CommandHandler('newphoto', start),
            MessageHandler(BTN_NEW_FILTER, start),
            # A bare photo is a valid way to begin — see photo_first.
            MessageHandler(PHOTO_ENTRY_FILTER, photo_first),
        ],
        states={
            SELECT_MATCH: [CallbackQueryHandler(match_chosen)],
            SELECT_EVENT: [CallbackQueryHandler(event_chosen)],
            WAIT_PHOTO: [MessageHandler(filters.PHOTO | filters.Document.ALL, photo_received)],
        },
        # Fallbacks apply in every state, so the buttons keep working mid-flow:
        # New photo restarts it from the match list, and the shared ones above
        # cover Cancel and Help.
        fallbacks=common_fallbacks + [
            CommandHandler('start', start),
            CommandHandler('newphoto', start),
            MessageHandler(BTN_NEW_FILTER, start),
        ],
    )
    app.add_handler(card_conv)
    app.add_handler(conv)
    # Outside any conversation these still have to answer.
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(MessageHandler(BTN_HELP_FILTER, help_cmd))
    app.add_handler(MessageHandler(BTN_CANCEL_FILTER, cancel))
    app.add_handler(CommandHandler('batch', batch_cmd))
    app.add_handler(CallbackQueryHandler(batch_clear, pattern=r'^batch:clear$'))
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
