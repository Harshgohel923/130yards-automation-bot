# telegram_bot.py — match photo intake bot.
"""
Telegram bot for uploading the base photo used by the scorecard overlay.

Two ways in, because the bot restarts often in production (its watchdog stops
it whenever no match worker is active) and conversation state lives only in
memory:

  /start (or /newphoto)          →  pick match  →  pick HT/FT  →  send photo
  just send the photo            →  pick match  →  pick HT/FT  →  uploaded

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

import json
import logging
import os
import tempfile
import traceback

import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv
from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _allowed(update):
        await update.message.reply_text("Not authorized.")
        return ConversationHandler.END

    keyboard = _match_keyboard()
    if keyboard is None:
        await update.message.reply_text("No matches found in matches.json.")
        return ConversationHandler.END

    context.user_data.clear()
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


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled. /start to begin again.")
    return ConversationHandler.END


async def stray_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Anything that reached no other handler. Answer, don't ignore."""
    if not _allowed(update) or not update.message:
        return
    await update.message.reply_text(
        "Send me a match photo, or /start to pick a match first."
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


def main() -> None:
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing from .env — create a bot with @BotFather first.")

    app = ApplicationBuilder().token(token).build()
    conv = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CommandHandler('newphoto', start),
            # A bare photo is a valid way to begin — see photo_first.
            MessageHandler(PHOTO_ENTRY_FILTER, photo_first),
        ],
        states={
            SELECT_MATCH: [CallbackQueryHandler(match_chosen)],
            SELECT_EVENT: [CallbackQueryHandler(event_chosen)],
            WAIT_PHOTO: [MessageHandler(filters.PHOTO | filters.Document.ALL, photo_received)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    app.add_handler(conv)
    # Lower-priority group: only sees what the conversation didn't take.
    app.add_handler(MessageHandler(STRAY_FILTER, stray_message), group=1)
    app.add_error_handler(on_error)
    print("[bot] Running. Ctrl+C to stop.")
    # Pending updates are deliberately NOT dropped: the bot is restarted often,
    # and a photo sent while it was down is exactly the one worth keeping.
    app.run_polling()


if __name__ == '__main__':
    main()
