// Tests for everything worker.test.mjs cannot see: the HTTP adapter, the
// storage that replaced Durable Objects, and what happens across a restart.
//
// Real server, real socket, real files. No Fly account, no network, no
// `npm install`.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import http from "node:http";
import { createHash } from "node:crypto";
import { createRelay, MAX_BODY_BYTES, describeRequest, parsePreclaimed } from "../src/server.js";
import { preclaimEntry } from "../mint.mjs";
import { derivePairing, configureUpdateCheck, _seedFirmwareCache } from "../src/index.js";

configureUpdateCheck({ enabled: false });
_seedFirmwareCache("9.9.9");

let pass = 0, fail = 0;
function assert(name, cond, detail = "") {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${cond ? "" : "  " + detail}`);
  cond ? pass++ : fail++;
}

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "peek-relay-"));
const sha = (s) => createHash("sha256").update(s).digest("hex");
const SAMPLE = JSON.stringify({ host: "dietpi", cpu_percent: 12.5, ram_percent: 43.2 });

async function start(opts = {}) {
  const logs = [];
  const relay = createRelay({ log: (l) => logs.push(l), ...opts });
  const port = await relay.listen(0, "127.0.0.1");
  const base = `http://127.0.0.1:${port}`;
  const call = async (p, { method = "GET", token, body, headers = {} } = {}) => {
    const r = await fetch(base + p, {
      method,
      body,
      headers: { ...(token ? { Authorization: "Bearer " + token } : {}), ...headers },
    });
    let json = null;
    try { json = await r.json(); } catch { /* not JSON */ }
    return { status: r.status, json, headers: r.headers };
  };
  const stop = () => relay.close();
  return { relay, base, port, call, logs, stop };
}

// ===========================================================================
//  The adapter
// ===========================================================================
{
  const s = await start({ dataDir: tmp(), secrets: { MASTER_SECRET: "m".repeat(64) } });
  const d = await derivePairing("ADAPT3RTST1");

  let r = await s.call("/health");
  assert("health answers over real HTTP", r.status === 200 && r.json.ok === true);
  assert("health reports that claims are being persisted", r.json.durable === true, JSON.stringify(r.json));
  assert("health still says which modes are on", r.json.streams === true && r.json.configured === false);

  r = await s.call("/telemetry/" + d.stream, { token: d.read });
  assert("a paired device's first poll claims its stream (503, nothing pushed)", r.status === 503);

  r = await s.call("/ingest/" + d.stream, { method: "POST", token: d.push, body: SAMPLE });
  assert("the host pushes", r.status === 200);

  r = await s.call("/telemetry/" + d.stream, { token: d.read });
  assert("the device reads it back with age_s", r.status === 200 && r.json.host === "dietpi" && typeof r.json.age_s === "number");
  assert("responses are never cacheable", r.headers.get("cache-control") === "no-store");

  r = await s.call("/telemetry/" + d.stream, { token: d.read, headers: {} });
  r = await fetch(s.base + "/telemetry/" + d.stream, { method: "OPTIONS" });
  assert("CORS preflight is answered", r.status === 204 && r.headers.get("access-control-allow-origin") === "*");

  r = await s.call("/nope", { token: d.read });
  assert("unknown path -> 404", r.status === 404);
  r = await s.call("/ingest/" + d.stream, { method: "DELETE", token: d.push });
  assert("an unsupported method is refused, not crashed on", r.status === 405);
  r = await s.call("/ingest/" + d.stream, { method: "POST", token: d.push, body: "not json" });
  assert("a non-JSON body is a 400 over the wire too", r.status === 400);

  // ---- body limits ---------------------------------------------------------
  r = await s.call("/ingest/" + d.stream, { method: "POST", token: d.push, body: "x".repeat(MAX_BODY_BYTES + 1) });
  assert("a body over the limit is refused with 413", r.status === 413, JSON.stringify(r));
  r = await s.call("/ingest/" + d.stream, {
    method: "POST", token: d.push,
    body: JSON.stringify({ host: "big", pad: "y".repeat(MAX_BODY_BYTES - 200) }),
  });
  assert("a body just under the limit is accepted", r.status === 200, JSON.stringify(r));

  // A chunked upload has no Content-Length to check up front, so the limit has
  // to be enforced by counting. Raw http.request, because fetch would buffer.
  const chunkedStatus = await new Promise((resolve) => {
    const req = http.request(
      { host: "127.0.0.1", port: s.port, method: "POST", path: "/ingest/" + d.stream,
        headers: { Authorization: "Bearer " + d.push, "Transfer-Encoding": "chunked" } },
      (res) => { res.resume(); resolve(res.statusCode); });
    req.on("error", () => resolve("error"));
    for (let i = 0; i < 6; i++) req.write("z".repeat(10_000));
    req.end();
  });
  assert("a chunked body over the limit is cut off too", chunkedStatus === 413, String(chunkedStatus));

  await s.stop();
}

