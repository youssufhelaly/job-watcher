#!/usr/bin/env python3
"""
check_jobs.py

Watches a list of GitHub repos for newly-added job/internship rows and
sends a push notification for each one. Handles the two table formats
these tracker repos commonly use:
  - HTML <table><tr><td> rows (e.g. SimplifyJobs/Summer-Internships style)
  - Markdown "| cell | cell |" pipe-table rows

New rows are detected with a SET difference on a stable identity key
(the job's real application link, when one exists) rather than a
line-by-line diff. This matters because these repos frequently re-sort
their table (e.g. "actively hiring" first) -- a positional diff would
misfire on every reorder, a set diff will not.

State (last-seen README per repo) lives in state/<owner>_<repo>.txt so
this script only has to compare "what's there now" to "what was there
last time it ran."

Notifications go to a Discord channel via webhook, set through the
DISCORD_WEBHOOK_URL environment variable. If it isn't set, the script
prints what it would have sent instead (dry run), which is handy for
testing without spamming the channel.
"""

import os
import re
import json
import base64
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 1. CONFIG -- replace with your four repos, "owner/repo" format
# ---------------------------------------------------------------------------
REPOS = [
    "SimplifyJobs/Summer2026-Internships",
    "vanshb03/Summer2027-Internships",
    "speedyapply/2027-SWE-College-Jobs",
    "speedyapply/2027-AI-College-Jobs",
]

STATE_DIR = Path("state")
LOG_FILE = STATE_DIR / "new_jobs_log.jsonl"
LEDGER_FILE = STATE_DIR / "notified.json"
GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # auto-provided inside GitHub Actions

# If a single run turns up more than this many "new" postings, that's
# almost certainly a repo-side format/link change rather than a genuine
# hiring surge -- send ONE heads-up instead of a few hundred pushes.
FLOOD_THRESHOLD = int(os.environ.get("FLOOD_THRESHOLD", "50"))

AGE_CELL_RE = re.compile(r"^\d+\s*(d|day|days|h|hr|hrs|hour|hours|mo|month|months|w|wk|week|weeks)$", re.I)
HEADER_WORDS = {
    "company", "role", "position", "title", "location", "date", "link",
    "application", "apply", "notes", "age", "posted", "sponsorship",
}


# ---------------------------------------------------------------------------
# 2. Fetch README content for a repo (works regardless of default branch)
# ---------------------------------------------------------------------------
def fetch_readme(owner_repo: str) -> str:
    headers = {"Accept": "application/vnd.github.raw+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    try:
        resp = requests.get(f"{GITHUB_API}/repos/{owner_repo}/readme", headers=headers, timeout=30)
        resp.raise_for_status()
        if resp.headers.get("Content-Type", "").startswith("text/") or resp.text.lstrip().startswith(("#", "<", "!", "[")):
            return resp.text
        data = resp.json()
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception:
        # Fallback for local testing without a token / API hiccups: try the CDN directly.
        for branch in ("main", "master"):
            r = requests.get(f"https://raw.githubusercontent.com/{owner_repo}/{branch}/README.md", timeout=30)
            if r.status_code == 200:
                return r.text
        raise


# ---------------------------------------------------------------------------
# 3. Extract job rows from either HTML tables or markdown pipe tables
# ---------------------------------------------------------------------------
# Query params that are pure tracking noise and must NOT affect a job's
# identity. Everything else is preserved, because on these job boards the
# query string frequently *is* the identity -- e.g. greenhouse's
# ?gh_jid=123 and ?token=456 point at completely different postings that
# share one path. Stripping the whole query string merges unrelated jobs
# together and they silently never get announced.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "source", "src", "gh_src", "referrer",
}


def normalize_url(url: str) -> str:
    """Reduce a URL to a stable identity.

    Drops the fragment and tracking parameters (these repos append tags
    like ?utm_source=github-vansh-ouckah, and a maintainer rewriting
    those repo-wide would otherwise make every job look brand new), while
    keeping any parameter that actually identifies the posting.
    """
    parsed = urlparse(url.strip())
    kept = {
        k: v for k, v in parse_qs(parsed.query, keep_blank_values=False).items()
        if k.lower() not in TRACKING_PARAMS
    }
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/").lower()
    query = urlencode(sorted((k, v[0]) for k, v in kept.items())) if kept else ""
    return f"{host}{path}" + (f"?{query}" if query else "")


