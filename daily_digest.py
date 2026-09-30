#!/usr/bin/env python3
"""
daily_digest.py

Reads state/new_jobs_log.jsonl (written by check_jobs.py) and sends ONE
summary notification covering everything delivered since the previous
digest. Run this from a separate, once-a-day GitHub Actions workflow if
you'd rather get a morning rundown instead of (or in addition to) the
instant per-posting pushes from check_jobs.py.

"Since the previous digest" rather than "the last 24 hours": scheduled
runs drift and occasionally get dropped, and a fixed window then repeats
some postings and misses others. The last covered timestamp is kept in
state/digest_watermark.txt, which the workflow commits.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from check_jobs import keep_row

LOG_FILE = Path("state/new_jobs_log.jsonl")
WATERMARK_FILE = Path("state/digest_watermark.txt")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL")


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def send_notification(title: str, message: str) -> bool:
    """True once Discord has the message (or in a dry run)."""
    embed = {
        "title": _clip(title, 256),
        "description": _clip(message, 4096),
        "color": 0x5865F2,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if not DISCORD_WEBHOOK:
        print(f"[DRY RUN discord] {json.dumps(embed)[:1500]}")
        return True
    for attempt in range(1, 5):
        try:
            resp = requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=20)
        except requests.RequestException as e:
            print(f"Discord request error (attempt {attempt}): {e}")
            time.sleep(2 * attempt)
            continue
        if resp.status_code in (200, 204):
            return True
        if resp.status_code == 429:
            try:
                wait = float(resp.json().get("retry_after", 2))
            except (ValueError, KeyError, TypeError):
                wait = 2.0
            time.sleep(wait + 0.5)
            continue
        print(f"Discord returned {resp.status_code}: {resp.text[:300]}")
        if 400 <= resp.status_code < 500:
            return False
        time.sleep(2 * attempt)
    return False


def load_watermark() -> datetime:
    try:
        return datetime.fromisoformat(WATERMARK_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return datetime.now(timezone.utc) - timedelta(hours=24)


def read_entries(since: datetime) -> list[dict]:
    if not LOG_FILE.exists():
        return []
    entries = []
    with open(LOG_FILE, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            # One bad line (a botched merge, a truncated write) must not
            # cost the whole digest.
            try:
                entry = json.loads(line)
                seen_at = datetime.fromisoformat(entry["seen_at"])
            except (ValueError, KeyError, TypeError) as e:
                print(f"Skipping unreadable log line {n}: {e}")
                continue
            if seen_at <= since:
                continue
            # Entries logged before a filter change (international, grad-
            # level) would otherwise resurface here.
            row = {"cells": entry.get("cells") or [], "link": entry.get("link")}
            if not keep_row(row, "all", entry.get("source") or ""):
                continue
            entry["_seen_at"] = seen_at
            entries.append(entry)
    return entries


def main() -> int:
    since = load_watermark()
    recent = read_entries(since)

    if not recent:
        message = "No new postings since the last rundown."
    else:
        lines = []
        for e in recent:
            cells = e.get("cells", [])
            label = " — ".join(cells[:2]) if len(cells) >= 2 else (cells[0] if cells else "New posting")
            lines.append(f"• [{e['repo'].split('/')[-1]}] {label}")
        message = f"{len(recent)} new posting(s) since the last rundown:\n" + "\n".join(lines[:25])
        if len(lines) > 25:
            message += f"\n…and {len(lines) - 25} more."

    if not send_notification("Daily internship rundown", message):
        # Watermark untouched, so the next digest covers these too.
        print("::error::Digest could not be delivered to Discord.")
        return 1

    if recent and DISCORD_WEBHOOK:
        newest = max(e["_seen_at"] for e in recent)
        WATERMARK_FILE.write_text(newest.isoformat() + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