// ===========================================================================
//  Concurrency: the property a Durable Object gave for free
// ===========================================================================
{
  const s = await start({});
  const d = await derivePairing("RACE1NGTOK2");
  // Twenty different tokens arrive for a stream nobody has claimed, all at once.
  // Trust on first use means exactly one may win. Without per-stream
  // serialisation, claimOrVerify (a read, then a write) lets several see
  // "unclaimed" and all of them succeed.
  const tokens = Array.from({ length: 20 }, (_, i) => String(i).padStart(2, "0").repeat(24));
  const results = await Promise.all(tokens.map((t) => s.call("/telemetry/" + d.stream, { token: t })));
  const winners = results.filter((x) => x.status === 503).length;
  const losers = results.filter((x) => x.status === 401).length;
  assert("exactly one of 20 simultaneous first claims wins", winners === 1 && losers === 19,
    `winners=${winners} losers=${losers}`);
  await s.stop();
}

// ===========================================================================
//  Claims survive a restart; telemetry deliberately does not
// ===========================================================================
{
  const dir = tmp();
  const d = await derivePairing("PERS1STENT3");
  const wrongRead = "f".repeat(48);

  let s = await start({ dataDir: dir });
  await s.call("/telemetry/" + d.stream, { token: d.read });
  await s.call("/ingest/" + d.stream, { method: "POST", token: d.push, body: SAMPLE });
  let r = await s.call("/telemetry/" + d.stream, { token: d.read });
  assert("before the restart: data is there", r.status === 200);
  await s.stop();

  const file = path.join(dir, "claims.json");
  const raw = fs.readFileSync(file, "utf8");
  assert("the claims file exists", fs.existsSync(file));
  assert("it holds hashes, never the tokens themselves",
    !raw.includes(d.read) && !raw.includes(d.push) && raw.includes(sha(d.read)) && raw.includes(sha(d.push)));
  assert("the telemetry is not written to disk", !raw.includes("dietpi") && !raw.includes("cpu_percent"));
  if (process.platform !== "win32") {
    assert("the file is readable by its owner only", (fs.statSync(file).mode & 0o077) === 0);
  }
  assert("no temp file is left behind", fs.readdirSync(dir).every((f) => !f.endsWith(".tmp")));

  s = await start({ dataDir: dir });
  r = await s.call("/telemetry/" + d.stream, { token: wrongRead });
  assert("after the restart: a stranger's token is STILL refused", r.status === 401);
  r = await s.call("/telemetry/" + d.stream, { token: d.read });
  assert("...the real read token still works, and reports no stale data", r.status === 503);
  r = await s.call("/ingest/" + d.stream, { method: "POST", token: wrongRead, body: SAMPLE });
  assert("...and a stranger cannot push", r.status === 401);
  r = await s.call("/ingest/" + d.stream, { method: "POST", token: d.push, body: SAMPLE });
  assert("...the real push token still works", r.status === 200);
  await s.stop();

  // Without persistence the same sequence hands the stream to the stranger.
  // This is the failure mode the whole file exists to prevent, so it is shown
  // rather than assumed.
  const m1 = await start({});
  await m1.call("/telemetry/" + d.stream, { token: d.read });
  await m1.stop();
  const m2 = await start({});
  r = await m2.call("/telemetry/" + d.stream, { token: wrongRead });
  assert("(control) with no DATA_DIR a restart DOES reopen the claim", r.status === 503);
  r = await m2.call("/health");
  assert("(control) and /health says so", r.json.durable === false);
  await m2.stop();
}

// ===========================================================================
//  A bad data directory degrades; it does not take the relay down
// ===========================================================================
{
  const dir = tmp();
  fs.writeFileSync(path.join(dir, "claims.json"), "{ this is not json");
  const s = await start({ dataDir: dir });
  assert("a damaged claims file is set aside, not fatal",
    fs.readdirSync(dir).some((f) => f.startsWith("claims.corrupt-")));
  assert("...and the relay says so in its log", s.logs.some((l) => l.includes("damaged")));
  assert("...and carries on with durable storage", (await s.call("/health")).json.durable === true);
  await s.stop();

  // A path that is a file cannot be a directory: stands in for a read-only or
  // missing volume.
  const blocker = path.join(tmp(), "afile");
  fs.writeFileSync(blocker, "x");
  const u = await start({ dataDir: blocker });
  const d = await derivePairing("UNWR1TABLE4");
  assert("an unwritable DATA_DIR is reported by /health", (await u.call("/health")).json.durable === false);
  assert("...and logged", u.logs.some((l) => l.includes("cannot write claims")));
  const r = await u.call("/telemetry/" + d.stream, { token: d.read });
  assert("...but pairing still works in memory", r.status === 503);
  await u.stop();
}

