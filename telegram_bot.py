# telegram_bot.py — match photo intake bot.
"""
Telegram bot for uploading the base photo used by the scorecard overlay.

Flow:
  /start (or /newphoto)
    → inline keyboard of matches from matches.json
    → inline keyboard: Half Time / Full Time
    → user sends the photo (as a photo or, better, as a file/document
      to avoid Telegram's compression)
    → photo is uploaded to Cloudinary as  match_photos/<match_id>_<HT|FT>
      (overwrite=True, so re-sending replaces the previous photo)

The public_id is deterministic, so the pipeline can fetch the photo for a
match/event with fetch_match_photo() below — no state is shared beyond that.

Setup:
  1. Create a bot with @BotFather, put the token in .env as TELEGRAM_BOT_TOKEN.
  2. Optional: set TELEGRAM_ALLOWED_USER_IDS (comma-separated numeric user ids)
     to restrict who can use the bot. Strongly recommended — anyone who finds
     the bot can otherwise overwrite your match photos.
  3. Run:  python telegram_bot.py
"""

import json
import os
import tempfile

import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv
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

cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET'),
)

MATCHES_FILE = 'matches.json'

SELECT_MATCH, SELECT_EVENT, WAIT_PHOTO = range(3)


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


# ── Conversation handlers ─────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _allowed(update):
        await update.message.reply_text("Not authorized.")
        return ConversationHandler.END

    matches = _load_matches()
    if not matches:
        await update.message.reply_text("No matches found in matches.json.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(
            f"{m['home_team']} vs {m['away_team']} — {m.get('kickoff_utc', '')[:10]}",
            callback_data=f"match:{m['match_id']}",
        )]
        for m in matches
    ]
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    await update.message.reply_text(
        "Select the match:", reply_markup=InlineKeyboardMarkup(keyboard)
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

    context.user_data['event_type'] = query.data.split(':', 1)[1]
    match = context.user_data['match']
    await query.edit_message_text(
        f"{match['home_team']} vs {match['away_team']} — {context.user_data['event_type']}\n\n"
        "Now send the photo.\n"
        "Tip: send it as a *file/document* to keep full quality "
        "(Telegram compresses regular photo messages).",
        parse_mode='Markdown',
    )
    return WAIT_PHOTO


async def photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message

    if msg.document:
        if not (msg.document.mime_type or '').startswith('image/'):
            await msg.reply_text("That file isn't an image — send a JPG/PNG.")
            return WAIT_PHOTO
        tg_file = await msg.document.get_file()
    elif msg.photo:
        tg_file = await msg.photo[-1].get_file()  # largest size Telegram kept
    else:
        await msg.reply_text("Please send a photo (or an image file).")
        return WAIT_PHOTO

    match = context.user_data['match']
    event_type = context.user_data['event_type']
    public_id = photo_public_id(match['match_id'], event_type)

    await msg.reply_text("Uploading to Cloudinary…")
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)
        result = cloudinary.uploader.upload(
            tmp_path,
            public_id=public_id,
            overwrite=True,
            invalidate=True,   # purge CDN cache when replacing a photo
        )
    except Exception as e:
        await msg.reply_text(f"Upload failed: {e}\nSend the photo again, or /cancel.")
        return WAIT_PHOTO
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
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled. /start to begin again.")
    return ConversationHandler.END


def main() -> None:
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing from .env — create a bot with @BotFather first.")

    app = ApplicationBuilder().token(token).build()
    conv = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CommandHandler('newphoto', start),
        ],
        states={
            SELECT_MATCH: [CallbackQueryHandler(match_chosen)],
            SELECT_EVENT: [CallbackQueryHandler(event_chosen)],
            WAIT_PHOTO: [MessageHandler(filters.PHOTO | filters.Document.ALL, photo_received)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    app.add_handler(conv)
    print("[bot] Running. Ctrl+C to stop.")
    app.run_polling()


if __name__ == '__main__':
    main()
