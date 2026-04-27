"""
add_site.py
-----------
Interactive CLI helper to add a new site to sites.yaml.

Usage:
    python add_site.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

SITES_FILE = Path("sites.yaml")


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _prompt(label: str, required: bool = True) -> str:
    while True:
        value = input(f"  {label}: ").strip()
        if value or not required:
            return value
        print("    This field is required.")


def main() -> None:
    print("\nTupureel — Add New Site")
    print("=" * 40)

    site_id = _prompt("Site ID (e.g. CHCH-B-02)").upper()
    name = _prompt("Site name")
    location = _prompt("Location")
    telegram_chat_id = _prompt("Telegram chat ID")
    bearing_raw = _prompt("Bearing (degrees, e.g. 247)")
    landmark = _prompt("Landmark description")
    contact_email = _prompt("Contact email")

    try:
        bearing = int(bearing_raw)
    except ValueError:
        print(f"  Warning: '{bearing_raw}' is not a whole number — storing as-is.")
        bearing = bearing_raw

    data = _load(SITES_FILE)
    if "sites" not in data or data["sites"] is None:
        data["sites"] = {}

    if site_id in data["sites"]:
        answer = input(f"\n  Site {site_id} already exists. Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            print("  Aborted.")
            sys.exit(0)

    data["sites"][site_id] = {
        "name": name,
        "location": location,
        "telegram_chat_id": telegram_chat_id,
        "bearing": bearing,
        "landmark": landmark,
        "contact_email": contact_email,
        "active": True,
        "created": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),
        "notes": "",
    }

    with SITES_FILE.open("w") as fh:
        yaml.dump(data, fh, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print(f"\n  ✓ Site {site_id} added to {SITES_FILE}.")
    print(f"    Name:     {name}")
    print(f"    Location: {location}")
    print(f"    Bearing:  {bearing}°")
    print(f"    Landmark: {landmark}")


if __name__ == "__main__":
    main()
