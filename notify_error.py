"""
notify_error.py
---------------
Sends a workflow failure notification email via Gmail SMTP.

Called from GitHub Actions when a workflow step fails:
    python notify_error.py --workflow "check_emails" --run-url "https://..."

Required environment variables:
    GMAIL_ADDRESS      — Gmail account (used as both sender and recipient)
    GMAIL_APP_PASSWORD — Gmail App Password for SMTP authentication
"""

import argparse
import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", required=True, help="Name of the workflow that failed")
    parser.add_argument("--run-url", required=True, help="URL of the failed Actions run")
    args = parser.parse_args()

    address = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not address or not password:
        raise SystemExit("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set.")

    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    body = (
        f"Tupureel workflow failure notification\n"
        f"{'=' * 40}\n\n"
        f"Workflow:  {args.workflow}\n"
        f"Date/Time: {now}\n"
        f"Run URL:   {args.run_url}\n\n"
        f"This workflow run failed. Check the Actions log at the URL above for details.\n"
    )

    msg = MIMEText(body)
    msg["Subject"] = f"⚠️ Tupureel workflow failed: {args.workflow}"
    msg["From"] = address
    msg["To"] = address

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(address, password)
        smtp.sendmail(address, address, msg.as_string())

    print(f"Failure notification sent for workflow '{args.workflow}'.")


if __name__ == "__main__":
    main()
