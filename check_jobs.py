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

DISCORD_BOT_TOKEN is optional and does one thing: pre-place the triage
emoji on each card, which a webhook is not allowed to do. Postings
deliver normally without it.
"""

import os
import re
import json
import base64
import time
from datetime import datetime, timezone, date
from urllib.parse import urlparse, parse_qs, urlencode, quote
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
    # Renamed from Summer2026-Internships when the season rolled over on
    # 2026-07-29. GitHub 301s the old name, so the fetch kept working and
    # the swap was invisible until 76 rows looked new at once -- pin the
    # current name so the source label matches what the rows actually are.
    ("SimplifyJobs/Summer2027-Internships", "README.md",       "all"),
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


# Roles to drop everywhere, on every list, regardless of the filter mode.
#
# Two separate things get dropped, and they fail the intern/co-op filter
# in opposite ways:
#
#   grad-level -- a real internship, but for PhD / master's / MBA
#                 students. "Quantitative Researcher PhD Intern" is an
#                 internship you cannot apply to.
#   new-grad   -- not an internship at all, just filed as if it were.
#
# Set either flag to 0 to get those back as tagged-but-delivered.
EXCLUDE_GRAD_LEVEL = os.environ.get("EXCLUDE_GRAD_LEVEL", "1") != "0"
EXCLUDE_NEW_GRAD = os.environ.get("EXCLUDE_NEW_GRAD", "1") != "0"

# Markers that an internship is pitched at postgraduates. These override
# the intern/co-op check -- the role IS an internship, which is exactly
# why the word "intern" can't rescue it.
GRAD_LEVEL_TERMS = [
    "phd", "ph.d", "ph.d.", "doctoral", "doctorate",
    "graduate student", "grad student", "graduate students",
    "masters student", "master's student", "ms/phd", "msc/phd",
    "graduate intern", "graduate research", "graduate level",
    "postgraduate", "post-graduate", "mba",
]

# Phrases, never the bare word "graduate" -- these lists carry real
# internships called "Graduation Internship" (Accenture runs several),
# and matching "graduate" alone would throw them away. Every term here
# names a full-time entry-level role.
NEW_GRAD_TERMS = [
    "new grad", "new grads", "new graduate", "new graduates", "newgrad",
    "graduate program", "graduate programme", "graduate scheme",
    "graduate rotational", "grad program", "graduate trainee",
    "graduate analyst", "graduate engineer", "graduate developer",
    "graduate consultant", "graduate scientist", "campus hire",
    "entry level", "entry-level",
]


def excluded_role(role: str) -> str | None:
    """Why this role is being dropped, or None to keep it."""
    # Grad-level overrides everything, including the intern check.
    if EXCLUDE_GRAD_LEVEL and _GRAD_LEVEL_RE.search(role):
        return "grad-level"

    # A title LEADING with "Graduate" means a graduate-student role in
    # both conventions -- UK new-grad ("Graduate Software Engineer") and
    # US grad-student ("Graduate Research Intern") -- so it drops even
    # when the title also says intern. "Graduation Internship" is a
    # different thing (a thesis project) and is not matched.
    if EXCLUDE_NEW_GRAD and re.match(r"\s*graduate(?![a-z])", role, re.I):
        return "new-grad"

    # A new-grad phrase does NOT override an explicit intern/co-op role.
    # Real examples that must survive: Boeing's "Intern to Entry Level
    # Conversion Intern Program" and "Entry-Level Software Engineer -
    # Internship - Fresh Graduate". Both are internships that happen to
    # contain "entry level".
    # A title LEADING with "Graduate" is the British new-grad convention
    # ("Graduate Software Engineer"). Anchored to the start so
    # "Graduation Internship ..." and "Graduate Research Intern" -- which
    # the intern check below rescues anyway -- aren't caught by accident.
    if EXCLUDE_NEW_GRAD and _NEW_GRAD_RE.search(role) and not INTERN_COOP_RE.search(role):
        return "new-grad"

    return None


# "Internship application is closed" in both Simplify's and vanshb03's
# legends. Worth dropping for two reasons, and the second one is not
# obvious: the marker does not sit ALONGSIDE the apply link, it REPLACES
# it. So a row changes identity the moment it closes -- ledger_key falls
# off its URL branch onto the repo-scoped text branch, the ledger has no
# record of that second key, and the posting is announced again at
# exactly the moment it stops being applicable.
#
# find_new_rows filters both snapshots, so a row closing now registers as
# neither an addition nor a removal -- it just goes quiet. And if it
# reopens, the link comes back, the key reverts to the URL already sitting
# in the ledger, and it stays suppressed.
CLOSED_MARKER = "\U0001F512"          # 🔒


def keep_row(row: dict, mode: str) -> bool:
    cells = row.get("cells") or []
    role = cells[1] if len(cells) > 1 else ""

    # Checked across every cell, not just the role: these repos put the
    # marker in whichever column held the apply link.
    if any(CLOSED_MARKER in c for c in cells):
        return False

    if excluded_role(role):
        return False

    if mode == "all":
        return True
    return bool(INTERN_COOP_RE.search(role))


# ---------------------------------------------------------------------------
# 1b. CATEGORIES -- the tags stamped on each card
# ---------------------------------------------------------------------------
# Ten source files with overlapping contents means "where did this come
# from" is not obvious from the posting itself, and the interesting
# questions (is it a big name? is it AI? can I even apply?) aren't
# answered by the source at all. So every posting gets classified on four
# axes and the tags ride along on the card.
#
# All of it is derived from the row text, so it costs nothing per run and
# can't fail a delivery. Everything below is meant to be edited -- add
# companies you care about, drop tiers you don't.

# Company tiers, most specific first: a name matched by an earlier tier
# is not tested against later ones (Google is FAANG, not Big Tech).
COMPANY_TIERS = [
    ("⭐ FAANG", [
        "meta", "facebook", "apple", "amazon", "aws", "netflix",
        "google", "alphabet", "youtube",
    ]),
    ("\U0001F9EA AI lab", [
        "openai", "anthropic", "deepmind", "google deepmind", "mistral",
        "cohere", "scale ai", "perplexity", "xai", "hugging face",
        "figure", "waymo", "cruise", "midjourney", "runway",
        "character.ai", "inflection", "adept", "sakana",
    ]),
    ("\U0001F4C8 Quant", [
        "jane street", "citadel", "citadel securities", "two sigma",
        "hudson river trading", "hrt", "optiver", "imc", "imc trading",
        "jump trading", "de shaw", "d. e. shaw", "akuna capital",
        "susquehanna", "sig", "point72", "millennium", "drw",
        "old mission", "belvedere", "five rings", "virtu",
        "tower research", "xtx", "squarepoint", "balyasny", "cubist",
        "peak6", "group one", "radix", "qube", "marshall wace",
    ]),
    ("\U0001F3E2 Big Tech", [
        "microsoft", "nvidia", "tesla", "uber", "lyft", "airbnb",
        "stripe", "databricks", "snowflake", "salesforce", "adobe",
        "intel", "amd", "qualcomm", "ibm", "oracle", "linkedin",
        "tiktok", "bytedance", "snap", "spotify", "pinterest",
        "dropbox", "cloudflare", "palantir", "roblox", "coinbase",
        "samsung", "sony", "bloomberg", "atlassian", "shopify",
        "servicenow", "vmware", "cisco", "dell", "hp", "sap",
        "paypal", "block", "square", "doordash", "instacart",
        "reddit", "discord", "figma", "notion", "canva", "twilio",
        "datadog", "mongodb", "hubspot", "zoom", "slack",
        # Names that actually show up in these lists in volume, mostly
        # semiconductor and industrial. Worth a tier because they hire
        # heavily for the hardware roles tagged below.
        "tencent", "alibaba", "baidu", "huawei", "xiaomi", "asml",
        "nxp", "bosch", "siemens", "philips", "hitachi", "panasonic",
        "ericsson", "nokia", "tsmc", "micron", "texas instruments",
        "analog devices", "broadcom", "arm", "marvell", "infineon",
        "stmicroelectronics", "renesas", "rivian", "motorola",
        "ge", "honeywell", "abb", "schneider electric", "lockheed",
    ]),
]

# Role-text domains. A posting can carry more than one of these.
ROLE_DOMAINS = [
    ("\U0001F9E0 AI/ML", [
        "machine learning", "ml", "ai", "artificial intelligence",
        "deep learning", "nlp", "llm", "computer vision", "cv",
        "data scien", "research scientist", "perception", "robotics",
        "reinforcement learning", "generative", "genai", "mlops",
    ]),
    ("\U0001F527 Hardware", [
        "hardware", "embedded", "firmware", "fpga", "asic", "rtl",
        "silicon", "verification", "signal processing", "dsp", "rf",
        "analog", "pcb", "vlsi", "soc",
    ]),
    ("\U0001F510 Security", [
        "security", "cryptography", "appsec", "infosec", "malware",
        "penetration", "vulnerability",
    ]),
]

# Roles you cannot apply to are worth flagging loudly -- several of these
# lists are thick with PhD-only research internships.
PHD_ONLY = ["phd", "ph.d", "doctoral", "doctorate"]

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
CA_PROVINCES = {"ON", "QC", "BC", "AB", "MB", "SK", "NS", "NB", "NL", "PE"}
US_NAMES = {"USA", "US", "U.S.", "U.S.A.", "UNITED STATES"}

# Placeholders, not places. Without this they'd read as "somewhere that
# isn't a US state", which the fallback would call International.
VAGUE_LOCATIONS = {
    "MULTIPLE LOCATIONS", "VARIOUS", "VARIOUS LOCATIONS", "TBD", "N/A",
    "MULTIPLE", "SEVERAL LOCATIONS", "NATIONWIDE",
}


def _term_re(terms: list[str]) -> re.Pattern:
    """Match any term as a whole word, tolerating punctuation in names.

    Same lookaround trick as INTERN_COOP_RE: plain \\b breaks on the
    short uppercase terms that matter most here. "ML" must not match
    inside "HTML", and "AI" must not match inside "Chair" -- with these
    lookarounds neither does, because the neighbouring character is
    alphanumeric in both cases.
    """
    alts = "|".join(sorted((re.escape(t) for t in terms), key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])({alts})(?![a-z0-9])", re.I)


_TIER_RES = [(tag, _term_re(names)) for tag, names in COMPANY_TIERS]
_DOMAIN_RES = [(tag, _term_re(terms)) for tag, terms in ROLE_DOMAINS]
_PHD_RE = _term_re(PHD_ONLY)
# Used by excluded_role() above, which runs long after import.
_GRAD_LEVEL_RE = _term_re(GRAD_LEVEL_TERMS)
_NEW_GRAD_RE = _term_re(NEW_GRAD_TERMS)


def _region_tags(label: str, location: str) -> list[str]:
    """Region from the location cell, with the source file as a fallback.

    Location text is more trustworthy than the file name: speedyapply's
    USA lists do carry the odd Canadian posting, and Simplify has no
    USA/INTL split at all.
    """
    loc = location or ""
    tags = []

    remote_re = re.compile(r"(?<![a-z0-9])remote(?![a-z0-9])", re.I)
    if remote_re.search(loc):
        tags.append("\U0001F3E0 Remote")
    # "Remote" on its own names no country, so it must not fall through to
    # the International guess below. "Remote, US" still has "US" to go on.
    stripped = remote_re.sub("", loc).strip(" ,;/-")
    named_place = bool(re.sub(r"[^a-z0-9]", "", stripped, flags=re.I)) \
        and stripped.upper() not in VAGUE_LOCATIONS

    # Trailing "..., NY" / "..., ON" is the reliable signal.
    tail = {p.strip().upper() for p in loc.split(",")}
    if "CANADA" in tail or tail & CA_PROVINCES:
        tags.append("\U0001F1E8\U0001F1E6 Canada")
    elif tail & US_STATES or tail & US_NAMES:
        tags.append("\U0001F1FA\U0001F1F8 USA")
    elif "INTL" in (label or "").upper():
        tags.append("\U0001F30D International")
    elif named_place:
        # A place that names neither a US state nor a province is
        # somewhere else in the world.
        tags.append("\U0001F30D International")

    return tags


def classify(repo: str, label: str, row: dict) -> list[str]:
    """Tags for one posting, in reading order: who / what / who-can / where."""
    cells = row.get("cells") or []
    company = cells[0] if cells else ""
    role = cells[1] if len(cells) > 1 else ""
    location = cells[2] if len(cells) > 2 else ""

    tags = []

    for tag, pattern in _TIER_RES:
        if pattern.search(company):
            tags.append(tag)
            break          # one tier per company

    domains = [tag for tag, pattern in _DOMAIN_RES if pattern.search(role)]
    # The AI repo is an AI list by construction, so trust it when the role
    # title alone doesn't say so ("Research Intern - Redmond").
    ai_tag = ROLE_DOMAINS[0][0]
    if "AI-College-Jobs" in repo and ai_tag not in domains:
        domains.insert(0, ai_tag)
    tags.extend(domains)

    if _PHD_RE.search(role):
        tags.append("\U0001F393 PhD")

    if "NEW_GRAD" in (label or "").upper():
        # Kept only because it read as a co-op; flagged because it was
        # filed on a new-grad list and may really be full-time.
        tags.append("⚠️ Filed new-grad")

    tags.extend(_region_tags(label, location))
    return tags


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

# Only postings this fresh are worth an alert. Being new to a LIST is not
# the same as being newly posted: these repos backfill heavily. On
# 2026-07-31 speedyapply added 68 rows in one pass, of which 13 had gone
# up within a day, 40 within four days, and 15 were over a week old --
# one of them 105 days. Age comes straight from each list's own last
# column, so the cutoff is on when the employer posted, not on when the
# scraper noticed.
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "2"))

# "3d", "10h", "2mo" (Simplify, speedyapply) ...
_AGE_RELATIVE = re.compile(r"^(\d+)\s*([a-z]+)$", re.I)
_AGE_UNIT_DAYS = {
    "h": 0, "hr": 0, "hrs": 0, "hour": 0, "hours": 0,
    "d": 1, "day": 1, "days": 1,
    "w": 7, "wk": 7, "week": 7, "weeks": 7,
    "mo": 30, "month": 30, "months": 30,
}
# ... and "Jul 24" (vanshb03, whose column is literally "Date Posted").
_AGE_DATE = re.compile(r"^([a-z]{3,9})\.?\s+(\d{1,2})$", re.I)
_AGE_MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def row_age_days(row: dict, today: date | None = None) -> int | None:
    """How long ago the posting went up, per the list's own last column.

    Returns None when that cell says neither of the two shapes above.
    Callers must read None as "can't tell" and keep the row -- a source
    that drops or reformats the column has to keep alerting rather than
    fall silent, since silence here is indistinguishable from "no jobs".

    Deliberately does NOT widen AGE_CELL_RE, which decides what trailing
    cell to strip from a link-less row's identity: extending that would
    change ledger keys and re-announce postings already sent.
    """
    cells = row.get("cells") or []
    value = cells[-1].strip() if cells else ""

    m = _AGE_RELATIVE.match(value)
    if m:
        per_unit = _AGE_UNIT_DAYS.get(m.group(2).lower())
        return int(m.group(1)) * per_unit if per_unit is not None else None

    m = _AGE_DATE.match(value)
    if m:
        month = _AGE_MONTHS.get(m.group(1)[:3].lower())
        if not month:
            return None
        today = today or datetime.now(timezone.utc).date()
        # The cell carries no year. A date ahead of today belongs to last
        # year -- "Dec 15" read in January is three weeks ago, not eleven
        # months out. One day of slack absorbs timezone skew between the
        # list's clock and ours.
        for year in (today.year, today.year - 1):
            try:
                posted = date(year, month, int(m.group(2)))
            except ValueError:
                return None                  # e.g. Feb 30
            if (posted - today).days <= 1:
                return max(0, (today - posted).days)   # tomorrow == today
    return None


def is_fresh(row: dict) -> bool:
    age = row_age_days(row)
    return age is None or age <= MAX_AGE_DAYS


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


_RENAME_WARNED = set()


def _warn_if_renamed(resp, owner_repo: str, headers: dict) -> None:
    """Flag a source whose repo has been renamed out from under it.

    GitHub answers 301 for a renamed repo and requests follows it, so the
    call returns 200 carrying a DIFFERENT list's contents. Nothing
    downstream can notice: the rows simply look new, and the flood valve
    can only report that something changed, not what. This is how
    SimplifyJobs/Summer2026-Internships -> Summer2027-Internships went
    unseen until 76 postings arrived at once on 2026-07-29.

    Diagnostic only -- the fetch still returns the redirected content,
    because dropping it would be worse than labelling it imprecisely.
    Fixing it properly means editing SOURCES and renaming the state file,
    which can't be guessed safely: rename the wrong one and you either
    re-announce a whole list or silently stop watching it.
    """
    if not resp.history or owner_repo in _RENAME_WARNED:
        return
    _RENAME_WARNED.add(owner_repo)
    # The redirect lands on an ID-based URL (/repositories/12345), which
    # doesn't name the new repo -- so ask that URL what it's called now.
    # Only ever one extra request, and only on the rename path.
    actual = ""
    try:
        base = resp.url.split("/contents/", 1)[0]
        actual = requests.get(base, headers=headers, timeout=30).json().get("full_name", "")
    except Exception:
        pass
    print(f"::warning::{owner_repo} redirected to {actual or 'another repo'}. If it was "
          f"renamed, update SOURCES and rename its state file -- until then this source's "
          f"rows come from a different list than its label says.")


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
        _warn_if_renamed(resp, owner_repo, headers)
        text = resp.text
        if text.lstrip().startswith(("#", "<", "!", "[", "|")):
            return text
        data = resp.json()
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception:
        # Unauthenticated API calls get rate limited fast; the CDN doesn't.
        # No rename check needed on this route: raw.githubusercontent does
        # NOT redirect a renamed repo, it 404s -- so a rename surfaces here
        # as an ordinary fetch failure, which the caller already reports.
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


# Two lists routinely carry the SAME posting under different URL shapes.
# Left alone each shape is its own identity, so the job is announced once
# per shape. Observed between speedyapply and SimplifyJobs:
#
#   Workday  /en-US/<site>/job/...       vs  /<site>/job/...
#   iCIMS    /jobs/<id>/<title-slug>/job vs  /jobs/<id>/job?mobile=true&...
#   Apple    /details/<id>/<title-slug>  vs  /details/<id>-<team>
#
# Each rule discards only a display detail -- the locale a page renders
# in, or a slug/suffix derived from the title -- and never the
# requisition id. Deliberately per-host rather than a general "same host
# plus same number is the same job": on greenhouse and lever the trailing
# path segment IS the posting, so a generic rule would merge distinct
# jobs and silently stop announcing them.
_LOCALE_SEG = re.compile(r"^/[a-z]{2}-[a-z]{2}(?=/)")
_ICIMS_JOB = re.compile(r"^/jobs/(\d+)(?:/|$)")
_APPLE_JOB = re.compile(r"^(?:/[a-z]{2}-[a-z]{2})?/details/(\d+)")


def _canonical_job_path(host: str, path: str) -> tuple[str, bool]:
    """Collapse a provider's interchangeable URL forms onto one path.

    Takes an already-lowercased host and path. Returns (path,
    keep_query); the query is dropped only where the requisition id in
    the path is the entire identity, so no rule here can merge two
    postings that a query param would have told apart.
    """
    if host.endswith(".myworkdayjobs.com"):
        return _LOCALE_SEG.sub("", path), True
    if host.endswith(".icims.com"):
        m = _ICIMS_JOB.match(path)
        return (f"/jobs/{m.group(1)}", False) if m else (path, True)
    if host == "jobs.apple.com":
        m = _APPLE_JOB.match(path)
        return (f"/details/{m.group(1)}", False) if m else (path, True)
    return path, True


def normalize_url(url: str) -> str:
    """Reduce a URL to a stable identity.

    Drops the fragment and tracking parameters (these repos append tags
    like ?utm_source=github-vansh-ouckah, and a maintainer rewriting
    those repo-wide would otherwise make every job look brand new), while
    keeping any parameter that actually identifies the posting, then
    folds the known interchangeable provider forms together.
    """
    parsed = urlparse(url.strip())
    kept = {
        k: v for k, v in parse_qs(parsed.query, keep_blank_values=False).items()
        if k.lower() not in TRACKING_PARAMS
    }
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path, keep_query = _canonical_job_path(host, parsed.path.rstrip("/").lower())
    query = (
        urlencode(sorted((k, v[0]) for k, v in kept.items()))
        if kept and keep_query else ""
    )
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


# Simplify and vanshb03 write the company cell as "↳" when a row belongs
# to the same company as the row above it. Left alone, that posting shows
# up as a card titled "↳ — Software Engineer Intern" and can't be matched
# against the company tiers.
CONTINUATION_MARKERS = {"↳", "⤷", "→"}   # ↳ ⤷ →


def _fill_continuation_companies(rows: list[dict]) -> list[dict]:
    """Replace "↳" company cells with the company they point back to.

    Runs AFTER the identity key is computed, deliberately: the key is
    derived from the original cells, so filling these in cannot shift a
    ledger key and cannot cause a re-notification.

    Only the explicit markers are treated as continuations. A blank
    company cell is left blank -- a wrong company name is worse than a
    missing one.
    """
    last_company = None
    for row in rows:
        cells = row.get("cells")
        if not cells:
            continue
        company = cells[0].strip()
        if company in CONTINUATION_MARKERS:
            if last_company:
                cells[0] = last_company
        elif company:
            last_company = company
    return rows


def extract_rows(text: str) -> list[dict]:
    soup = BeautifulSoup(text, "html.parser")
    html_rows = extract_rows_html(soup)
    rows = html_rows if html_rows else extract_rows_markdown(text)
    rows = _disambiguate_keys(rows)

    # The signature as the row was PARSED, with "↳" still in the company
    # cell. No longer the identity -- kept only so ledger entries written
    # before the fill-then-key switch can be recognised and carried
    # forward instead of re-announced. See legacy_ledger_key.
    for row in rows:
        row["sig_legacy"] = role_signature(row)

    # Resolve "↳" FIRST, then pin the signature, so a row keeps one
    # identity whether upstream spells the company out or abbreviates it
    # as a continuation. Upstream flips that cell freely: Simplify writes
    # "↳" only while a row sits directly under its company, so any
    # re-sort rewrites it -- and keying off the raw cell turned every
    # re-sort into a false "new posting" alert.
    rows = _fill_continuation_companies(rows)
    for row in rows:
        row["sig"] = role_signature(row)

    return rows


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
    # Prefer the signature pinned at parse time; fall back for rows built
    # by hand (tests) that never went through extract_rows.
    sig = row.get("sig") or role_signature(row)
    return f"{url}#{sig}"                           # evergreen page: add role


def legacy_ledger_key(repo: str, row: dict) -> str | None:
    """The key this row carried while "↳" was still keyed on literally.

    Returns None when the old and new forms agree, which is the common
    case -- only an evergreen link whose company cell was written as a
    continuation marker is affected. Callers use this to recognise an
    already-announced posting whose key has since changed, rather than
    announcing it a second time.

    Unlike migrate_ledger this can't be a blind pass over the ledger: the
    old and new keys are only relatable through the row that produced
    them, so it needs the parsed row in hand.
    """
    link = row.get("link")
    if not link:
        return None                     # text keys never included the signature
    url = normalize_url(link)
    if JOB_ID_RE.search(url):
        return None                     # requisition keys never included it either
    legacy = row.get("sig_legacy")
    if not legacy or legacy == row.get("sig"):
        return None
    return f"{url}#{legacy}"


def _recanonicalize_key(key: str) -> str:
    """Re-key a stored URL key through the current canonical form.

    Works on an already-normalized key ("host/path?query" with an
    optional "#signature"), not a URL, so it can't go through
    normalize_url -- that would read the whole schemeless key as a path.
    """
    head, hash_sep, sig = key.partition("#")
    base, _, query = head.partition("?")
    host, slash, rest = base.partition("/")
    path, keep_query = _canonical_job_path(host, f"/{rest}" if slash and rest else "")
    out = host + path
    if query and keep_query:
        out += f"?{query}"
    return out + (hash_sep + sig if hash_sep else "")


def migrate_ledger(ledger: dict) -> tuple[dict, int]:
    """Bring stored keys onto the current identity format.

    Two rewrites, both of which exist so that changing how identity is
    computed doesn't re-announce postings already sent:

    1. Old repo-scoped URL keys ("owner/repo::example.com/job") become
       global, since a linked row is now keyed by URL alone.
    2. URL keys are re-canonicalized (see _canonical_job_path). This also
       MERGES the pairs already stored under two provider forms -- the
       earlier timestamp wins, being when the posting actually went out.
    """
    migrated, changed = {}, 0
    url_like = re.compile(r"^[\w.-]+\.[a-z]{2,}(/|\?|$)", re.I)
    known_repos = tuple(f"{r}::" for r in REPOS)
    for key, value in ledger.items():
        new_key = key
        if key.startswith(known_repos):
            _repo_part, rest = key.split("::", 1)
            if url_like.match(rest):        # URL keys become global
                new_key = rest
        if url_like.match(new_key):         # text fallbacks stay repo-scoped
            new_key = _recanonicalize_key(new_key)
        if new_key != key:
            changed += 1
        # Both forms of one posting can be present; keep the first sending.
        migrated[new_key] = min(migrated[new_key], value) if new_key in migrated else value
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

EMBEDS_PER_MESSAGE = 1

# Triage reactions, pre-placed on each card so marking a posting is one
# click instead of a trip through the emoji picker.
#
# A webhook cannot react to its own message -- only a bot can add a
# reaction -- so this needs DISCORD_BOT_TOKEN (a bot in the server with
# View Channel + Read Message History + Add Reactions). It is OPTIONAL:
# without it everything still posts, you just add the emoji yourself.
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
TRIAGE_EMOJI = ["✅", "❌", "\U0001F516"]   # applied / no / shortlist
DISCORD_API = "https://discord.com/api/v10"

# Cards go out as the bot rather than through the webhook when a channel
# id is configured, because a webhook created in the channel-settings UI
# is not allowed to attach components -- and the Apply button IS a
# component. Needs Send Messages + Embed Links on top of the reaction
# permissions above.
#
# The button is a LINK button (style 5). Discord opens the URL itself and
# never calls back, so this stays a plain outbound script -- no endpoint,
# no signature checking, nothing to host. Buttons that report a click
# back (Applied / Saved) are a different thing entirely and would need
# all of that.
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID")
POST_AS_BOT = bool(DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID)
BUTTON_STYLE_LINK = 5

# Nowhere to deliver means the run only prints what it would have sent
# and persists nothing. Writing state in that mode would mark postings as
# notified that nobody ever received.
DRY_RUN = not (DISCORD_WEBHOOK or POST_AS_BOT)
DISCORD_COLORS = {
    "SimplifyJobs/Summer2027-Internships": 0x5865F2,   # blurple
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


def _webhook_url(want_message: bool) -> str:
    """The webhook URL, with wait=true when the caller needs the message.

    Discord returns 204 No Content by default; wait=true makes it return
    the created message, which is the only way to learn the message id
    that reactions have to be attached to. Built with urlencode rather
    than string concatenation because the configured URL may already
    carry a query string (e.g. ?thread_id=...).
    """
    if not want_message:
        return DISCORD_WEBHOOK
    parts = urlparse(DISCORD_WEBHOOK)
    query = parse_qs(parts.query)
    query["wait"] = ["true"]
    return parts._replace(query=urlencode(query, doseq=True)).geturl()


def _post_discord(payload: dict, want_message: bool = False) -> dict | None:
    """Send one message, as the bot when configured, else via webhook.

    Returns the created message on success (an empty dict when Discord
    sent no body), or None on failure. Callers must test `is not None`
    -- a successful post with no body is an empty, falsy dict.
    """
    if DRY_RUN:
        print(f"[DRY RUN discord] {json.dumps(payload)[:1500]}")
        return {}

    if POST_AS_BOT:
        url = f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages"
        headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    else:
        # Components are rejected on a UI-created webhook, so drop the
        # button rather than lose the whole card to a 400.
        payload = {k: v for k, v in payload.items() if k != "components"}
        url = _webhook_url(want_message)
        headers = None

    for attempt in range(1, 6):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=20)
        except requests.RequestException as e:
            print(f"Discord request error (attempt {attempt}): {e}")
            time.sleep(2 * attempt)
            continue

        if resp.status_code in (200, 204):
            try:
                return resp.json() if resp.content else {}
            except ValueError:
                return {}

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
            return None   # malformed payload; retrying won't help
        time.sleep(2 * attempt)

    print("Discord: giving up after repeated failures.")
    return None


def _add_triage_reactions(message: dict) -> None:
    """Pre-place the triage emoji on a posted card.

    Best effort by design. The card is already delivered by the time this
    runs, so a failure here must NOT bubble up -- treating it as a failed
    delivery would leave the posting out of the ledger and re-post it on
    the next run. A missing emoji you can add by hand; a duplicate card
    is noise in the feed.
    """
    if not (DISCORD_BOT_TOKEN and message.get("id") and message.get("channel_id")):
        return

    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    base = (f"{DISCORD_API}/channels/{message['channel_id']}"
            f"/messages/{message['id']}/reactions")

    for emoji in TRIAGE_EMOJI:
        # safe="" so the emoji is fully percent-encoded into the path.
        url = f"{base}/{quote(emoji, safe='')}/@me"
        for attempt in range(1, 4):
            try:
                resp = requests.put(url, headers=headers, timeout=20)
            except requests.RequestException as e:
                print(f"Reaction request error ({emoji}): {e}")
                break

            if resp.status_code in (200, 204):
                break
            if resp.status_code == 429:
                try:
                    wait = float(resp.json().get("retry_after", 1))
                except (ValueError, KeyError, TypeError):
                    wait = 1.0
                time.sleep(wait + 0.25)
                continue

            print(f"Reaction {emoji} failed: {resp.status_code} {resp.text[:200]}")
            break

        # Reaction endpoints are rate limited per channel and more
        # tightly than message sends, so pace them deliberately.
        time.sleep(0.3)


def build_apply_button(row: dict) -> list | None:
    """An action row holding one link button, or None if there's no link.

    Discord rejects a link button whose url isn't http(s), and some rows
    carry no application link at all -- both cases just get a card with
    no button rather than a failed send.
    """
    link = (row.get("link") or "").strip()
    if not link.startswith(("http://", "https://")):
        return None
    return [{
        "type": 1,                       # action row
        "components": [{
            "type": 2,                   # button
            "style": BUTTON_STYLE_LINK,
            "label": "Apply",
            "url": link,
        }],
    }]


def build_job_embed(repo: str, row: dict, label: str | None = None,
                    apply_field: bool | None = None) -> dict:
    # With a real Apply button on the message, the Apply field inside the
    # embed is the same link twice. Checked at call time, not bound as a
    # default, so tests can flip the transport.
    if apply_field is None:
        apply_field = not POST_AS_BOT

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
    if apply_field and row.get("link"):
        embed["fields"].append({"name": "Apply", "value": _clip(f"[Open posting]({row['link']})", MAX_FIELD_VALUE), "inline": True})

    # Tags last and full-width, so they read as one line under the
    # details rather than competing with them for a column.
    tags = classify(repo, label or "", row)
    if tags:
        embed["fields"].append({
            "name": "Tags",
            "value": _clip(" · ".join(tags), MAX_FIELD_VALUE),
            "inline": False,
        })

    return embed


# Phone push notifications show a message's CONTENT. Embed titles and
# fields don't reliably make it into the notification -- an embed-only
# message pushes as a bare "sent a message" -- so the company and role go
# in the content as well, and the card repeats them for the desktop view.
#
# Deliberately no markdown: bold survives in-channel but some clients
# show the raw asterisks in the notification itself, and a push that
# reads "**Meta** — ..." is worse than a plain one. Kept short because
# lock screens truncate around 100 characters.
MAX_PUSH_LINE = 150


def push_line(item: dict) -> str:
    """The one line you'll read on your phone: company, then role."""
    cells = item["row"]["cells"]
    company = (cells[0] if cells else "").strip() or "Unknown"
    role = (cells[1] if len(cells) > 1 else "").strip() or "New posting"
    return _clip(f"{company} — {role}", MAX_PUSH_LINE)


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
        # One posting per message is what makes a single Apply button
        # unambiguous; _post_discord strips components on the webhook
        # transport, which cannot carry them.
        if len(batch) == 1:
            components = build_apply_button(batch[0]["row"])
            if components:
                payload["components"] = components
        if len(batch) == 1:
            payload["content"] = push_line(batch[0])

        # want_message: the reactions need the id of the card just posted.
        message = _post_discord(payload, want_message=bool(DISCORD_BOT_TOKEN))
        if message is not None:
            delivered.extend(batch)
            _add_triage_reactions(message)
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
        # No triage emoji on the flood path: one message carries many
        # postings there, so a reaction on it wouldn't mean anything.
        if _post_discord({"embeds": [{"description": body, "color": 0x99AAB5}]}) is not None:
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
        print(f"Migrated {migrated} ledger entries to the current key format "
              f"({len(ledger)} distinct postings after merging duplicate URL forms).")
    now = datetime.now(timezone.utc).isoformat()

    pending = []          # rows that are new AND not already in the ledger
    seen_this_run = set() # guards against the same URL arriving from two repos
    # Snapshots are held back until delivery is confirmed. Advancing a
    # snapshot past a posting that never made it to Discord would hide the
    # row from the next run's diff, and the ledger wouldn't catch it either
    # because an undelivered posting is deliberately never recorded there.
    snapshots = {}        # state_file -> new file text, written at the end
    relabelled = 0        # ledger entries moved onto the post-"↳" key format

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

        # Drop anything the list itself says is older than the cutoff.
        # These are never recorded in the ledger and the snapshot advances
        # past them, so they will not resurface if MAX_AGE_DAYS is raised
        # later -- the point is that an old posting isn't news, not that
        # it's already been handled.
        stale = sum(1 for r in candidates if not is_fresh(r))
        if stale:
            candidates = [r for r in candidates if is_fresh(r)]
            print(f"{label}: skipped {stale} row(s) posted over {MAX_AGE_DAYS} days ago.")

        unsent = []
        for r in candidates:
            k = ledger_key(repo, r)
            if k in ledger or k in seen_this_run:
                continue
            # Recorded under the pre-"↳" key format? Then it HAS been
            # announced. Move the record onto the current key so this run
            # stays quiet and the next one doesn't have to look again.
            legacy = legacy_ledger_key(repo, r)
            if legacy and legacy in ledger:
                ledger[k] = ledger.pop(legacy)
                relabelled += 1
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

    # Flood valve: a huge jump is usually structural -- a repo rewriting
    # its links, or a list renamed/replaced wholesale (see SOURCES) -- but
    # it can also be a real season opening, and nothing here can tell the
    # two apart. So send them all either way, as one compact list rather
    # than a few hundred individual cards.
    if len(pending) > FLOOD_THRESHOLD:
        by_repo = {}
        for item in pending:
            k = item.get("label") or item["repo"]
            by_repo[k] = by_repo.get(k, 0) + 1
        breakdown = ", ".join(f"{r}: {n}" for r, n in by_repo.items())
        delivered = send_compact_list(pending, (
            f"{len(pending)} postings looked new this run ({breakdown}). Could be a real surge "
            "(a season opening), or a repo rewriting its links, or a list being renamed or "
            "replaced wholesale \u2014 this run can't tell which. Every one of them is genuinely "
            "unannounced either way, so they are all below, compactly rather than as individual "
            "cards. Nothing has been dropped."
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
                        # Logged as well as displayed, so the digest can
                        # group by tag without re-deriving anything.
                        "tags": classify(item["repo"], item.get("label") or "", row),
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
    if relabelled:
        print(f"Carried {relabelled} ledger entr(ies) onto the resolved-continuation key format.")
    print(f"Done. {len(delivered)} posting(s) sent this run. Ledger holds {len(ledger)} known postings.")


if __name__ == "__main__":
    main()