// ===========================================================================
//  Growth is bounded
// ===========================================================================
{
  const s = await start({ maxStreams: 3, secrets: { MASTER_SECRET: "m".repeat(64) } });
  const codes = ["CAPCODE0001", "CAPCODE0002", "CAPCODE0003", "CAPCODE0004"];
  const ds = [];
  for (const c of codes) ds.push(await derivePairing(c));

  const first3 = [];
  for (const d of ds.slice(0, 3)) first3.push((await s.call("/telemetry/" + d.stream, { token: d.read })).status);
  assert("up to the cap, new paired streams are accepted", first3.every((x) => x === 503));

  let r = await s.call("/telemetry/" + ds[3].stream, { token: ds[3].read });
  assert("past the cap a new paired stream is refused, and says why",
    r.status === 503 && /not accepting/.test(r.json.error), JSON.stringify(r.json));

  r = await s.call("/ingest/" + ds[0].stream, { method: "POST", token: ds[0].push, body: SAMPLE });
  assert("an existing stream is unaffected by the cap", r.status === 200);

  // Named streams need the master secret to exist at all, so only the
  // unauthenticated kind is capped.
  const { deriveTokenNode } = await import("../mint.mjs");
  const tok = deriveTokenNode("m".repeat(64), "alice", "push");
  r = await s.call("/ingest/alice", { method: "POST", token: tok, body: SAMPLE });
  assert("named streams are not counted against it", r.status === 200);
  await s.stop();
}

{
  // An injected clock, because "idle for 90 days" is not something to wait for.
  let t = 1_800_000_000_000;
  const day = 86_400_000;
  const s = await start({ idleDays: 90, now: () => t });
  const d = await derivePairing("SWEEPABLE05");
  const intruder = "e".repeat(48);

  await s.call("/telemetry/" + d.stream, { token: d.read });
  t += 89 * day;
  assert("a stream idle for 89 days is kept", s.relay.namespace.sweep() === 0);
  let r = await s.call("/telemetry/" + d.stream, { token: intruder });
  assert("...and still refuses a stranger", r.status === 401);

  // That request counted as activity, so the clock restarts.
  t += 91 * day;
  assert("a stream idle for 91 days is dropped", s.relay.namespace.sweep() === 1);
  assert("...which frees the cap", s.relay.namespace.size().paired === 0);
  r = await s.call("/telemetry/" + d.stream, { token: intruder });
  assert("...so an abandoned stream can be claimed afresh", r.status === 503);
  await s.stop();
}

// ===========================================================================
//  Logs
// ===========================================================================
{
  const s = await start({ logAll: true });
  const d = await derivePairing("L0GHYGIENE6");
  const secretToken = "SECRETTOKENVALUE" + "0".repeat(32);
  await s.call("/telemetry/" + d.stream, { token: d.read });
  await s.call("/telemetry/" + d.stream + "?token=" + secretToken);
  await s.call("/ingest/" + d.stream, { method: "POST", token: "wrong", body: SAMPLE });

  const all = s.logs.join("\n");
  assert("requests are logged", s.logs.length >= 3);
  assert("no log line contains a full stream id", !all.includes(d.stream));
  assert("...but the first four characters are there, for finding a request",
    all.includes(d.stream.slice(0, 4)));
  assert("a ?token= fallback never reaches the log", !all.includes("SECRETTOKENVALUE"));
  assert("no log line contains a token", !all.includes(d.read) && !all.includes(d.push));
  assert("describeRequest drops the query string",
    describeRequest("GET", "/telemetry/abcdef0123456789?token=zzz") === "GET /telemetry/abcd…");
  await s.stop();

  const q = await start({});
  await q.call("/health");
  await q.call("/telemetry/" + d.stream, { token: d.read });          // a normal 503
  await q.call("/ingest/" + d.stream, { method: "POST", token: d.push, body: SAMPLE });   // 200: silent
  await q.call("/ingest/" + d.stream, { method: "POST", token: "x", body: SAMPLE });      // 401: logged
  assert("by default only failures are logged, not every poll",
    q.logs.length === 2 && q.logs.every((l) => / -> [45]\d\d /.test(l)), JSON.stringify(q.logs));
  await q.stop();
}


