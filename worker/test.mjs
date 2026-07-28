import worker from "./src/index.js";

const BASE_ENV = {
  GITHUB_OWNER: "youssufhelaly",
  GITHUB_REPO: "job-watcher",
  GITHUB_WORKFLOW: "watch-jobs.yml",
  GITHUB_REF: "main",
  GITHUB_TOKEN: "fake-token",
  DISCORD_WEBHOOK_URL: "https://discord.test/webhook",
  TRIGGER_SECRET: "s3cret",
};

// crypto.subtle.timingSafeEqual is a Workers-runtime extension that Node's
// webcrypto doesn't implement. Shim it so these tests can run under plain node.
if (!crypto.subtle.timingSafeEqual) {
  crypto.subtle.timingSafeEqual = (a, b) => {
    const x = new Uint8Array(a), y = new Uint8Array(b);
    if (x.length !== y.length) return false;
    let diff = 0;
    for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
    return diff === 0;
  };
}

let calls, alerts;
const real = globalThis.fetch;

function mock(responder) {
  calls = [];
  alerts = [];
  globalThis.fetch = async (url, opts) => {
    if (String(url).startsWith("https://discord.test")) {
      alerts.push(JSON.parse(opts.body).content);
      return new Response(null, { status: 204 });
    }
    calls.push({ url: String(url), opts });
    return responder(calls.length);
  };
}

const ctx = { waitUntil: (p) => p };
const post = (path, auth) =>
  new Request(`https://w.dev${path}`, {
    method: "POST",
    headers: auth ? { authorization: auth } : {},
  });

let failures = 0;
function check(label, cond, extra = "") {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "  <-- " + extra}`);
  if (!cond) failures++;
}

// --- 1. happy path -----------------------------------------------------
mock(() => new Response(null, { status: 204 }));
let res = await worker.fetch(post("/trigger", "Bearer s3cret"), BASE_ENV);
let body = await res.json();
check("204 dispatch -> ok", res.status === 200 && body.ok === true, JSON.stringify(body));
check("one API call made", calls.length === 1, `got ${calls.length}`);
check(
  "endpoint targets workflow filename",
  calls[0].url ===
    "https://api.github.com/repos/youssufhelaly/job-watcher/actions/workflows/watch-jobs.yml/dispatches",
  calls[0].url,
);
check("sends ref=main", JSON.parse(calls[0].opts.body).ref === "main");
check("sends bearer token", calls[0].opts.headers.Authorization === "Bearer fake-token");
check("sends User-Agent", !!calls[0].opts.headers["User-Agent"]);
check("no alert on success", alerts.length === 0, JSON.stringify(alerts));

// --- 2. dead token: fail fast, alert ------------------------------------
mock(() => new Response('{"message":"Bad credentials"}', { status: 401 }));
res = await worker.fetch(post("/trigger", "Bearer s3cret"), BASE_ENV);
body = await res.json();
check("401 -> 502 to caller", res.status === 502 && body.ok === false);
check("401 does NOT retry", calls.length === 1, `got ${calls.length} attempts`);
check("401 alerts Discord", alerts.length === 1, JSON.stringify(alerts));
check(
  "401 alert names the fix",
  alerts[0]?.includes("wrangler secret put GITHUB_TOKEN"),
  alerts[0],
);

// --- 3. 404 (missing workflow_dispatch trigger) ------------------------
mock(() => new Response("Not Found", { status: 404 }));
await worker.fetch(post("/trigger", "Bearer s3cret"), BASE_ENV);
check("404 does NOT retry", calls.length === 1, `got ${calls.length}`);
check("404 alert mentions workflow_dispatch", alerts[0]?.includes("workflow_dispatch"), alerts[0]);

// --- 4. transient 500: retries, then alerts ----------------------------
mock(() => new Response("boom", { status: 500 }));
const t0 = Date.now();
await worker.fetch(post("/trigger", "Bearer s3cret"), BASE_ENV);
check("500 retries 3x", calls.length === 3, `got ${calls.length}`);
check("500 backs off ~7s", Date.now() - t0 >= 6900, `${Date.now() - t0}ms`);
check("500 alerts after exhausting", alerts.length === 1);

// --- 5. transient then success -----------------------------------------
mock((n) => n === 1 ? new Response("boom", { status: 503 }) : new Response(null, { status: 204 }));
body = await (await worker.fetch(post("/trigger", "Bearer s3cret"), BASE_ENV)).json();
check("recovers on retry", body.ok === true && body.attempt === 2, JSON.stringify(body));
check("no alert when recovered", alerts.length === 0);

// --- 6. network throw --------------------------------------------------
mock(() => {
  throw new Error("ECONNRESET");
});
body = await (await worker.fetch(post("/trigger", "Bearer s3cret"), BASE_ENV)).json();
check("network error handled, not thrown", body.ok === false);

// --- 7. auth on the manual endpoint ------------------------------------
mock(() => new Response(null, { status: 204 }));
res = await worker.fetch(post("/trigger", null), BASE_ENV);
check("no token -> 401", res.status === 401);
check("no token -> no dispatch", calls.length === 0, `got ${calls.length}`);

res = await worker.fetch(post("/trigger", "Bearer wrong"), BASE_ENV);
check("wrong token -> 401", res.status === 401);
check("wrong token -> no dispatch", calls.length === 0, `got ${calls.length}`);

res = await worker.fetch(post("/trigger", "Bearer s3cret"), { ...BASE_ENV, TRIGGER_SECRET: undefined });
check("TRIGGER_SECRET unset -> 403", res.status === 403);
check("TRIGGER_SECRET unset -> no dispatch", calls.length === 0, `got ${calls.length}`);

// --- 8. missing GITHUB_TOKEN ------------------------------------------
mock(() => new Response(null, { status: 204 }));
body = await (await worker.fetch(post("/trigger", "Bearer s3cret"), { ...BASE_ENV, GITHUB_TOKEN: undefined })).json();
check("missing PAT -> no API call", calls.length === 0, `got ${calls.length}`);
check("missing PAT alerts", alerts.length === 1, JSON.stringify(alerts));

// --- 9. cron path ------------------------------------------------------
mock(() => new Response(null, { status: 204 }));
await worker.scheduled({}, BASE_ENV, ctx);
check("cron dispatches", calls.length === 1, `got ${calls.length}`);

// --- 10. status + 404 routing -----------------------------------------
res = await worker.fetch(new Request("https://w.dev/"), BASE_ENV);
const status = await res.json();
check("GET / is 200 and dispatches nothing", res.status === 200 && calls.length === 1);
check("status reports target", status.target?.includes("watch-jobs.yml"), JSON.stringify(status));
check("status leaks no secret", !JSON.stringify(status).includes("fake-token"));
res = await worker.fetch(new Request("https://w.dev/nope"), BASE_ENV);
check("unknown path -> 404", res.status === 404);

globalThis.fetch = real;
console.log(failures ? `\n${failures} FAILURE(S)` : "\nAll checks passed.");
process.exit(failures ? 1 : 0);
