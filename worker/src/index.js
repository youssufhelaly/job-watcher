// Cron-triggered dispatcher for the job-watcher GitHub Actions workflow.
//
// Why this exists: `schedule:` events on the repo were being throttled to
// roughly one run every 2.6 hours despite a "*/15" cron. `workflow_dispatch`
// is not subject to that throttling, so Cloudflare keeps the time and GitHub
// just does the work.

const ATTEMPTS = 3;
const BACKOFF_MS = [2000, 5000];   // waits between attempt 1->2 and 2->3

export default {
  async scheduled(event, env, ctx) {
    // waitUntil keeps the Worker alive through the retries; without it the
    // runtime can tear us down as soon as scheduled() returns.
    ctx.waitUntil(run(env, "cron"));
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/") {
      return json(200, {
        worker: "job-watcher-trigger",
        target: `${env.GITHUB_OWNER}/${env.GITHUB_REPO}/${env.GITHUB_WORKFLOW}@${env.GITHUB_REF}`,
        schedule: "*/30 * * * *",
        manual_trigger: env.TRIGGER_SECRET ? "POST /trigger" : "disabled (TRIGGER_SECRET unset)",
      });
    }

    if (request.method === "POST" && url.pathname === "/trigger") {
      // An unauthenticated dispatch endpoint would let anyone on the internet
      // drive the repo's Actions, so it stays off unless a secret is set.
      if (!env.TRIGGER_SECRET) {
        return json(403, { error: "Manual trigger disabled. Set TRIGGER_SECRET to enable." });
      }
      const presented = (request.headers.get("authorization") || "").replace(/^Bearer /, "");
      if (!(await secretsMatch(presented, env.TRIGGER_SECRET))) {
        return json(401, { error: "Bad or missing bearer token." });
      }
      const result = await run(env, "manual");
      return json(result.ok ? 200 : 502, result);
    }

    return json(404, { error: "Not found." });
  },
};

async function run(env, source) {
  if (!env.GITHUB_TOKEN) {
    const error = "GITHUB_TOKEN is not set. Run: npx wrangler secret put GITHUB_TOKEN";
    log("error", { event: "misconfigured", source, error });
    await alert(env, `job-watcher trigger is misconfigured: ${error}`);
    return { ok: false, error };
  }

  const endpoint = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}` +
    `/actions/workflows/${env.GITHUB_WORKFLOW}/dispatches`;

  let last = null;

  for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
    let status, body;
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Accept": "application/vnd.github+json",
          "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
          "X-GitHub-Api-Version": "2022-11-28",
          "Content-Type": "application/json",
          // GitHub rejects API calls without a User-Agent.
          "User-Agent": "job-watcher-trigger",
        },
        body: JSON.stringify({ ref: env.GITHUB_REF }),
      });
      status = res.status;
      body = status === 204 ? "" : (await res.text()).slice(0, 500);
    } catch (e) {
      status = 0;
      body = String(e);
    }

    if (status === 204) {
      log("log", { event: "dispatched", workflow: env.GITHUB_WORKFLOW, source, attempt });
      return { ok: true, source, attempt };
    }

    last = { status, body };
    log("error", { event: "dispatch_failed", source, attempt, of: ATTEMPTS, status, body });

    // A dead or under-scoped token will fail identically forever -- retrying
    // wastes time and delays the alert that actually needs a human.
    if (status === 401 || status === 403 || status === 404) break;

    if (attempt < ATTEMPTS) await sleep(BACKOFF_MS[attempt - 1]);
  }

  await alert(env, describe(env, last));
  return { ok: false, source, ...last };
}

function describe(env, last) {
  const where = `${env.GITHUB_OWNER}/${env.GITHUB_REPO} (${env.GITHUB_WORKFLOW})`;
  if (!last) return `Could not dispatch ${where}: no response.`;

  if (last.status === 401) {
    return `**The job watcher has stopped.** GitHub rejected the token (401) when dispatching ` +
      `${where}. The fine-grained PAT has most likely expired or been revoked. Mint a new one ` +
      `(repo access: ${env.GITHUB_REPO}, permission: Actions read+write) and run ` +
      `\`npx wrangler secret put GITHUB_TOKEN\`. No postings are being checked until then.`;
  }
  if (last.status === 403) {
    return `**The job watcher has stopped.** GitHub returned 403 dispatching ${where} -- the ` +
      `token is valid but lacks \`Actions: read and write\` on this repo, or is rate limited. ` +
      `No postings are being checked until this is fixed.`;
  }
  if (last.status === 404) {
    return `**The job watcher has stopped.** GitHub returned 404 dispatching ${where}. Either ` +
      `the token can't see the repo, or \`${env.GITHUB_WORKFLOW}\` is missing its ` +
      `\`workflow_dispatch:\` trigger on \`${env.GITHUB_REF}\`.`;
  }
  return `Could not dispatch ${where} after ${ATTEMPTS} attempts. Last response: ` +
    `${last.status} ${last.body}. Will try again at the next scheduled tick.`;
}

// Best-effort: a failed alert must not mask the dispatch failure in the logs.
async function alert(env, message) {
  if (!env.DISCORD_WEBHOOK_URL) return;
  try {
    await fetch(env.DISCORD_WEBHOOK_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: message.slice(0, 1900) }),
    });
  } catch (e) {
    log("error", { event: "alert_failed", error: String(e) });
  }
}

// Structured JSON so the fields are searchable in Workers Logs rather than
// being buried in a message string. console.error/warn set log severity.
function log(level, fields) {
  console[level](JSON.stringify(fields));
}

// Comparing secrets with `!==` leaks their length and prefix through timing.
// Hashing first gives timingSafeEqual the equal-length inputs it requires.
async function secretsMatch(a, b) {
  const enc = new TextEncoder();
  const [ha, hb] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a)),
    crypto.subtle.digest("SHA-256", enc.encode(b)),
  ]);
  return crypto.subtle.timingSafeEqual(ha, hb);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const json = (status, obj) =>
  new Response(JSON.stringify(obj, null, 2) + "\n", {
    status,
    headers: { "Content-Type": "application/json" },
  });
