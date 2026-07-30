# Internship Job Watcher

Watches up to four GitHub repos (the "who's hiring" README-table style
repos, e.g. SimplifyJobs/Summer-Internships and similar) for newly added
postings, and pushes a phone notification for each one.

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

- `check_jobs.py` runs every 30 minutes on GitHub Actions, so nothing runs
  on your own machine. The clock lives in a small Cloudflare Worker rather
  than a GitHub `cron:` — see [Scheduling](#scheduling) for why. It fetches each repo's README, compares it to
  the version saved from the last run, and finds rows that are
  genuinely new — using each posting's real application link as its
  identity, not its position in the file. That matters because these
  repos frequently re-sort their tables; a position-based diff would
  misfire constantly, this doesn't.
- New postings are posted to your Discord channel as embeds — company,
  role, location, pay, and a clickable apply link. One job per message,
  so a reaction on a message means something about that one posting
  (see [Triage](#triage)). Above `FLOOD_THRESHOLD` postings in a single
  run the flood valve takes over and sends one compact list instead —
  that many rows at once almost always means a repo changed its link
  format, and it isn't something you'd triage anyway.
- `daily_digest.py` reads everything found in the last 24 hours and
  sends one morning summary. **Currently parked** — its schedule is
  commented out in `.github/workflows/daily-digest.yml`, since the
  instant cards already cover the same postings and a daily recap on
  top is a duplicate ping. The script still works; run it by hand from
  the Actions tab, or uncomment the `cron:` to bring it back.
- State (which postings have already been seen) is committed back into
  the repo after each run, so there's no external database needed.

## Tags

Ten source files overlap heavily, so "where did this come from" isn't
obvious from the posting — and the questions that actually matter (big
name? AI? can I even apply?) aren't answered by the source at all. Every
card carries tags on four axes, all derived from the row text, so they
cost nothing per run and can't fail a delivery.

| Axis | Tags |
|---|---|
| Company | ⭐ FAANG · 🧪 AI lab · 📈 Quant · 🏢 Big Tech |
| Domain | 🧠 AI/ML · 🔧 Hardware · 🔐 Security |
| Eligibility | 🎓 PhD · ⚠️ Filed new-grad |
| Region | 🇺🇸 USA · 🇨🇦 Canada · 🌍 International · 🏠 Remote |

Measured over the postings currently in `state/`: 47% AI/ML, 50%
International, 12% Quant. **⚠️ Filed new-grad** is the one to watch — a
row kept off a new-grad list because it read as a co-op, so it may really
be full-time.

### What gets dropped

Two kinds of posting are filtered out entirely rather than tagged. They
fail the intern/co-op filter in opposite directions:

| Reason | What it is |
|---|---|
| `grad-level` | a real internship, but for PhD / master's / MBA students |
| `new-grad` | not an internship at all, just filed on these lists as one |

On the current snapshots that's **76 of 1,273 dropped, 1,197 kept** — 75
grad-level (mostly Citadel and Jane Street PhD research internships) and
1 new-grad. Set `EXCLUDE_GRAD_LEVEL=0` or `EXCLUDE_NEW_GRAD=0` in the
workflow env to get them back as tagged-but-delivered.

The distinction that makes this work is that `grad-level` overrides the
word "intern" and `new-grad` does not. "Quantitative Researcher PhD
Intern" is genuinely an internship, which is exactly why "intern" can't
rescue it. But "Intern to Entry Level Conversion Intern Program" and
"Entry-Level Software Engineer - Internship" are internships that merely
contain "entry level", so there the intern reading wins.

Three real titles the word lists are shaped around, all of which must
survive: **Graduation Internship** (a thesis project, not a graduate
role), **Undergraduate Research Intern** (the lookbehind stops "graduate
research" matching inside it), and the two "entry level" internships
above. Matching the bare word "graduate" would throw all of them away.

Region comes from the location cell first and the source file second,
because speedyapply's USA lists do carry the odd Canadian posting and
Simplify has no USA/INTL split at all. `COMPANY_TIERS` and `ROLE_DOMAINS`
in `check_jobs.py` are plain lists meant to be edited.

Tags also go into `state/new_jobs_log.jsonl`, so the digest can group by
them, and filtering on them later ("skip PhD-only") is a one-line change.

### The ↳ rows

Simplify and vanshb03 write the company as `↳` when a row belongs to the
same company as the row above — 110 of the 1,273. Those used to render as
a card titled `↳ — Software Engineer Intern` and matched no company tier;
now the real company is carried forward.

The subtlety: for postings behind an evergreen careers page, the ledger
key includes a signature built from the company cell **at notify time**,
so filling these in would rewrite those keys and re-announce the
postings. The signature is therefore pinned when the row is parsed,
before the fill. Verified against the live ledger — 0 of 1,273 rows would
re-notify.

## Triage

Three channels. Postings land in the feed; you route each one out of it.

| Channel | Holds |
|---|---|
| `#jobs` | the raw feed — every new posting the watcher finds |
| `#shortlist` | the work queue — jobs you mean to apply to, not yet done |
| `#applied` | the record — jobs you've sent |

Each card arrives with an **Apply** button and ✅ ❌ 🔖 already on it, so
both applying and marking are one click. Those need a bot — see
[Bot setup](#bot-setup-optional) — and without one everything still
works, with the apply link inside the card and no emoji.

**From the feed**, every posting gets one of three moves:

| Decision | Do this |
|---|---|
| Not interested | react ❌ |
| Apply later | Forward → `#shortlist`, react 🔖 |
| Applying now | Forward → `#applied`, react ✅ |

The reaction is what stops you re-reading a posting you've already
judged. The forward is what routes it. Applying straight from the feed
skips `#shortlist` on purpose — the queue is only for *later*, and if
everything flowed through it, it would never be empty.

**Working the queue:** open `#shortlist`, apply to something, forward it
to `#applied`, delete it from `#shortlist`. It's gone from the queue and
still on the record. A channel that only holds unprocessed work is the
whole trick — deleting is dequeuing, and Discord already does the rest.

### Bot setup (optional)

Two things a webhook is not allowed to do: react to its own message, and
attach a button. Both need a bot. Note what this bot does *not* do —
there's no server to host, no database, and nothing running between
checks. It makes outbound calls during the Actions run and that's all.

1. [Discord Developer Portal](https://discord.com/developers/applications)
   → **New Application** → **Bot** → **Reset Token**, copy it.
2. **OAuth2 → URL Generator**: scope `bot`, permissions **View
   Channel**, **Send Messages**, **Embed Links**, **Read Message
   History**, **Add Reactions**. Open the generated URL and add it to
   your server.
3. Right-click your feed channel → **Copy Channel ID** (needs Developer
   Mode on, under Settings → Advanced).
4. Repo **Settings → Secrets and variables → Actions** → two new
   secrets, `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID`.

The two secrets do different things, and setting only the token is a
valid halfway state:

| Configured | What you get |
|---|---|
| neither | webhook posts the card, apply link inside it, no emoji |
| token only | same card, plus ✅ ❌ 🔖 pre-placed |
| token + channel id | bot posts the card, Apply button, emoji |

The Apply button is why the channel id matters. Buttons are components,
components require a bot-authored message, and a bot posts to a channel
rather than to a webhook URL. With both set, `DISCORD_WEBHOOK_URL` is no
longer used for job cards — keep it, since `daily_digest.py` still uses
it.

The bot's own reaction is why each emoji shows a count of 1 — yours makes
it 2. Reactions cost three API calls per posting, paced to stay under
Discord's per-channel reaction limit, so a run with many new postings
takes a bit longer to finish delivering.

A card with no application link gets no button, and a failed reaction
still counts the posting as delivered. Both are deliberate: re-sending a
card you already received is worse than a missing button or an emoji you
add by hand.

### Why this and not buttons

Real buttons that write to a pipeline database are buildable — the same
Discord app, plus a Cloudflare Worker on its interactions endpoint to
answer clicks within Discord's 3-second window, and a small D1 table for
status. Still no always-on process, but it is a service you own and it
fails silently when it's down. The difference in daily use is clicks,
not capability: one button instead of a forward and a react.

But every tracker dies the same way, which is that you stop updating it.
So this runs first, with nothing to deploy and nothing to maintain.
After a week the answer is visible without counting anything: **did
`#shortlist` ever reach zero?** If you worked the queue down, the habit
is real and the buttons are worth building. If it silted up, buttons
would have silted up too — the bottleneck was never the clicks.

One thing to know either way: ✅ will undercount. You click through and
submit twenty minutes later on some Workday page, long after the message
scrolled away. `#applied` is a good record, your email is the complete
one. ❌ and 🔖 carry the reliable signal, because those are decisions you
make in the moment you read the posting.

And if you're ❌-ing most of what arrives, the fix isn't better tracking.
It's tighter filtering upstream in `check_jobs.py`.

## Setup (10–15 minutes)

### 1. Create a new GitHub repo — make it PUBLIC
Add all the files from this bundle, keeping the `.github/workflows/`
folder structure intact.

**Public matters at this polling rate.** GitHub Actions is unmetered on
public repos, but private repos on the Free plan get 2,000 Linux
minutes/month and billing rounds up to a whole minute per run. Polling
every 30 minutes is ~1,440 runs/month, which fits — but only just, and
only if each run stays under about a minute. On a public repo it's free
and you never have to think about it.

Nothing sensitive lives in the repo — the state files only contain
public job listings, and the Discord webhook goes in GitHub Secrets,
which stays private even on a public repo.

### 2. Sources are already configured
Internships and co-ops only — no new-grad roles. Several of these repos
keep **more than one list**, and only one is linked as the main README,
so watching just the README would have missed most of two repos.
`check_jobs.py` reads ten files:

| Source | File | Kept | Postings |
| --- | --- | --- | --- |
| SimplifyJobs/Summer2027-Internships | README | all | 99 |
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
- The daily rundown is parked and will not run. To turn it on, uncomment the `schedule:` block in `.github/workflows/daily-digest.yml` and set your preferred time.

### 5. Deploy the scheduler

`watch-jobs.yml` has no `cron:` — it only runs when something dispatches it.
The Worker in [`worker/`](worker/) is what does that, every 30 minutes.
See [Scheduling](#scheduling) below for why, then:

1. **Mint a token.** GitHub → Settings → Developer settings → **Fine-grained
   tokens** → Generate new token. Repository access: **only this repo**.
   Permissions: **Actions → Read and write**, nothing else. Copy it.
2. **Deploy.**
   ```bash
   cd worker
   npx wrangler login
   npx wrangler secret put GITHUB_TOKEN         # paste the PAT
   npx wrangler secret put DISCORD_WEBHOOK_URL  # same URL as the Actions secret
   npx wrangler deploy
   ```
3. **Verify.** Within 30 minutes a run should appear in the Actions tab with
   `workflow_dispatch` as its trigger. `npx wrangler tail` shows each
   dispatch live if you don't want to wait.

`DISCORD_WEBHOOK_URL` is optional but strongly recommended — it's how the
Worker tells you the token died instead of failing silently. Edit the
`crons` line in `worker/wrangler.jsonc` and redeploy to change the cadence.

The Worker has **no public URL** — `workers_dev` and `preview_urls` are both
off, because a cron trigger is the only thing it needs.

**On a brand-new Cloudflare account, do this before the first deploy:** open
[dash.cloudflare.com](https://dash.cloudflare.com) → **Compute (Workers)** once.
Loading that page creates your account's `workers.dev` subdomain. Cloudflare
won't register *any* trigger without one — including crons, and including when
`workers_dev` is `false` — and the deploy fails with
`Some triggers failed to deploy` / API error **10063**. The script uploads
successfully, so it looks half-broken: no schedule, no error on the code
itself. The subdomain is account-level and shared by every Worker you deploy;
creating it doesn't expose this one.

One gotcha worth stating plainly, since it's easy to get backwards:
`wrangler secret put NAME` takes the secret's **name** as the argument and
reads the **value** from the prompt that follows. Passing the value as the
argument creates a secret named after your credential — and names are not
masked in the dashboard, the CLI, or shell history. `npm test` in `worker/` exercises the dispatch logic against a
mocked GitHub API — 30 checks, no network or credentials needed.

## Scheduling

**GitHub's `schedule:` events were unusable for this.** With a `*/15` cron,
five scheduled runs landed in the first fifteen hours — one every ~2.6 hours
on average, against an expected sixty. That's documented behavior, not a
misconfiguration: schedule events are the lowest priority on the shared
runner pool, high-frequency crons get throttled hardest, and dropped ticks
are never retried. A watcher meant to catch postings early can't sit behind a
2.6-hour queue.

`workflow_dispatch` isn't throttled that way, so the schedule moved off
GitHub. `worker/src/index.js` is a Cloudflare Worker on a `*/30` cron trigger
that POSTs to the dispatch API, retries transient failures, and posts to
Discord if the token expires or loses its permissions. Free tier: 48 requests
a day against a 100,000/day allowance.

The trade is one long-lived credential outside GitHub. It's a fine-grained
PAT limited to this repo with `Actions: read and write` and nothing else, held
in Cloudflare's secret store (write-only once set, not readable from the
dashboard). Worst case is revoke and reissue.

**Rotating the token** — do this before the expiry you chose, or right away
if it leaks:

```bash
cd worker && npx wrangler secret put GITHUB_TOKEN   # paste the new PAT
```

No redeploy needed. Then delete the old token on GitHub.

**The daily digest needs no trigger.** Its schedule is commented out, so
nothing fires it — run it from the Actions tab when you want it. If you
ever re-enable the `cron:`, note that once-a-day timing tolerates hours
of slop, so it doesn't need the Worker.

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

- **The 30-minute cadence depends on the Worker being deployed.** If you
  skip [step 5](#5-deploy-the-scheduler), `watch-jobs.yml` has no trigger at
  all and will only ever run when you click "Run workflow". This is the
  deliberate trade for punctuality — see [Scheduling](#scheduling). Runs are
  queued (not cancelled) if one overlaps the next, so nothing is skipped.
- **The PAT expires.** When it does, the Worker starts getting 401s and
  alerts your Discord channel, but no postings are checked until you rotate
  it. Set a calendar reminder for a week before the expiry you chose.
- GitHub auto-disables *scheduled* workflows after 60 days of zero repo
  activity. Neither workflow has a schedule any more — `watch-jobs.yml`
  is dispatched by the Worker and the digest is parked — so it can't
  affect either one.
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
