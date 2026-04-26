"""
check_emails.py
---------------
Tupureel photopoint monitoring — Gmail ingestion script.

Connects to Gmail via IMAP, finds unread emails addressed to
photos@tupureel.org, saves any photo attachments to a local folder
tree, and labels each email as either "processed" or "unknown-site".
After saving locally, each photo is also uploaded to Cloudflare R2 under
{site_id}/{YYYY-MM-DD}.jpg.  R2 upload failures are logged but do not
affect local saving.

Required environment variables (loaded from .env automatically):
    GMAIL_ADDRESS        — the Gmail account used to receive photos
    GMAIL_APP_PASSWORD   — a Gmail App Password (not your main password)
    R2_ACCOUNT_ID        — Cloudflare account ID
    R2_ACCESS_KEY_ID     — R2 access key ID
    R2_SECRET_ACCESS_KEY — R2 secret access key
    R2_BUCKET_NAME       — R2 bucket name

Usage:
    python check_emails.py
"""

import email
import imaplib
import logging
import os
import re
import sys
from datetime import datetime
from email.header import decode_header
from email.message import Message
from pathlib import Path

import boto3
import yaml
from dotenv import load_dotenv

# Load environment variables from .env (does nothing if file is absent).
load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
TARGET_ADDRESS = "photos@tupureel.org"
PHOTOS_ROOT = Path("photos")
SITES_FILE = Path("sites.yaml")

# Gmail label names — created automatically if they don't exist.
LABEL_PROCESSED = "processed"
LABEL_UNKNOWN_SITE = "unknown-site"

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic"}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_sites(path: Path) -> dict:
    """Return the sites dict from sites.yaml, or {} if the file is missing."""
    if not path.exists():
        log.warning("sites.yaml not found at %s — all sites will be treated as unknown", path)
        return {}
    with path.open() as fh:
        data = yaml.safe_load(fh)
    return data.get("sites", {})


def get_credentials() -> tuple[str, str]:
    """Read Gmail credentials from environment variables."""
    address = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not address or not password:
        log.error(
            "GMAIL_ADDRESS and GMAIL_APP_PASSWORD environment variables must be set."
        )
        sys.exit(1)
    return address, password


def decode_subject(raw_subject: str) -> str:
    """Decode a potentially RFC-2047-encoded email subject to a plain string."""
    parts = decode_header(raw_subject)
    decoded_parts = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded_parts.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded_parts.append(part)
    return "".join(decoded_parts).strip()


def extract_site_id(subject: str) -> str | None:
    """
    Pull the site ID out of the email subject.

    The subject is expected to be exactly the site ID (e.g. DART-B-04),
    but we strip surrounding whitespace to be forgiving.
    """
    cleaned = subject.strip()
    # Accept IDs made of uppercase letters, digits, and hyphens.
    if re.fullmatch(r"[A-Z0-9][A-Z0-9\-]*[A-Z0-9]", cleaned):
        return cleaned
    return None


def find_photo_attachment(msg: Message) -> tuple[str, bytes] | None:
    """
    Walk the email MIME tree and return (filename, data) for the first
    recognised photo attachment, or None if none is found.
    """
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get("Content-Disposition") is None:
            continue

        raw_filename = part.get_filename()
        if not raw_filename:
            continue

        # RFC-2047 filenames also need decoding.
        filename_parts = decode_header(raw_filename)
        filename = ""
        for chunk, charset in filename_parts:
            if isinstance(chunk, bytes):
                filename += chunk.decode(charset or "utf-8", errors="replace")
            else:
                filename += chunk

        ext = Path(filename).suffix.lower()
        if ext in ALLOWED_EXTENSIONS:
            return filename, part.get_payload(decode=True)

    return None


def ensure_gmail_label(imap: imaplib.IMAP4_SSL, label: str) -> None:
    """Create a Gmail label if it doesn't already exist."""
    # LIST returns existing mailboxes; CREATE is a no-op on most servers
    # if the mailbox already exists, but Gmail silently ignores duplicates.
    status, _ = imap.create(label)
    # status is "OK" on creation or "NO" if it already exists — both are fine.


def apply_label(imap: imaplib.IMAP4_SSL, uid: str, label: str) -> None:
    """
    Copy the message to the target label mailbox (Gmail's way of applying
    a label via IMAP) and then remove the INBOX copy by marking it deleted.

    We use COPY + STORE(+FLAGS \\Deleted) rather than moving, so that the
    message stays in All Mail as Gmail normally would.
    """
    imap.uid("COPY", uid, label)


def mark_read(imap: imaplib.IMAP4_SSL, uid: str) -> None:
    """Mark a message as read (remove the \\Unseen flag)."""
    imap.uid("STORE", uid, "+FLAGS", "\\Seen")


def upload_to_r2(local_path: Path, site_id: str) -> None:
    """Upload local_path to Cloudflare R2 under {site_id}/{YYYY-MM-DD}.jpg."""
    account_id = os.environ.get("R2_ACCOUNT_ID")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("R2_BUCKET_NAME")

    if not all([account_id, access_key, secret_key, bucket]):
        log.debug("R2 env vars not set — skipping R2 upload.")
        return

    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        key = f"{site_id}/{local_path.stem}.jpg"
        s3.upload_file(str(local_path), bucket, key)
        log.info("R2 upload OK: %s", key)

    except Exception:
        log.exception("R2 upload failed for %s — local copy is safe.", local_path.name)


