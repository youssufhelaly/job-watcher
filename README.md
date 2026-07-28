# Internship Job Watcher

Watches up to four GitHub repos (the "who's hiring" README-table style
repos, e.g. SimplifyJobs/Summer-Internships and similar) for newly added
postings, and pushes a phone notification for each one — plus an
optional daily rundown.

Tested against a real repo's live data while building this, so the
core logic (fetch → detect new rows → notify) is confirmed working.
You'll still need to plug in your specific 4 repos and pick where
notifications go — see below.

## Why Discord (and not iMessage/Messenger)

You originally mentioned iMessage or Messenger, but both are poor fits
for automation: iMessage needs a Mac powered on and signed in 24/7
running AppleScript, and Messenger requires a reviewed Meta developer
app just to send yourself a message.

Discord is a better fit than a pure push service anyway, because the
value here is *reviewing* a running list of jobs rather than reacting
within seconds:

- Every posting stays in the channel — scrollable, searchable, so you
  can check what you've already applied to.
- Rich embeds show company, role, location, pay, and a clickable apply
  link.
- Works on desktop, where you'll actually be filling out applications.
- You already have the app, so there's nothing new to install.

## How it works

- `check_jobs.py` runs every 15 minutes (via GitHub Actions, so nothing
  runs on your own machine). It fetches each repo's README, compares it to
  the version saved from the last run, and finds rows that are
  genuinely new — using each posting's real application link as its
  identity, not its position in the file. That matters because these
  repos frequently re-sort their tables; a position-based diff would
  misfire constantly, this doesn't.
- New postings are posted to your Discord channel as embeds — company,
  role, location, pay, and a clickable apply link. They're batched 10
  to a message so a burst of listings can't trip Discord's webhook rate
  limit. A single new job posts immediately as a single card — batching
  is a ceiling, never a wait.
- `daily_digest.py` (optional, separate workflow) reads everything
  found in the last 24 hours and sends one morning summary instead of
  / in addition to the instant pushes.
- State (which postings have already been seen) is committed back into
  the repo after each run, so there's no external database needed.

## Setup (10–15 minutes)

### 1. Create a new GitHub repo — make it PUBLIC
Add all the files from this bundle, keeping the `.github/workflows/`
folder structure intact.

**Public matters at 15-minute polling.** GitHub Actions is unmetered on
public repos, but private repos on the Free plan get 2,000 Linux
minutes/month and billing rounds up to a whole minute per run. Polling
every 15 minutes is 2,880 runs/month, so a private repo would exhaust
the free tier around day 20 and then just stop running.

Nothing sensitive lives in the repo — the state files only contain
public job listings, and the Discord webhook goes in GitHub Secrets,
which stays private even on a public repo. If you'd rather keep it
private anyway, change the cron to `*/30 * * * *` (~1,440 runs/month)
to stay inside the free allowance.

### 2. Sources are already configured
Internships and co-ops only — no new-grad roles. Several of these repos
keep **more than one list**, and only one is linked as the main README,
so watching just the README would have missed most of two repos.
`check_jobs.py` reads ten files:

| Source | File | Kept | Postings |
| --- | --- | --- | --- |
| SimplifyJobs/Summer2026-Internships | README | all | 231 |
| vanshb03/Summer2027-Internships | README | all | 144 |
| speedyapply/2027-SWE-College-Jobs | README (USA) | all | 155 |
| speedyapply/2027-SWE-College-Jobs | INTERN_INTL | all | 244 |
| speedyapply/2027-AI-College-Jobs | README (USA) | all | 195 |
| speedyapply/2027-AI-College-Jobs | INTERN_INTL | all | 300 |
| speedyapply/2027-SWE-College-Jobs | NEW_GRAD_USA | co-ops only | 1 |
| speedyapply/2027-SWE-College-Jobs | NEW_GRAD_INTL | co-ops only | 1 |
| speedyapply/2027-AI-College-Jobs | NEW_GRAD_USA | co-ops only | 0 |
| speedyapply/2027-AI-College-Jobs | NEW_GRAD_INTL | co-ops only | 0 |

**1,221 unique postings** after cross-list dedupe.

**Why the new-grad files are read at all.** They're mostly full-time
graduate roles you don't want — but a couple of genuine co-ops get filed
there by mistake (e.g. "Software Engineering Co-op_Spring 2027"). Those
files are scanned with an `intern_coop` filter: 1,289 rows in, 2 kept.
Skipping them entirely would have silently lost those two.

The filter matches Intern / Internship / Co-op / Coop / Co op in the role
title, and deliberately does *not* match "Internal" or "International".
To change what's included, edit `SOURCES` at the top of `check_jobs.py`
— set a file to `"all"` to take everything from it, or drop the line to
ignore it.

SimplifyJobs keeps all five categories (Software Engineering, Product
Management, Data Science/AI/ML, Quantitative Finance, Hardware) in one
README — all captured, with Legend/FAQ/contributor tables correctly
ignored.

Two format quirks, both handled automatically:
- **SimplifyJobs** uses raw HTML `<table>` markup; the rest use markdown
  pipe tables.
- **speedyapply** rows carry two links (company homepage *and* the real
  apply link); the parser prefers the specific one, otherwise every job
  at a given company would look identical.

### 3. Create the Discord webhook
1. In Discord, pick (or create) the channel you want jobs posted to.
2. **Channel settings → Integrations → Webhooks → New Webhook.** Name it
   something like `Job Watcher`, then **Copy Webhook URL**.
3. In your GitHub repo: **Settings → Secrets and variables → Actions →
   New repository secret**, name it exactly `DISCORD_WEBHOOK_URL`, and
   paste the URL as the value.

