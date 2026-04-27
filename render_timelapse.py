"""
render_timelapse.py
-------------------
Tupureel timelapse rendering script.

For each site in sites.yaml, lists all approved photos stored in
Cloudflare R2, renders a timelapse video with FFmpeg, uploads the MP4
back to R2 under timelapses/, and sends it to the site's Telegram
channel.  Sites with fewer than 10 photos are skipped.  If one site
fails the error is logged and rendering continues for the remaining sites.

Required environment variables (loaded from .env automatically):
    R2_ACCOUNT_ID        — Cloudflare account ID
    R2_ACCESS_KEY_ID     — R2 access key ID
    R2_SECRET_ACCESS_KEY — R2 secret access key
    R2_BUCKET_NAME       — R2 bucket name
    TELEGRAM_TOKEN       — bot token from BotFather

Usage:
    python render_timelapse.py
"""

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import boto3
import yaml
from dotenv import load_dotenv
from telegram import Bot

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SITES_FILE = Path("sites.yaml")
MIN_PHOTOS = 10

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
        log.warning("sites.yaml not found at %s — no sites to process", path)
        return {}
    with path.open() as fh:
        data = yaml.safe_load(fh)
    return data.get("sites", {})


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    )


def list_site_photos(s3, bucket: str, site_id: str) -> list[str]:
    """Return a sorted list of R2 keys for all approved photos in {site_id}/approved/."""
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{site_id}/approved/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(".jpg"):
                keys.append(key)
    return sorted(keys)


def download_photos(s3, bucket: str, keys: list[str], dest_dir: Path) -> list[Path]:
    """Download each R2 key into dest_dir and return sorted local paths."""
    local_paths = []
    for key in keys:
        dest = dest_dir / Path(key).name
        s3.download_file(bucket, key, str(dest))
        local_paths.append(dest)
    return sorted(local_paths)


def build_concat_file(photo_paths: list[Path], concat_path: Path, duration: int = 2) -> None:
    """Write an FFmpeg concat demuxer file with each photo held for `duration` seconds."""
    lines = []
    for p in photo_paths:
        lines.append(f"file '{p}'")
        lines.append(f"duration {duration}")
    # Repeat the last entry without a duration so FFmpeg flushes the final frame.
    if photo_paths:
        lines.append(f"file '{photo_paths[-1]}'")
    concat_path.write_text("\n".join(lines) + "\n")


def render_timelapse(concat_path: Path, output_path: Path) -> None:
    """Run FFmpeg to produce a 1920x1080 H.264 timelapse from a concat filelist."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_path),
        "-vf", (
            "scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black"
        ),
        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{result.stderr[-2000:]}")


def extract_date(key: str) -> str:
    """Extract YYYY-MM-DD from a key like CHCH-A-01/2025-04-26-1.jpg."""
    return Path(key).stem[:10]


def months_between(d1_str: str, d2_str: str) -> int:
    """Return the number of whole months between two YYYY-MM-DD date strings."""
    d1 = datetime.strptime(d1_str, "%Y-%m-%d")
    d2 = datetime.strptime(d2_str, "%Y-%m-%d")
    return (d2.year - d1.year) * 12 + d2.month - d1.month


async def _send_timelapse(
    token: str,
    chat_id: str,
    video_path: Path,
    site_name: str,
    count: int,
    first_date: str,
    last_date: str,
    months: int,
) -> None:
    months_label = f"{months} month{'s' if months != 1 else ''}"
    caption = (
        f"🌱 New timelapse ready\n"
        f"📍 {site_name}\n"
        f"📸 {count} photos · {first_date} → {last_date}\n"
        f"⏱ {months_label} of regeneration\n\n"
        f"Ready to share!"
    )
    async with Bot(token=token) as bot:
        with open(video_path, "rb") as fh:
            await bot.send_video(
                chat_id=chat_id,
                video=fh,
                caption=caption,
                supports_streaming=True,
            )


def send_timelapse(
    site_id: str,
    site_info: dict,
    video_path: Path,
    count: int,
    first_date: str,
    last_date: str,
    months: int,
) -> None:
    """Send the rendered timelapse video to the site's Telegram channel."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = site_info.get("telegram_chat_id")
    if not token or not chat_id:
        log.debug("%s: TELEGRAM_TOKEN or telegram_chat_id not set — skipping Telegram send.", site_id)
        return
    try:
        asyncio.run(_send_timelapse(
            token, str(chat_id), video_path,
            site_info.get("name", site_id),
            count, first_date, last_date, months,
        ))
        log.info("%s: Telegram video sent.", site_id)
    except Exception:
        log.exception("%s: Telegram send failed — R2 upload is still complete.", site_id)


# ---------------------------------------------------------------------------
# Per-site processing
# ---------------------------------------------------------------------------


def process_site(
    site_id: str,
    site_info: dict,
    s3,
    bucket: str,
    render_date: datetime,
) -> None:
    """Download photos, render, upload, and notify for a single site."""
    keys = list_site_photos(s3, bucket, site_id)

    if len(keys) < MIN_PHOTOS:
        log.info(
            "%s: only %d photo%s, minimum %d required — skipping",
            site_id, len(keys), "" if len(keys) == 1 else "s", MIN_PHOTOS,
        )
        return

    log.info("%s: %d photos found — starting render.", site_id, len(keys))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        photos = download_photos(s3, bucket, keys, tmp_dir)
        log.info("%s: downloaded %d photos.", site_id, len(photos))

        concat_file = tmp_dir / "filelist.txt"
        build_concat_file(photos, concat_file)

        output_name = f"{site_id}-timelapse-{render_date.strftime('%Y-%m')}.mp4"
        output_path = tmp_dir / output_name
        render_timelapse(concat_file, output_path)
        log.info("%s: render complete → %s", site_id, output_name)

        r2_key = f"timelapses/{output_name}"
        s3.upload_file(str(output_path), bucket, r2_key)
        log.info("%s: uploaded to R2 at %s", site_id, r2_key)

        first_date = extract_date(keys[0])
        last_date = extract_date(keys[-1])
        months = months_between(first_date, last_date)
        send_timelapse(site_id, site_info, output_path, len(keys), first_date, last_date, months)

    # TemporaryDirectory auto-cleans all downloaded photos and the MP4 on exit.


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run() -> None:
    sites = load_sites(SITES_FILE)
    if not sites:
        log.warning("No sites found in %s — nothing to render.", SITES_FILE)
        return

    required_vars = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    s3 = _r2_client()
    bucket = os.environ["R2_BUCKET_NAME"]
    render_date = datetime.utcnow()

    for site_id, site_info in sites.items():
        try:
            process_site(site_id, site_info, s3, bucket, render_date)
        except Exception:
            log.exception("%s: unexpected error — skipping this site.", site_id)

    log.info("Done.")


if __name__ == "__main__":
    run()
