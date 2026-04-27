"""
telegram_bot.py
---------------
Tupureel Telegram moderation bot.

Listens for Approve / Reject button presses on photo moderation messages
sent by check_emails.py.

Photos live in R2 under {site_id}/pending/ when first uploaded.  On
Approve the photo is moved to {site_id}/approved/; on Reject it is moved
to {site_id}/rejected/.  Both operations are a copy-then-delete so the
original is preserved until the destination is confirmed written.

Button tap sequence:
  1. Answer the callback query with a dismissable alert popup — must happen
     within 10 s of the bot *receiving* the update.  If the query has
     already expired the answer is skipped but processing continues.
  2. Immediately edit the caption to a ⏳ pending state and remove the
     buttons so the message cannot be tapped twice.
  3. Move the photo in R2 (copy to destination, delete source).
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


def move_r2_object(s3, bucket: str, source_key: str, dest_key: str) -> None:
    """Copy source_key to dest_key then delete the source."""
    s3.copy_object(
        Bucket=bucket,
        CopySource={"Bucket": bucket, "Key": source_key},
        Key=dest_key,
    )
    s3.delete_object(Bucket=bucket, Key=source_key)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""

    if ":" not in data:
        await query.answer()
        return

    action, r2_key = data.split(":", 1)

    if action not in ("approve", "reject"):
        await query.answer()
        return

    original_caption = query.message.caption or ""

    # ---- Step 1: answer immediately (required within 10 s of receiving the
    # update; wrapped so an expired query never prevents the caption update) ---
    toast = "✓ Response recorded" if action == "approve" else "✗ Response recorded"
    try:
        await query.answer(text=toast, show_alert=True)
    except Exception:
        log.warning("Could not send callback answer for %s (query may have expired)", r2_key)

    # ---- Step 2: remove buttons and show pending state immediately -----------
    pending_caption = (
        f"{original_caption}\n\n⏳ Approved — moving to approved folder"
        if action == "approve"
        else f"{original_caption}\n\n⏳ Rejected — moving to rejected folder"
    )
    await query.edit_message_caption(caption=pending_caption, reply_markup=None)

    # ---- Steps 3 & 4: move the file in R2, then show the confirmed outcome --
    bucket = os.environ.get("R2_BUCKET_NAME", "")

    if action == "approve":
        dest_key = r2_key.replace("/pending/", "/approved/", 1)
        try:
            move_r2_object(_r2_client(), bucket, r2_key, dest_key)
            await query.edit_message_caption(
                caption=f"{original_caption}\n\n✓ Approved — photo added to timelapse queue",
                reply_markup=None,
            )
            log.info("Approved: moved %s → %s", r2_key, dest_key)
        except Exception:
            log.exception("Failed to move %s to approved/", r2_key)
            await query.edit_message_caption(
                caption=f"{original_caption}\n\n✓ Approved — R2 move failed (see logs)",
                reply_markup=None,
            )

    elif action == "reject":
        dest_key = r2_key.replace("/pending/", "/rejected/", 1)
        try:
            move_r2_object(_r2_client(), bucket, r2_key, dest_key)
            await query.edit_message_caption(
                caption=f"{original_caption}\n\n✗ Rejected — photo moved to rejected folder",
                reply_markup=None,
            )
            log.info("Rejected: moved %s → %s", r2_key, dest_key)
        except Exception:
            log.exception("Failed to move %s to rejected/", r2_key)
            await query.edit_message_caption(
                caption=f"{original_caption}\n\n✗ Rejected — R2 move failed (see logs)",
                reply_markup=None,
            )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error processing update: %s", context.error)


def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_TOKEN environment variable is required.")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)
    log.info("Bot started — polling for updates.")
    app.run_polling()


if __name__ == "__main__":
    main()