def _row_identity(cells: list[str], link: str | None) -> str:
    if link:
        return normalize_url(link)
    trimmed = cells[:-1] if cells and AGE_CELL_RE.match(cells[-1].strip()) else cells
    return "|".join(c.strip().lower() for c in trimmed)


def _link_specificity(url: str) -> int:
    # Bare homepage links (e.g. "https://www.google.com") have an empty/trivial
    # path; real application links have a long, job-specific path. Longer
    # path = more specific = more likely to be the actual posting, not the
    # company's front page (which would collide across every job they post).
    path = url.split("://", 1)[-1].split("/", 1)[1] if "/" in url.split("://", 1)[-1] else ""
    return len(path.strip("/"))


def _best_link(links: list[str]) -> str | None:
    # Prefer the real application/ATS link over a referral/profile link.
    candidates = [l for l in links if "simplify.jobs" not in l and "airtable.com" not in l] or links
    if not candidates:
        return None
    specific = [l for l in candidates if _link_specificity(l) > 0]
    pool = specific if specific else candidates
    return max(pool, key=len)


def extract_rows_html(soup: BeautifulSoup) -> list[dict]:
    rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue  # header row (th only)
        cells = [td.get_text(strip=True) for td in tds]
        links = [a.get("href") for a in tr.find_all("a", href=True) if a.get("href")]
        link = _best_link(links)
        rows.append({"cells": cells, "link": link, "key": _row_identity(cells, link)})
    return rows