> **Put the webhook in Secrets, never in a file.** Anyone with that URL
> can post to your channel. It's the one thing here that actually needs
> protecting.

> **Set the channel to All Messages.** Right-click the channel →
> Notification Settings → All Messages. Server channels default to
> mentions-only, which is the most common reason people set this up and
> then never hear anything.

### 4. Enable Actions and test
- Go to the **Actions** tab in your new repo, enable workflows if prompted.
- Run **"Watch for new internship postings"** manually once (Actions tab → select it → "Run workflow"). First run just saves a baseline — you won't get notifications yet, that's expected.
- Confirm the first run posted nothing and created `state/notified.json` — that's the seed pass working correctly.
- To verify Discord is wired up, delete a few lines from any file in `state/` (and remove the matching entries from `state/notified.json`), commit, then re-run the workflow — those postings should appear in your channel within a minute.
- The daily rundown is a separate workflow on its own schedule (default 9am Eastern) — edit the `cron:` line in `.github/workflows/daily-digest.yml` for your preferred time.

## Duplicates vs missed postings

The system is deliberately biased toward **showing you a posting twice
rather than never showing it at all**. Where identity is ambiguous, it
errs on the side of alerting.

Three layers prevent genuine duplicates:

1. **Per-repo snapshot** (`state/<repo>.txt`) — what changed since the
   last run.
2. **Global ledger** (`state/notified.json`) — every posting ever sent.
   A lost snapshot or failed commit can't cause a repeat.
3. **Cross-repo dedupe** — the key is the application URL, not scoped
   per repo. Your repos overlap heavily (50 shared URLs; one careers
   page appears in all four), so without this you'd get some jobs four
   times.

### How identity works

- **URL contains a job ID** (594 of 644 URLs) — e.g. `.../jobs/8041237`,
  `?gh_jid=`, `?token=`. The URL alone is the identity. Unambiguous.
- **URL has no job ID** (50 URLs) — evergreen careers pages a company
  reuses, like `tower-research.com/open-positions`. The company and role
  are folded into the key, so a *new* role behind the same page still
  alerts. Costs the occasional duplicate when two repos word a title
  differently; that's the intended trade.

Job titles are normalized before comparison (case, emoji, punctuation, a
trailing "US", engineer/engineering) but **years and season words are
deliberately preserved** — "Summer 2026" and "Summer 2027" must stay
distinct, since telling them apart is the point.

### Seasons

Identity is the application URL, not the season:

- Separate 2026 and 2027 reqs have separate URLs → an alert for each.
- The same posting cross-listed in your 2026 and 2027 repos shares one
  URL → one alert.
- A new season's role behind an evergreen careers page → alerts, thanks
  to the role-in-key fallback above.

### Verified against live content from all four repos

| Scenario | Result |
| --- | --- |
| Job cross-listed in 3 repos | 1 alert |
| Same URL path, different `?token=` | 2 alerts (different jobs) |
| New season role at an evergreen careers page | 1 alert |
| Same evergreen role, reworded ("US" suffix added) | 0 alerts |
| Age column ticks `6d → 7d` on every row | 0 alerts |
| All rows re-sorted | 0 alerts |
| Repo rewrites every `utm_source` tag | 0 alerts |
| All snapshots deleted (failed commit) | 0 alerts |
| Upgrading from an older ledger format | 0 alerts (auto-migrates) |
| 195 postings appear at once | all 195 delivered, 9 messages |

**Spike handling.** If a run turns up more than 50 apparently-new
postings — usually a repo changing its link format — you get a warning
followed by all of them as a compact list, roughly 40 per message
instead of individual cards. Nothing is dropped. Tune with
`FLOOD_THRESHOLD`.

**First run sends nothing.** It seeds the ledger with the ~1,221 unique
postings already live across your four repos. Alerts start next run.

## A couple of honest caveats

- GitHub's scheduled runs can be delayed several minutes under load,
  especially on the busy `*/15` schedule — treat 15 minutes as a target,
  not a guarantee. Runs are queued (not cancelled) if one overlaps the
  next, so nothing is skipped.
- GitHub auto-disables scheduled workflows after 60 days of *zero
  repo activity* — but since this workflow commits state back every
  run, that resets the clock automatically, so it won't happen here.
- Anyone holding the Discord webhook URL can post to that channel.
  Keep it in GitHub Secrets; if it ever leaks, delete the webhook in
  Discord and create a new one.
- A job that's removed and later re-added stays in the ledger, so it
  won't re-notify. That's usually what you want, but it does mean a
  genuinely reopened posting will be silent.
- A small number of rows (2 of 144 in vanshb03) have no link at all —
  they use the repo's "↳ same company as above" convention. These fall
  back to a repo-scoped text identity, slightly less bulletproof than a
  URL. Worst case is one spurious alert if such a row's text is edited.
- **Evergreen careers pages may occasionally double-alert.** For the 50
  URLs without a job ID, identity includes the role title. If two repos
  word the same role differently in a way the normalizer doesn't catch,
  you'll get it twice. On current data that affects about 3 postings.
  This is the deliberate trade for never missing a new role behind a
  reused careers page.
- `state/notified.json` grows over time (~725 entries at seed, tiny).
  Even after a full season it'll be well under a megabyte, so there's
  nothing to prune.

## Want it tailored further?
Send me the actual 4 repo URLs and I'll plug them in and sanity-check
each one's format the same way I tested this build — some tracker
repos have quirks (extra footnote rows, closed-listing sections, etc.)
worth spot-checking.