def save_photo(site_id: str, extension: str, data: bytes, received_date: datetime) -> Path:
    """Write photo bytes to /photos/{site_id}/{YYYY-MM-DD}{ext} and return the path."""
    dest_dir = PHOTOS_ROOT / site_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    date_str = received_date.strftime("%Y-%m-%d")
    dest_path = dest_dir / f"{date_str}{extension}"

    # If a file for today already exists, append a counter to avoid overwriting.
    counter = 1
    while dest_path.exists():
        dest_path = dest_dir / f"{date_str}-{counter}{extension}"
        counter += 1

    dest_path.write_bytes(data)
    return dest_path


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


def process_email(imap: imaplib.IMAP4_SSL, uid: str, sites: dict) -> None:
    """Fetch and process a single email identified by its UID."""
    # Fetch the full RFC822 message.
    status, data = imap.uid("FETCH", uid, "(RFC822 INTERNALDATE)")
    if status != "OK" or not data or data[0] is None:
        log.warning("UID %s: could not fetch message, skipping.", uid)
        return

    raw_message = data[0][1]
    msg = email.message_from_bytes(raw_message)

    # ---- Subject → site ID ------------------------------------------------
    raw_subject = msg.get("Subject", "")
    subject = decode_subject(raw_subject)
    site_id = extract_site_id(subject)

    if not site_id:
        log.info("UID %s: subject %r does not look like a site ID — skipping.", uid, subject)
        mark_read(imap, uid)
        return

    # ---- Date the email was received --------------------------------------
    # INTERNALDATE from the server is the most reliable source.
    internaldate_str = data[0][0].decode()
    # imaplib ships with a parser for this.
    received_dt = imaplib.Internaldate2tuple(data[0][0])
    if received_dt:
        received = datetime(*received_dt[:6])
    else:
        received = datetime.utcnow()

    # ---- Attachment check -------------------------------------------------
    attachment = find_photo_attachment(msg)

    if attachment is None:
        log.info(
            "UID %s  site=%s: no photo attachment found — marking read and skipping.",
            uid, site_id,
        )
        mark_read(imap, uid)
        return

    original_filename, photo_data = attachment
    extension = Path(original_filename).suffix.lower()

    # ---- Site ID validation -----------------------------------------------
    if site_id not in sites:
        log.warning(
            "UID %s  site=%s: site ID not recognised — applying label '%s'.",
            uid, site_id, LABEL_UNKNOWN_SITE,
        )
        ensure_gmail_label(imap, LABEL_UNKNOWN_SITE)
        apply_label(imap, uid, LABEL_UNKNOWN_SITE)
        mark_read(imap, uid)
        return

    # ---- Save the photo ---------------------------------------------------
    saved_path = save_photo(site_id, extension, photo_data, received)
    upload_to_r2(saved_path, site_id)

    site_info = sites[site_id]
    log.info(
        "UID %s  site=%s (%s, %s): photo saved → %s",
        uid,
        site_id,
        site_info.get("name", "?"),
        site_info.get("location", "?"),
        saved_path,
    )

    # ---- Label + mark read ------------------------------------------------
    ensure_gmail_label(imap, LABEL_PROCESSED)
    apply_label(imap, uid, LABEL_PROCESSED)
    mark_read(imap, uid)


def run() -> None:
    """Main entry point — connect, iterate over unread emails, disconnect."""
    address, password = get_credentials()
    sites = load_sites(SITES_FILE)

    log.info("Connecting to Gmail IMAP as %s …", address)
    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as imap:
        imap.login(address, password)
        log.info("Login successful.")

        imap.select("INBOX")

        # Search for unread messages addressed to the photos inbox.
        # Gmail ignores the TO criteria in IMAP SEARCH; we filter manually below.
        status, data = imap.uid("SEARCH", None, "UNSEEN")
        if status != "OK":
            log.error("SEARCH command failed: %s", data)
            return

        uids = data[0].split()
        if not uids:
            log.info("No unread messages found.")
            return

        log.info("Found %d unread message(s). Checking recipients …", len(uids))

        for uid in uids:
            # Fetch only the headers first to check the To/Delivered-To field
            # without downloading the full body for irrelevant messages.
            status, header_data = imap.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (TO DELIVERED-TO)])")
            if status != "OK":
                continue

            header_bytes = header_data[0][1]
            header_msg = email.message_from_bytes(header_bytes)

            to_fields = [
                (header_msg.get("To") or ""),
                (header_msg.get("Delivered-To") or ""),
            ]
            addressed_to_us = any(TARGET_ADDRESS in field for field in to_fields)

            if not addressed_to_us:
                # Not meant for us — leave it completely untouched.
                continue

            try:
                process_email(imap, uid, sites)
            except Exception:
                log.exception("UID %s: unexpected error — skipping this message.", uid)

    log.info("Done.")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run()
