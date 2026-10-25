#!/usr/bin/env node
/**
 * Mint a stream's token pair.
 *
 *   MASTER_SECRET=... npm run mint -- alice
 *   MASTER_SECRET=... npm run mint -- alice --url https://peek-relay.onrender.com
 *   node mint.mjs --preclaim K7M2-P4QX-9R
 *
 * Runs entirely offline. Tokens are a pure function of (master secret,
 * stream, role), so the relay recomputes the same values and there is
 * nothing to upload, register or keep in sync. Onboarding someone never
 * touches the deployment.
 *
 * This derivation must stay byte-identical to deriveToken() in src/index.js.
 * test/worker.test.mjs asserts that the two agree. Run it on your own machine,
 * never on the host: it is the one tool that handles MASTER_SECRET.
 */

import { createHash, createHmac, randomBytes } from "node:crypto";
import { pathToFileURL } from "node:url";

const TOKEN_HEX_LEN = 48;
const STREAM_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;

export function deriveTokenNode(masterSecret, stream, role) {
  return createHmac("sha256", masterSecret)
    .update(`${stream}:${role}`)
    .digest("hex")
    .slice(0, TOKEN_HEX_LEN);
}

/**
 * What the relay needs to remember about a paired stream so that it is
 * protected from the first request after a restart, with no disk.
 *
 * Mirrors derivePairing() in src/index.js and the claim the relay would record
 * by itself on first use: the stream id, and the SHA-256 of each derived token
 * (hashed as the 48-character hex STRING, exactly as the relay hashes what it
 * is presented). test/server.test.mjs proves the two agree by letting a real
 * device's tokens through and refusing a stranger's.
 *
 * The code itself is never part of the output.
 */
export function preclaimEntry(code) {
  const c = String(code || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  const sha = (t) => createHash("sha256").update(t).digest("hex");
  return {
    stream: sha("peek-stream:" + c).slice(0, 16),
    push: sha(sha("peek-push:" + c).slice(0, TOKEN_HEX_LEN)),
    read: sha(sha("peek-read:" + c).slice(0, TOKEN_HEX_LEN)),
  };
}

function die(msg) {
  console.error("error: " + msg);
  process.exit(1);
}

// The test suite imports deriveTokenNode from this file to prove it agrees
// with the relay's WebCrypto derivation, so the CLI must not run on import.
const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (!isMain) {
  // exported for tests; nothing else to do
} else {
  main();
}

function main() {
const argv = process.argv.slice(2);

if (argv.includes("--new-master")) {
  // 32 bytes: this is an HMAC key, not something anyone types.
  console.log(randomBytes(32).toString("hex"));
  console.log("\nStore it as an environment variable named MASTER_SECRET on the relay");
  console.log("(Render: service -> Environment). Saving it restarts the service once;");
  console.log("devices simply reconnect.");
  console.log("Keep a copy somewhere safe - you need it to mint further streams,");
  console.log("and rotating it invalidates every stream at once.");
  process.exit(0);
}

if (argv.includes("--preclaim")) {
  // Everything after the flag that is not itself a flag is a pairing code.
  const codes = argv.slice(argv.indexOf("--preclaim") + 1).filter((a) => !a.startsWith("--"));
  if (!codes.length) {
    die("give at least one pairing code:  node mint.mjs --preclaim K7M2-P4QX-9R");
  }
  const map = {};
  for (const code of codes) {
    const clean = code.toUpperCase().replace(/[^A-Z0-9]/g, "");
    if (clean.length !== 10) die(`"${code}" is not a pairing code (10 characters, shown on the device)`);
    const { stream, push, read } = preclaimEntry(code);
    map[stream] = { push, read };
  }
  console.log(`
Add this as an environment variable named PRECLAIMED on the relay:

${JSON.stringify(map)}

What it does: the relay forgets which token claimed each paired stream whenever
it restarts (a free Render instance has no disk), and for a few seconds
afterwards anyone who already knew your stream id could claim it first. With
this set, your stream is claimed from the very first request after every
restart.

What it contains: the stream id and a SHA-256 of each token - not the pairing
code, and not a working token. Keep in mind the code only has about 50 bits of
entropy, so a hash is something to keep out of public places, not a secret you
can publish. Anyone who can read the service's environment can read it.

Re-pair the device (new code) and the old entry stops mattering: run this again
with the new code and replace the value.
`);
  process.exit(0);
}

const stream = argv.find((a) => !a.startsWith("--"));
const urlIdx = argv.indexOf("--url");
const base = urlIdx !== -1 ? (argv[urlIdx + 1] || "").replace(/\/+$/, "") : null;

if (!stream) {
  console.error(`Usage:
  MASTER_SECRET=... node mint.mjs <stream> [--url https://peek-relay.onrender.com]
  node mint.mjs --new-master
  node mint.mjs --preclaim <pairing code> [more codes]   (protect paired streams across restarts)

  <stream>  lowercase letters, digits, - and _, starting alphanumeric, max 32 chars`);
  process.exit(1);
}

if (!STREAM_RE.test(stream)) {
  die(`invalid stream name "${stream}" - lowercase letters, digits, - and _, ` +
      "starting alphanumeric, max 32 chars");
}

const master = process.env.MASTER_SECRET;
if (!master) {
  die("MASTER_SECRET is not set.\n" +
      "  Generate one:  node mint.mjs --new-master\n" +
      "  Then:          MASTER_SECRET=... node mint.mjs " + stream);
}

const push = deriveTokenNode(master, stream, "push");
const read = deriveTokenNode(master, stream, "read");
const shown = base || "https://YOUR-SERVICE.onrender.com";

console.log(`
stream: ${stream}

  push token (host agent - keep private)
    ${push}

  read token (the display device)
    ${read}

Give the owner of this stream:

  On the host:
    python3 peek-agent.py --push ${shown}/ingest/${stream} --token ${push}

  In the device's setup portal (Relay section):
    Worker URL   ${shown}/telemetry/${stream}
    Read token   ${read}

  Check it:
    curl -H "Authorization: Bearer ${read}" ${shown}/telemetry/${stream}
    (503 "no telemetry received yet" means auth works and nothing has pushed yet)
${base ? "" : `
Pass --url https://your-service.onrender.com to print real URLs.`}
To revoke this stream later, move the owner to a new name (${stream}-v2):
tokens are derived, so they change with the name and no one else is affected.
`);
}
