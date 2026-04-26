"""
telegram_bot.py
---------------
Tupureel Telegram moderation bot.

Listens for Approve / Reject button presses on photo moderation messages
sent by check_emails.py.

Button tap sequence:
  1. Immediately answer the callback query (toast popup) — must happen
     within 10 s of the tap before the query expires.
  2. Immediately edit the caption to show a ⏳ pending state and remove
     the buttons so the message cannot be tapped twice.
  3. Perform the actual work (R2 deletion for Reject; nothing for Approve).
  4. Edit the caption one final time to show the confirmed outcome.

Required environment variables (loaded from .env automatically):
    TELEGRAM_TOKEN       — bot token from BotFather
    R2_ACCOUNT_ID        — Cloudflare account ID
    R2_ACCESS_KEY_ID     — R2 access key ID
    R2_SECRET_ACCESS_KEY — R2 secret access key
    R2_BUCKET_NAME       — R2 bucket name
"""

import logging
import os

import boto3
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, ContextTypes

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""

    if ":" not in data:
        await query.answer()
        return

    action, r2_key = data.split(":", 1)
    original_caption = query.message.caption or ""

    # ---- Step 1: answer immediately (Telegram requires this within 10 s) ----
    if action == "approve":
        await query.answer("✓ Response recorded")
    elif action == "reject":
        await query.answer("✗ Response recorded")
    else:
        await query.answer()
        return

    # ---- Step 2: show pending state and remove buttons immediately ----------
    pending_caption = (
        f"{original_caption}\n\n⏳ Approved — will be processed at next run"
        if action == "approve"
        else f"{original_caption}\n\n⏳ Rejected — will be processed at next run"
    )
    await query.edit_message_caption(caption=pending_caption, reply_markup=None)

    # ---- Step 3 & 4: do the work, then show the final outcome ---------------
    if action == "approve":
        await query.edit_message_caption(
            caption=f"{original_caption}\n\n✓ Approved — photo added to timelapse queue",
            reply_markup=None,
        )
        log.info("Approved: %s", r2_key)

    elif action == "reject":
        bucket = os.environ.get("R2_BUCKET_NAME", "")
        try:
            _r2_client().delete_object(Bucket=bucket, Key=r2_key)
            await query.edit_message_caption(
                caption=f"{original_caption}\n\n✗ Rejected — photo removed from R2",
                reply_markup=None,
            )
            log.info("Rejected and deleted from R2: %s", r2_key)
        except Exception:
            log.exception("Failed to delete %s from R2", r2_key)
            await query.edit_message_caption(
                caption=f"{original_caption}\n\n✗ Rejected — R2 deletion failed (see logs)",
                reply_markup=None,
            )


def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_TOKEN environment variable is required.")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CallbackQueryHandler(button_handler))
    log.info("Bot started — polling for updates.")
    app.run_polling()


if __name__ == "__main__":
    main()
