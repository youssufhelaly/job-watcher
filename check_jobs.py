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
# Each entry is (owner/repo, file). Several of these repos keep more than
# one list -- speedyapply splits USA/International and Internship/New-Grad
# across four separate files, and only the README is linked as the "main"
# one. Watching just the README there would miss roughly 85% of the repo.
# Each entry is (owner/repo, file, filter).
#
#   "all"        -- take every row in the file
#   "intern_coop"-- take only rows whose role reads as an internship or
#                   co-op. Used on the new-grad lists, which are mostly
#                   full-time graduate roles you don't want, but which
#                   do contain the occasional stray co-op.
#
# Several of these repos keep more than one list: speedyapply splits
# USA/International and Internship/New-Grad across four files, and only
# the README is linked as the "main" one. Watching just the README there
# would miss roughly 85% of the repo.
SOURCES = [
    ("SimplifyJobs/Summer2026-Internships", "README.md",       "all"),
    ("vanshb03/Summer2027-Internships",     "README.md",       "all"),

    ("speedyapply/2027-SWE-College-Jobs",   "README.md",       "all"),
    ("speedyapply/2027-SWE-College-Jobs",   "INTERN_INTL.md",  "all"),
    ("speedyapply/2027-AI-College-Jobs",    "README.md",       "all"),
    ("speedyapply/2027-AI-College-Jobs",    "INTERN_INTL.md",  "all"),

    # New-grad lists: scanned, but only intern/co-op rows are kept. A
    # handful of co-ops get filed here by mistake and would otherwise be
    # missed entirely.
    ("speedyapply/2027-SWE-College-Jobs",   "NEW_GRAD_USA.md",  "intern_coop"),
    ("speedyapply/2027-SWE-College-Jobs",   "NEW_GRAD_INTL.md", "intern_coop"),
    ("speedyapply/2027-AI-College-Jobs",    "NEW_GRAD_USA.md",  "intern_coop"),
    ("speedyapply/2027-AI-College-Jobs",    "NEW_GRAD_INTL.md", "intern_coop"),
]

# Matches "Intern", "Internship", "Co-op", "Coop", "Co op", "Co-op_Spring"
# -- but NOT "internal". Plain \b fails here because an underscore counts
# as a word character, so "Co-op_Spring 2027" wouldn't match; the explicit
# lookarounds treat anything non-alphanumeric as a boundary.
INTERN_COOP_RE = re.compile(
    r"(?<![a-z0-9])(intern(ship|s)?|co[-\s_]?ops?)(?![a-z0-9])", re.I
)


def keep_row(row: dict, mode: str) -> bool:
    if mode == "all":
        return True
    cells = row.get("cells") or []
    role = cells[1] if len(cells) > 1 else ""
    return bool(INTERN_COOP_RE.search(role))


# Used for ledger-migration detection and nothing else.
REPOS = sorted({repo for repo, _, _ in SOURCES})


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
def source_id(repo: str, path: str) -> str:
    """Stable label for a (repo, file) pair, e.g. 2027-SWE-College-Jobs/INTERN_INTL."""
    stem = path.rsplit(".", 1)[0]
    short = repo.split("/")[-1]
    return short if stem.upper() == "README" else f"{short}/{stem}"


