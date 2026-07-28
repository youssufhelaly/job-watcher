#!/usr/bin/env python3
"""
daily_digest.py

Reads state/new_jobs_log.jsonl (written by check_jobs.py) and sends ONE
summary notification covering everything found in the last 24 hours.
Run this from a separate, once-a-day GitHub Actions workflow if you'd
rather get a morning rundown instead of (or in addition to) the
instant per-posting pushes from check_jobs.py.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

LOG_FILE = Path("state/new_jobs_log.jsonl")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL")


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def send_notification(title: str, message: str):
    embed = {
        "title": _clip(title, 256),
        "description": _clip(message, 4096),
        "color": 0x5865F2,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if not DISCORD_WEBHOOK:
        print(f"[DRY RUN discord] {json.dumps(embed)[:1500]}")
        return
    for attempt in range(1, 5):
        try:
            resp = requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=20)
        except requests.RequestException as e:
            print(f"Discord request error (attempt {attempt}): {e}")
            time.sleep(2 * attempt)
            continue
        if resp.status_code in (200, 204):
            return
        if resp.status_code == 429:
            try:
                wait = float(resp.json().get("retry_after", 2))
            except (ValueError, KeyError, TypeError):
                wait = 2.0
            time.sleep(wait + 0.5)
            continue
        print(f"Discord returned {resp.status_code}: {resp.text[:300]}")
        return


def main():
    if not LOG_FILE.exists():
        send_notification("Daily internship rundown", "No new postings found in the last 24 hours.")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent = []
    with open(LOG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            seen_at = datetime.fromisoformat(entry["seen_at"])
            if seen_at >= cutoff:
                recent.append(entry)

    if not recent:
        send_notification("Daily internship rundown", "No new postings found in the last 24 hours.")
        return

    lines = []
    for e in recent:
        cells = e.get("cells", [])
        label = " — ".join(cells[:2]) if len(cells) >= 2 else (cells[0] if cells else "New posting")
        lines.append(f"• [{e['repo'].split('/')[-1]}] {label}")

    message = f"{len(recent)} new posting(s) in the last 24h:\n" + "\n".join(lines[:25])
    if len(lines) > 25:
        message += f"\n…and {len(lines) - 25} more."

    send_notification("Daily internship rundown", message)


if __name__ == "__main__":
    main()