def extract_rows_markdown(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.count("|") < 3:
            continue
        if re.fullmatch(r"[|\-\s:]+", line):
            continue  # separator row, e.g. |---|---|
        raw_cells = [c.strip() for c in line.strip("|").split("|")]
        cells = []
        for c in raw_cells:
            cleaned = re.sub(r"</?br\s*/?>", ", ", c, flags=re.I)
            cleaned = re.sub(r"<[^>]+>", "", cleaned)
            cleaned = re.sub(r"\s*,\s*,\s*", ", ", cleaned).strip(" ,")
            cells.append(re.sub(r"\s+", " ", cleaned).strip())
        lowered = [c.lower() for c in cells]
        if sum(1 for c in lowered if c in HEADER_WORDS) >= 2:
            continue  # header row
        links_in_row = []
        for c in raw_cells:
            links_in_row.extend(re.findall(r'href=[\'"](https?://[^\'" ]+)[\'"]', c))
            links_in_row.extend(re.findall(r"\((https?://[^\s)]+)\)", c))
        link = _best_link(links_in_row)
        rows.append({"cells": cells, "link": link, "key": _row_identity(cells, link)})
    return rows


def _disambiguate_keys(rows: list[dict]) -> list[dict]:
    # If two rows land on the exact same key within one snapshot (this
    # happens for the rare row with no link and identical placeholder
    # text, e.g. a repo's "same company as above" convention), give the
    # 2nd+ occurrence a distinct suffix so they aren't treated as one.
    seen: dict[str, int] = {}
    for row in rows:
        seen[row["key"]] = seen.get(row["key"], 0) + 1
        if seen[row["key"]] > 1:
            row["key"] = f'{row["key"]}#{seen[row["key"]]}'
    return rows


def extract_rows(text: str) -> list[dict]:
    soup = BeautifulSoup(text, "html.parser")
    html_rows = extract_rows_html(soup)
    rows = html_rows if html_rows else extract_rows_markdown(text)
    return _disambiguate_keys(rows)


def find_new_rows(repo: str, old_text: str, new_text: str) -> list[dict]:
    """Rows present now that weren't in the previous snapshot.

    Compares using ledger_key -- the SAME identity the ledger uses. If
    this compared on the raw URL instead, a new role appearing behind an
    evergreen careers page would never become a candidate and would be
    silently missed, regardless of what the ledger thinks.
    """
    old_keys = {ledger_key(repo, r) for r in extract_rows(old_text)}
    return [r for r in extract_rows(new_text) if ledger_key(repo, r) not in old_keys]


# ---------------------------------------------------------------------------
# 3b. Permanent "already notified" ledger
# ---------------------------------------------------------------------------
# The per-repo snapshot answers "what changed since last run?", but it can
# be lost or fail to commit. The ledger is the durable record of what has
# actually been PUSHED to the phone, so a failed state commit, a reset
# snapshot, or a job being removed and re-added can't cause a repeat.
def load_ledger() -> dict:
    if not LEDGER_FILE.exists():
        return {}
    try:
        data = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        # A corrupt ledger must not cause a flood of re-notifications, so
        # treat it as "everything already seen" and rebuild going forward.
        print(f"WARNING: could not read ledger ({e}); treating as up to date.")
        return {}


def save_ledger(ledger: dict):
    LEDGER_FILE.write_text(json.dumps(ledger, indent=0, sort_keys=True), encoding="utf-8")


# A URL containing a long numeric run is a specific job requisition
# (greenhouse job IDs, Workday JR numbers, ?gh_jid=, ?token=). Those are
# unambiguous: the URL alone identifies the posting.
#
# A URL WITHOUT one is usually an evergreen careers page a company reuses
# season after season (e.g. tower-research.com/open-positions). Keying
# those on the URL alone means the page is announced once and a genuinely
# new role behind it never surfaces. Since a missed posting costs more
# than a duplicate, those fold company+role into the key.
JOB_ID_RE = re.compile(r"\d{4,}")

# Dropped from the role signature: wording that varies between repos
# without changing which job is meant.
SIG_STOPWORDS = {
    "us", "usa", "united", "states", "the", "a", "an", "of", "and", "at",
    "in", "for", "to", "onsite", "on", "site", "remote", "hybrid",
    "opportunity", "opportunities", "role", "roles", "program",
}


def _stem(token: str) -> str:
    # Crude but adequate: collapses engineer/engineering, researcher/research.
    for suffix in ("ing", "ers", "er", "s"):
        if len(token) > 5 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def role_signature(row: dict) -> str:
    """Normalized company+role, tolerant of cosmetic wording differences.

    Deliberately KEEPS years and season words -- "... Summer 2026" and
    "... Summer 2027" must stay distinct, since telling those apart is
    the entire point of this fallback.
    """
    cells = row.get("cells") or []
    company = cells[0] if cells else ""
    role = cells[1] if len(cells) > 1 else ""
    text = re.sub(r"[^a-z0-9 ]", " ", f"{company} {role}".lower())
    tokens = {_stem(t) for t in text.split() if t and t not in SIG_STOPWORDS}
    return " ".join(sorted(tokens))


def ledger_key(repo: str, row: dict) -> str:
    """Identity used for 'have I already announced this?'.

    For a row with a real application link the key is GLOBAL (no repo
    prefix), because your repos overlap heavily -- 50 URLs appear in more
    than one and one careers page appears in all four, so a repo-scoped
    key would announce the same job up to four times.

    Rows with no link fall back to a repo-scoped text key, since bare
    text like "Software Engineer Intern" could easily collide between
    two unrelated postings in different repos.
    """
    link = row.get("link")
    if not link:
        return f"{repo}::{row['key']}"

    url = normalize_url(link)
    if JOB_ID_RE.search(url):
        return url                                  # unambiguous requisition
    return f"{url}#{role_signature(row)}"           # evergreen page: add role


def migrate_ledger(ledger: dict) -> tuple[dict, int]:
    """Convert old repo-scoped URL keys to the new global form.

    Earlier versions stored "owner/repo::example.com/job". Without this,
    upgrading would make every previously-announced job look new again.
    """
    migrated, changed = {}, 0
    url_like = re.compile(r"^[\w.-]+\.[a-z]{2,}(/|\?|$)", re.I)
    known_repos = tuple(f"{r}::" for r in REPOS)
    for key, value in ledger.items():
        if key.startswith(known_repos):
            _repo_part, rest = key.split("::", 1)
            if url_like.match(rest):        # URL keys become global
                migrated.setdefault(rest, value)
                changed += 1
                continue
        migrated[key] = value               # text fallbacks stay repo-scoped
    return migrated, changed


# ---------------------------------------------------------------------------
# 4. Discord notifications
# ---------------------------------------------------------------------------
# Discord webhooks are rate limited (roughly 5 requests per 5 seconds per
# webhook), and an embed message can carry up to 10 embeds. So instead of
# one HTTP request per job -- which would get throttled the moment a repo
# adds 20 postings at once -- jobs are batched 10 to a message.
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL")

EMBEDS_PER_MESSAGE = 10
DISCORD_COLORS = {
    "SimplifyJobs/Summer2026-Internships": 0x5865F2,   # blurple
    "vanshb03/Summer2027-Internships": 0x57F287,       # green
    "speedyapply/2027-SWE-College-Jobs": 0xFEE75C,     # yellow
    "speedyapply/2027-AI-College-Jobs": 0xEB459E,      # pink
}

# Discord's documented hard caps. Exceeding any of these makes the whole
# request fail with a 400, so values get trimmed rather than risked.
MAX_EMBED_TITLE = 256
MAX_FIELD_VALUE = 1024
MAX_CONTENT = 2000


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def _post_discord(payload: dict) -> bool:
    """POST to the webhook, honouring 429 rate-limit backoff."""
    if not DISCORD_WEBHOOK:
        print(f"[DRY RUN discord] {json.dumps(payload)[:1500]}")
        return True

    for attempt in range(1, 6):
        try:
            resp = requests.post(DISCORD_WEBHOOK, json=payload, timeout=20)
        except requests.RequestException as e:
            print(f"Discord request error (attempt {attempt}): {e}")
            time.sleep(2 * attempt)
            continue

        if resp.status_code in (200, 204):
            return True

        if resp.status_code == 429:
            # Discord tells us exactly how long to wait; respect it.
            try:
                wait = float(resp.json().get("retry_after", 2))
            except (ValueError, KeyError, TypeError):
                wait = 2.0
            print(f"Discord rate limited; waiting {wait:.1f}s (attempt {attempt}).")
            time.sleep(wait + 0.5)
            continue

        print(f"Discord returned {resp.status_code}: {resp.text[:300]}")
        if 400 <= resp.status_code < 500:
            return False   # malformed payload; retrying won't help
        time.sleep(2 * attempt)

    print("Discord: giving up after repeated failures.")
    return False


def build_job_embed(repo: str, row: dict) -> dict:
    cells = row["cells"]
    company = cells[0] if cells else "Unknown"
    role = cells[1] if len(cells) > 1 else "New posting"

    embed = {
        "title": _clip(f"{company} \u2014 {role}", MAX_EMBED_TITLE),
        "color": DISCORD_COLORS.get(repo, 0x99AAB5),
        "footer": {"text": repo.split("/")[-1]},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": [],
    }
    if row.get("link"):
        embed["url"] = row["link"]

    # Cell layout differs per repo, so label by position only where it's
    # unambiguous: index 2 is location in all four, and speedyapply adds
    # a compensation column at index 3.
    if len(cells) > 2 and cells[2]:
        embed["fields"].append({"name": "Location", "value": _clip(cells[2], MAX_FIELD_VALUE), "inline": True})
    if len(cells) > 3 and cells[3] and ("$" in cells[3] or "/hr" in cells[3].lower()):
        embed["fields"].append({"name": "Pay", "value": _clip(cells[3], MAX_FIELD_VALUE), "inline": True})
    if row.get("link"):
        embed["fields"].append({"name": "Apply", "value": _clip(f"[Open posting]({row['link']})", MAX_FIELD_VALUE), "inline": True})

    return embed


def send_jobs_to_discord(pending: list[dict]) -> bool:
    """Post every new job as a Discord embed, batched to respect limits."""
    embeds = [build_job_embed(item["repo"], item["row"]) for item in pending]
    ok = True

    for i in range(0, len(embeds), EMBEDS_PER_MESSAGE):
        batch = embeds[i : i + EMBEDS_PER_MESSAGE]
        header = None
        if i == 0:
            n = len(embeds)
            header = f"**{n} new posting{'s' if n != 1 else ''}**"
        payload = {"embeds": batch}
        if header:
            payload["content"] = _clip(header, MAX_CONTENT)
        if not _post_discord(payload):
            ok = False
        # Stay comfortably under the webhook rate limit between batches.
        if i + EMBEDS_PER_MESSAGE < len(embeds):
            time.sleep(1.2)

    return ok


def send_compact_list(pending: list[dict], reason: str) -> bool:
    """Deliver a large batch as compact text lines rather than rich cards.

    Used when a run turns up an unusual number of postings. Rich embeds
    would mean dozens of messages; this keeps the volume manageable
    WITHOUT dropping anything, because a missed posting is worse than a
    noisy channel.
    """
    lines = []
    for item in pending:
        cells = item["row"]["cells"]
        company = cells[0] if cells else "?"
        role = cells[1] if len(cells) > 1 else ""
        link = item["row"].get("link")
        label = f"**{company}** — {role}".strip(" —")
        lines.append(f"{label} — <{link}>" if link else label)

    ok = _post_discord({
        "embeds": [{
            "title": "Job watcher: unusual spike",
            "description": _clip(reason, 4096),
            "color": 0xED4245,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    })

    # Pack lines into embed descriptions (4096 cap each, 6000 per message).
    chunk, size = [], 0
    def flush():
        nonlocal chunk, size, ok
        if not chunk:
            return
        if not _post_discord({"embeds": [{"description": "\n".join(chunk), "color": 0x99AAB5}]}):
            ok = False
        chunk, size = [], 0
        time.sleep(1.2)

    for line in lines:
        if size + len(line) + 1 > 3800:
            flush()
        chunk.append(line)
        size += len(line) + 1
    flush()
    return ok
    """Plain-text alert (used for spike warnings and the daily digest)."""
    embed = {
        "title": _clip(title, MAX_EMBED_TITLE),
        "description": _clip(message, 4096),
        "color": 0xED4245,   # red -- these are attention messages
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if url:
        embed["url"] = url
    _post_discord({"embeds": [embed]})


# ---------------------------------------------------------------------------
# 5. Main
# ---------------------------------------------------------------------------
def main():
    STATE_DIR.mkdir(exist_ok=True)

    # No ledger yet means either a fresh install or an upgrade from the
    # earlier snapshot-only version. Either way, seed it from what's live
    # right now and send nothing -- otherwise the first run would push
    # every posting that already exists.
    seeding = not LEDGER_FILE.exists()
    ledger = load_ledger()
    ledger, migrated = migrate_ledger(ledger)
    if migrated:
        print(f"Migrated {migrated} ledger entries to the new global key format.")
    now = datetime.now(timezone.utc).isoformat()

    pending = []          # rows that are new AND not already in the ledger
    seen_this_run = set() # guards against the same URL arriving from two repos

    for repo in REPOS:
        state_file = STATE_DIR / (repo.replace("/", "_") + ".txt")
        try:
            new_text = fetch_readme(repo)
        except Exception as e:
            print(f"Failed to fetch {repo}: {e}")
            continue

        rows = extract_rows(new_text)

        if seeding:
            for row in rows:
                ledger[ledger_key(repo, row)] = now
            state_file.write_text(new_text, encoding="utf-8")
            print(f"Seeded {len(rows)} existing postings for {repo} (no notifications)")
            continue

        # The snapshot narrows down what changed; the ledger has the final
        # say on whether it's ever been sent.
        if state_file.exists():
            candidates = find_new_rows(repo, state_file.read_text(encoding="utf-8"), new_text)
        else:
            # Snapshot lost (e.g. a state commit failed). Fall back to
            # checking every current row against the ledger.
            print(f"No snapshot for {repo}; falling back to full ledger comparison.")
            candidates = rows

        unsent = []
        for r in candidates:
            k = ledger_key(repo, r)
            if k in ledger or k in seen_this_run:
                continue
            seen_this_run.add(k)   # same job in two repos -> announce once
            unsent.append(r)
        skipped = len(candidates) - len(unsent)
        if skipped:
            print(f"{repo}: skipped {skipped} row(s) already in the ledger.")

        for row in unsent:
            pending.append({"repo": repo, "row": row})

        state_file.write_text(new_text, encoding="utf-8")

    if seeding:
        save_ledger(ledger)
        print(f"Ledger seeded with {len(ledger)} postings. Future runs will notify only on genuinely new ones.")
        return

    # Flood valve: a huge jump almost always means a repo changed its link
    # format, not that 200 jobs opened at once. Record them all so they
    # can't fire again later, but send a single heads-up.
    if len(pending) > FLOOD_THRESHOLD:
        by_repo = {}
        for item in pending:
            by_repo[item["repo"]] = by_repo.get(item["repo"], 0) + 1
        breakdown = ", ".join(f"{r.split('/')[-1]}: {n}" for r, n in by_repo.items())
        send_compact_list(pending, (
            f"{len(pending)} postings looked new this run ({breakdown}). That usually means a "
            "repo changed its link format rather than a real surge. Listing them compactly "
            "below rather than as individual cards \u2014 nothing has been dropped."
        ))
        print(f"FLOOD GUARD: {len(pending)} rows exceeded threshold of {FLOOD_THRESHOLD}; sent as a compact list.")
    else:
        send_jobs_to_discord(pending)

    # Mark everything handled this run as notified, and log it, whether it
    # went out individually or was rolled into the spike summary.
    if pending:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            for item in pending:
                repo, row = item["repo"], item["row"]
                ledger[ledger_key(repo, row)] = now
                f.write(json.dumps({
                    "repo": repo,
                    "cells": row["cells"],
                    "link": row["link"],
                    "seen_at": now,
                }) + "\n")

    save_ledger(ledger)
    print(f"Done. {len(pending)} new posting(s) this run. Ledger holds {len(ledger)} known postings.")


if __name__ == "__main__":
    main()