def fetch_file(owner_repo: str, path: str) -> str:
    """Fetch one file from a repo, trying the API first then the raw CDN."""
    headers = {"Accept": "application/vnd.github.raw+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    try:
        resp = requests.get(
            f"{GITHUB_API}/repos/{owner_repo}/contents/{path}", headers=headers, timeout=30
        )
        resp.raise_for_status()
        text = resp.text
        if text.lstrip().startswith(("#", "<", "!", "[", "|")):
            return text
        data = resp.json()
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception:
        # Unauthenticated API calls get rate limited fast; the CDN doesn't.
        for branch in ("main", "master"):
            r = requests.get(
                f"https://raw.githubusercontent.com/{owner_repo}/{branch}/{path}", timeout=30
            )
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
    """Length of the identifying part of a URL -- its path plus any query
    parameter that actually names the posting.

    Bare homepage links (e.g. "https://www.google.com") score 0; real
    application links score high. Measured off normalize_url so tracking
    params don't count -- otherwise a homepage padded with ?utm_campaign=...
    would out-score a genuine job link.
    """
    ident = normalize_url(url)
    host = ident.split("/", 1)[0].split("?", 1)[0]
    return len(ident) - len(host)


def _best_link(links: list[str]) -> str | None:
    # Prefer the real application/ATS link over a referral/profile link.
    candidates = [l for l in links if "simplify.jobs" not in l and "airtable.com" not in l] or links
    if not candidates:
        return None
    specific = [l for l in candidates if _link_specificity(l) > 0]
    pool = specific if specific else candidates
    # Most specific wins. Ties go to the shortest URL, so the pick (and
    # therefore the row's identity in the ledger) stays stable run to run.
    return max(pool, key=lambda u: (_link_specificity(u), -len(u)))


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


def find_new_rows(repo: str, old_text: str, new_text: str, mode: str = "all") -> list[dict]:
    """Rows present now that weren't in the previous snapshot.

    Compares using ledger_key -- the SAME identity the ledger uses. If
    this compared on the raw URL instead, a new role appearing behind an
    evergreen careers page would never become a candidate and would be
    silently missed, regardless of what the ledger thinks.

    The filter is applied to BOTH sides, so a row that the filter
    excludes can never register as an addition or a removal.
    """
    old_keys = {ledger_key(repo, r) for r in extract_rows(old_text) if keep_row(r, mode)}
    return [
        r for r in extract_rows(new_text)
        if keep_row(r, mode) and ledger_key(repo, r) not in old_keys
    ]


# ---------------------------------------------------------------------------
# 3b. Permanent "already notified" ledger
# ---------------------------------------------------------------------------
# The per-repo snapshot answers "what changed since last run?", but it can
# be lost or fail to commit. The ledger is the durable record of what has
# actually been PUSHED to the phone, so a failed state commit, a reset
# snapshot, or a job being removed and re-added can't cause a repeat.
class LedgerError(RuntimeError):
    """The ledger exists but can't be trusted."""


def load_ledger() -> dict:
    if not LEDGER_FILE.exists():
        return {}
    try:
        data = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise LedgerError(f"could not read ledger: {e}") from e
    if not isinstance(data, dict):
        raise LedgerError(f"ledger is a {type(data).__name__}, expected an object")
    return data


def save_ledger(ledger: dict):
    if DRY_RUN:
        return
    LEDGER_FILE.write_text(json.dumps(ledger, indent=0, sort_keys=True), encoding="utf-8")


def write_snapshot(state_file: Path, text: str):
    if DRY_RUN:
        return
    state_file.write_text(text, encoding="utf-8")


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
# webhook) and a message can carry up to 10 embeds, so batching was the
# obvious way to survive a repo dumping 20 postings at once.
#
# One job per message costs more requests, but a reaction attaches to a
# MESSAGE, not to an embed -- so triage marks (applied / no / save) are
# meaningless on a ten-job card. The flood path below still batches,
# because a 200-row false positive is noise nobody triages anyway.
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL")

# With no webhook configured there is nowhere to deliver, so the run only
# prints what it would have sent and persists nothing. Writing state in
# that mode would mark postings as notified that nobody ever received.
DRY_RUN = not DISCORD_WEBHOOK

EMBEDS_PER_MESSAGE = 1
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


def build_job_embed(repo: str, row: dict, label: str | None = None) -> dict:
    cells = row["cells"]
    company = cells[0] if cells else "Unknown"
    role = cells[1] if len(cells) > 1 else "New posting"

    embed = {
        "title": _clip(f"{company} \u2014 {role}", MAX_EMBED_TITLE),
        "color": DISCORD_COLORS.get(repo, 0x99AAB5),
        "footer": {"text": label or repo.split("/")[-1]},
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


def send_jobs_to_discord(pending: list[dict]) -> list[dict]:
    """Post every new job as its own Discord card, one per message.

    Returns the items that actually reached Discord. Anything missing from
    that list must NOT be written to the ledger -- leaving it out is what
    makes the next run retry it instead of dropping it silently.
    """
    delivered = []

    for i in range(0, len(pending), EMBEDS_PER_MESSAGE):
        batch = pending[i : i + EMBEDS_PER_MESSAGE]
        payload = {"embeds": [
            build_job_embed(item["repo"], item["row"], item.get("label")) for item in batch
        ]}
        if i == 0:
            n = len(pending)
            payload["content"] = _clip(f"**{n} new posting{'s' if n != 1 else ''}**", MAX_CONTENT)

        if _post_discord(payload):
            delivered.extend(batch)
        else:
            print(f"Batch of {len(batch)} posting(s) failed to send; they will be retried next run.")
        # Stay comfortably under the webhook rate limit between batches.
        if i + EMBEDS_PER_MESSAGE < len(pending):
            time.sleep(1.2)

    return delivered


def _compact_line(item: dict) -> str:
    cells = item["row"]["cells"]
    company = cells[0] if cells else "?"
    role = cells[1] if len(cells) > 1 else ""
    link = item["row"].get("link")
    label = f"**{company}** — {role}".strip(" —")
    return f"{label} — <{link}>" if link else label


def send_compact_list(pending: list[dict], reason: str) -> list[dict]:
    """Deliver a large batch as compact text lines rather than rich cards.

    Used when a run turns up an unusual number of postings. Rich embeds
    would mean dozens of messages; this keeps the volume manageable
    WITHOUT dropping anything, because a missed posting is worse than a
    noisy channel.

    Returns the items that actually reached Discord, same contract as
    send_jobs_to_discord.
    """
    delivered = []

    _post_discord({
        "embeds": [{
            "title": "Job watcher: unusual spike",
            "description": _clip(reason, 4096),
            "color": 0xED4245,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    })

    # Pack lines into embed descriptions (4096 cap each, 6000 per message).
    # Items travel alongside their rendered line so a failed chunk leaves
    # exactly those postings out of the delivered set.
    chunk, size = [], 0
    def flush():
        nonlocal chunk, size
        if not chunk:
            return
        body = "\n".join(_compact_line(item) for item in chunk)
        if _post_discord({"embeds": [{"description": body, "color": 0x99AAB5}]}):
            delivered.extend(chunk)
        else:
            print(f"Compact chunk of {len(chunk)} posting(s) failed to send; they will be retried next run.")
        chunk, size = [], 0
        time.sleep(1.2)

    for item in pending:
        line_len = len(_compact_line(item)) + 1
        if size + line_len > 3800:
            flush()
        chunk.append(item)
        size += line_len
    flush()
    return delivered


# ---------------------------------------------------------------------------
# 5. Main
# ---------------------------------------------------------------------------
def main():
    STATE_DIR.mkdir(exist_ok=True)
    if DRY_RUN:
        print("DISCORD_WEBHOOK_URL is not set -- dry run. Notifications are printed "
              "below and no snapshot or ledger changes will be saved.")

    # No ledger yet means either a fresh install or an upgrade from the
    # earlier snapshot-only version. Either way, seed it from what's live
    # right now and send nothing -- otherwise the first run would push
    # every posting that already exists.
    seeding = not LEDGER_FILE.exists()
    try:
        ledger = load_ledger()
    except LedgerError as e:
        # An unreadable ledger can't be trusted to answer "already sent?",
        # and carrying on with an empty one would re-announce every live
        # posting. Keep the bad copy for inspection and re-seed instead:
        # one silent run, then normal operation resumes.
        backup = LEDGER_FILE.with_name("notified.corrupt.json")
        if not DRY_RUN:
            LEDGER_FILE.replace(backup)
        print(f"::error::Ledger unreadable ({e}). Moved it to {backup} and re-seeding "
              "from what is live now; no alerts this run.")
        ledger, seeding = {}, True
    ledger, migrated = migrate_ledger(ledger)
    if migrated:
        print(f"Migrated {migrated} ledger entries to the new global key format.")
    now = datetime.now(timezone.utc).isoformat()

    pending = []          # rows that are new AND not already in the ledger
    seen_this_run = set() # guards against the same URL arriving from two repos
    # Snapshots are held back until delivery is confirmed. Advancing a
    # snapshot past a posting that never made it to Discord would hide the
    # row from the next run's diff, and the ledger wouldn't catch it either
    # because an undelivered posting is deliberately never recorded there.
    snapshots = {}        # state_file -> new file text, written at the end

    for repo, path, mode in SOURCES:
        label = source_id(repo, path)
        state_file = STATE_DIR / (f"{repo}_{path}".replace("/", "_").replace(".md", "") + ".txt")
        try:
            new_text = fetch_file(repo, path)
        except Exception as e:
            print(f"Failed to fetch {label}: {e}")
            continue

        rows = [r for r in extract_rows(new_text) if keep_row(r, mode)]

        if seeding:
            for row in rows:
                ledger[ledger_key(repo, row)] = now
            write_snapshot(state_file, new_text)
            print(f"Seeded {len(rows):4} postings from {label}")
            continue

        # The snapshot narrows down what changed; the ledger has the final
        # say on whether it's ever been sent.
        if state_file.exists():
            candidates = find_new_rows(repo, state_file.read_text(encoding="utf-8"), new_text, mode)
        else:
            # Snapshot lost (e.g. a state commit failed). Fall back to
            # checking every current row against the ledger.
            print(f"No snapshot for {label}; falling back to full ledger comparison.")
            candidates = rows

        unsent = []
        for r in candidates:
            k = ledger_key(repo, r)
            if k in ledger or k in seen_this_run:
                continue
            seen_this_run.add(k)   # same job in two lists -> announce once
            unsent.append(r)
        skipped = len(candidates) - len(unsent)
        if skipped:
            print(f"{label}: skipped {skipped} row(s) already known.")

        for row in unsent:
            pending.append({"repo": repo, "label": label, "row": row, "state_file": state_file})

        snapshots[state_file] = new_text

    if seeding:
        save_ledger(ledger)
        print(f"Ledger seeded with {len(ledger)} postings. Future runs will notify only on genuinely new ones.")
        return

    # Flood valve: a huge jump almost always means a repo changed its link
    # format, not that 200 jobs opened at once. Send them all, but as one
    # compact list rather than a few hundred individual cards.
    if len(pending) > FLOOD_THRESHOLD:
        by_repo = {}
        for item in pending:
            k = item.get("label") or item["repo"]
            by_repo[k] = by_repo.get(k, 0) + 1
        breakdown = ", ".join(f"{r}: {n}" for r, n in by_repo.items())
        delivered = send_compact_list(pending, (
            f"{len(pending)} postings looked new this run ({breakdown}). That usually means a "
            "repo changed its link format rather than a real surge. Listing them compactly "
            "below rather than as individual cards \u2014 nothing has been dropped."
        ))
        print(f"FLOOD GUARD: {len(pending)} rows exceeded threshold of {FLOOD_THRESHOLD}; sent as a compact list.")
    else:
        delivered = send_jobs_to_discord(pending)

    # Only what Discord actually accepted goes in the ledger. The rest is
    # left untouched on purpose so the next run picks it up again -- these
    # are the same objects that went into `pending`, hence the identity set.
    delivered_ids = {id(item) for item in delivered}
    stale_sources = {item["state_file"] for item in pending if id(item) not in delivered_ids}

    if delivered:
        for item in delivered:
            ledger[ledger_key(item["repo"], item["row"])] = now
        if not DRY_RUN:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                for item in delivered:
                    row = item["row"]
                    f.write(json.dumps({
                        "repo": item["repo"],
                        "source": item.get("label"),
                        "cells": row["cells"],
                        "link": row["link"],
                        "seen_at": now,
                    }) + "\n")

    save_ledger(ledger)

    # A source holding undelivered postings keeps its old snapshot, so the
    # next run re-detects them. Sources that fully delivered move forward.
    for state_file, text in snapshots.items():
        if state_file in stale_sources:
            continue
        write_snapshot(state_file, text)

    undelivered = len(pending) - len(delivered)
    if undelivered:
        print(f"::warning::{undelivered} posting(s) could not be delivered to Discord. They were "
              f"left out of the ledger and {len(stale_sources)} snapshot(s) held back so the next run retries them.")
    print(f"Done. {len(delivered)} posting(s) sent this run. Ledger holds {len(ledger)} known postings.")


if __name__ == "__main__":
    main()