// ===========================================================================
//  Pre-claimed streams: protection across a restart with no disk (Render free)
// ===========================================================================
{
  const code = "PRECLA1MED7";
  const d = await derivePairing(code);
  const e = preclaimEntry(code);
  const pre = { [e.stream]: { push: e.push, read: e.read } };
  const stranger = "5".repeat(48);

  assert("mint --preclaim derives the same stream id as the relay", e.stream === d.stream);
  assert("...and it contains hashes only, never the code or a token",
    !JSON.stringify(pre).includes(code) && !JSON.stringify(pre).includes(d.read) && !JSON.stringify(pre).includes(d.push));

  // Control: with nothing pinned, the very first request claims the stream, so
  // a stranger who gets there before the device wins. This is the hole.
  const c = await start({});
  let r = await c.call("/telemetry/" + d.stream, { token: stranger });
  assert("(control) without pinning, a stranger's first request claims the stream", r.status === 503);
  await c.stop();

  let s = await start({ preclaimed: pre });
  r = await s.call("/telemetry/" + d.stream, { token: stranger });
  assert("pinned: a stranger is refused on the very FIRST request", r.status === 401);
  r = await s.call("/ingest/" + d.stream, { method: "POST", token: stranger, body: SAMPLE });
  assert("...and cannot push either", r.status === 401);
  r = await s.call("/telemetry/" + d.stream, { token: d.read });
  assert("...while the device's real read token works", r.status === 503);
  r = await s.call("/ingest/" + d.stream, { method: "POST", token: d.push, body: SAMPLE });
  assert("...and the host's real push token works, which proves mint hashes match the relay's",
    r.status === 200);
  r = await s.call("/telemetry/" + d.stream, { token: d.read });
  assert("...end to end: the data comes back", r.status === 200 && r.json.host === "dietpi");
  await s.stop();

  // A restart on a host with no disk: new process, same environment.
  s = await start({ preclaimed: pre });
  r = await s.call("/telemetry/" + d.stream, { token: stranger });
  assert("after a restart with no disk, the stranger is STILL refused first", r.status === 401);
  r = await s.call("/telemetry/" + d.stream, { token: d.read });
  assert("...and the telemetry is honestly gone until the next push", r.status === 503);
  await s.stop();

  // The idle sweep must not undo it.
  let t = 1_800_000_000_000;
  const day = 86_400_000;
  const p = await start({ preclaimed: pre, idleDays: 90, now: () => t });
  const other = await derivePairing("UNPINNED0K9");
  await p.call("/telemetry/" + other.stream, { token: other.read });
  t += 120 * day;
  assert("an idle unpinned stream is swept, a pinned one is not", p.relay.namespace.sweep() === 1);
  r = await p.call("/telemetry/" + d.stream, { token: stranger });
  assert("...so the pinned stream still refuses a stranger after 120 idle days", r.status === 401);
  assert("status reports how many are pinned", p.relay.namespace.size().pinned === 1);
  await p.stop();

  // The environment wins over a file that disagrees: the operator changed it on purpose.
  const dir = tmp();
  const f = await start({ dataDir: dir });
  const stale = "7".repeat(48);
  await f.call("/telemetry/" + d.stream, { token: stale });      // claimed by a stale token
  await f.stop();
  const g = await start({ dataDir: dir, preclaimed: pre });
  r = await g.call("/telemetry/" + d.stream, { token: stale });
  assert("PRECLAIMED overrides a claim already on disk", r.status === 401);
  r = await g.call("/telemetry/" + d.stream, { token: d.read });
  assert("...and the pinned token now works", r.status === 503);
  await g.stop();
}

{
  const logs = [];
  const log = (l) => logs.push(l);
  const good = JSON.stringify({ "4b907ba136d0a7f2": { push: "a".repeat(64), read: "b".repeat(64) } });
  assert("a well-formed PRECLAIMED parses", Object.keys(parsePreclaimed(good, log).map).length === 1);
  assert("empty / unset is fine and silent", Object.keys(parsePreclaimed("", log).map).length === 0 && !logs.length);
  assert("invalid JSON is ignored whole, with a log line",
    Object.keys(parsePreclaimed("{oops", log).map).length === 0 && logs.some((l) => /not valid JSON/.test(l)));
  const mixed = JSON.stringify({
    "4b907ba136d0a7f2": { push: "a".repeat(64), read: "b".repeat(64) },
    "nothex": { push: "a".repeat(64), read: "b".repeat(64) },
    "0123456789abcdef": { push: "short", read: "b".repeat(64) },
    "fedcba9876543210": { push: "a".repeat(64) },
  });
  const m = parsePreclaimed(mixed, log);
  assert("one bad entry does not discard the good ones, and none is half-applied",
    Object.keys(m.map).length === 1 && m.rejected === 3 && !("0123456789abcdef" in m.map));
}

// ===========================================================================
//  Keep-alive: the 502 window on Render's proxy
// ===========================================================================
{
  const s = await start({});
  const { keepAliveTimeout: ka, headersTimeout: ht } = s.relay.server;
  assert("keep-alive outlasts Render's 120 s proxy reuse window", ka >= 120_000, String(ka));
  assert("headersTimeout exceeds keepAliveTimeout (or Node closes idle connections early)", ht > ka);
  await s.stop();
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
