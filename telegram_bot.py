"""
telegram_bot.py
---------------
Tupureel Telegram moderation bot.

Listens for Approve / Reject button presses on photo moderation messages
sent by check_emails.py.  On Approve the photo is kept in R2 as-is.
On Reject the photo is deleted from R2.  Both actions update the original
Telegram message to remove the buttons and confirm the outcome.

Runs until interrupted (Ctrl-C or SIGTERM).  In GitHub Actions, trigger
via workflow_dispatch and let the job run; it will stop when the runner
hits its 6-hour limit or you cancel the workflow.

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
    await query.answer()

    data = query.data or ""
    if ":" not in data:
        return

    action, r2_key = data.split(":", 1)
    original_caption = query.message.caption or ""

    if action == "approve":
        await query.edit_message_caption(
            caption=f"{original_caption}\n\n✓ Approved",
            reply_markup=None,
        )
        log.info("Approved: %s", r2_key)

    elif action == "reject":
        bucket = os.environ.get("R2_BUCKET_NAME", "")
        try:
            _r2_client().delete_object(Bucket=bucket, Key=r2_key)
            await query.edit_message_caption(
                caption=f"{original_caption}\n\n✗ Rejected — deleted from R2",
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
