/**
 * server.js - runs the relay as an ordinary Node HTTP server.
 *
 *   node src/server.js
 *
 * Configuration is environment variables, and every one is optional:
 *
 *   PORT            listen port                                  (8080)
 *   DATA_DIR        where claims.json lives; unset = memory only (unset)
 *   MASTER_SECRET   enables named streams  (node mint.mjs <name>)
 *   PUSH_TOKEN      \ enable the single-tenant /ingest + /telemetry
 *   READ_TOKEN      /
 *   PRECLAIMED      JSON of pre-claimed streams - see `mint.mjs --preclaim`.
 *                   For hosts with no disk (Render free): the environment
 *                   survives a restart, memory does not.
 *   MAX_STREAMS     cap on paired streams                        (2000)
 *   IDLE_DAYS       forget a paired stream untouched this long   (90)
 *   UPDATE_CHECK    "off" stops the relay asking GitHub for the newest release
 *   LOG_REQUESTS    "1" logs every request, not just failures
 *
 * Pairing needs none of the secrets.
 *
 * TLS is not this process's job: the host's proxy terminates it and forwards
 * plain HTTP, so this listens on HTTP and nothing here handles a certificate.
 */

import http from "node:http";
import { pathToFileURL } from "node:url";
import worker, { configureUpdateCheck } from "./index.js";
import { StoreNamespace } from "./storage.js";

// An ingest body is one host's readings, a few hundred bytes. The firmware
// cannot parse more than a few KB in any case, so a limit well under a megabyte
// costs nothing legitimate and stops a client from buying heap with a slow
// upload.
export const MAX_BODY_BYTES = 32 * 1024;

class BodyTooLarge extends Error {}

const HEX64 = /^[0-9a-f]{64}$/;
const PAIR_STREAM = /^[0-9a-f]{16}$/;

/**
 * Parse the PRECLAIMED environment variable. Lenient per entry and strict about
 * shape: a typo in one stream must not take the relay down, but a malformed
 * entry must never be half-applied, because a claim of "undefined" would match
 * the hash of nothing and quietly protect nothing.
 * @returns {{ map: object, rejected: number }}
 */
export function parsePreclaimed(text, log = () => {}) {
  const map = {};
  let rejected = 0;
  if (!text || !String(text).trim()) return { map, rejected };
  let doc;
  try {
    doc = JSON.parse(text);
  } catch (err) {
    log(`[relay] PRECLAIMED is not valid JSON (${err.message}); ignoring all of it`);
    return { map, rejected: 1 };
  }
  for (const [stream, h] of Object.entries(doc && typeof doc === "object" ? doc : {})) {
    if (PAIR_STREAM.test(stream) && h && HEX64.test(h.push) && HEX64.test(h.read)) {
      map[stream] = { push: h.push, read: h.read };
    } else {
      rejected++;
      log(`[relay] PRECLAIMED entry ${String(stream).slice(0, 4)}… is malformed; skipped`);
    }
  }
  return { map, rejected };
}

async function readBody(req, limit) {
  const declared = Number(req.headers["content-length"]);
  if (Number.isFinite(declared) && declared > limit) throw new BodyTooLarge();
  const chunks = [];
  let n = 0;
  for await (const chunk of req) {
    n += chunk.length;
    // Chunked uploads carry no length up front, so this has to be counted.
    if (n > limit) throw new BodyTooLarge();
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function toHeaders(nodeHeaders) {
  const h = new Headers();
  for (const [k, v] of Object.entries(nodeHeaders)) {
    if (Array.isArray(v)) for (const x of v) h.append(k, x);
    else if (v !== undefined) h.set(k, v);
  }
  return h;
}

function sendJson(res, status, body, extra = {}) {
  const data = Buffer.from(JSON.stringify(body));
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
    "Content-Length": data.length,
    ...extra,
  });
  res.end(data);
}

/**
 * One line per failure, and never a secret.
 *
 * The path carries the stream id, and the stream id is what stops a stranger
 * squatting an unclaimed stream - it is only ever meant to exist on the device
 * and the host. the host's log viewer is somewhere it would otherwise end up, so it is cut
 * to four characters. The query string is dropped entirely because the
 * ?token= fallback puts a bearer token there.
 */
export function describeRequest(method, rawUrl) {
  const pathname = String(rawUrl || "").split("?")[0];
  const [route = "", stream] = pathname.split("/").filter(Boolean);
  const shown = stream ? `/${route}/${stream.slice(0, 4)}…` : `/${route}`;
  return `${method} ${shown}`;
}

/**
 * Build a relay without starting it.
 * @returns {{ server: http.Server, namespace: StoreNamespace, listen: Function, close: Function }}
 */
export function createRelay({
  secrets = {},
  dataDir = null,
  maxStreams = 2000,
  idleDays = 90,
  logAll = false,
  log = console.log,
  now = Date.now,
  preclaimed = {},
} = {}) {
  const namespace = new StoreNamespace({ dir: dataDir, maxStreams, idleDays, now, log, preclaimed });
  const env = {
    PUSH_TOKEN: secrets.PUSH_TOKEN || "",
    READ_TOKEN: secrets.READ_TOKEN || "",
    MASTER_SECRET: secrets.MASTER_SECRET || "",
    TELEMETRY: namespace,
  };

  const server = http.createServer(async (req, res) => {
    const started = Date.now();
    const done = (status) => {
      if (logAll || (status >= 400 && !String(req.url).startsWith("/health"))) {
        log(`${describeRequest(req.method, req.url)} -> ${status} ${Date.now() - started}ms`);
      }
    };

    try {
      let url;
      try {
        // A fixed origin: the Host header is the client's to set, and nothing
        // in the handler reads it.
        url = new URL(req.url, "http://relay.internal");
      } catch {
        done(400);
        return sendJson(res, 400, { error: "bad request" });
      }

      let body;
      if (req.method !== "GET" && req.method !== "HEAD") {
        try {
          body = await readBody(req, MAX_BODY_BYTES);
        } catch (err) {
          if (!(err instanceof BodyTooLarge)) throw err;
          done(413);
          // Close rather than drain: draining is the thing being refused.
          res.on("finish", () => req.destroy());
          return sendJson(res, 413, { error: "body too large" }, { Connection: "close" });
        }
      }

      let request;
      try {
        request = new Request(url, { method: req.method, headers: toHeaders(req.headers), body });
      } catch {
        done(405);
        return sendJson(res, 405, { error: "method not allowed" });
      }

      const response = await worker.fetch(request, env);
      const data = Buffer.from(await response.arrayBuffer());
      done(response.status);
      res.writeHead(response.status, Object.fromEntries(response.headers));
      res.end(data);
    } catch (err) {
      // Never put err in the response: it can name paths and, for a malformed
      // request, echo part of it back.
      log(`[server] ${describeRequest(req.method, req.url)} failed: ${err && err.stack || err}`);
      if (!res.headersSent) sendJson(res, 500, { error: "internal error" });
      else res.destroy();
    }
  });

  // The host's proxy keeps connections to this process open and reuses them.
  // Node's default keep-alive is 5 s, so the proxy can pick a connection in the
  // instant Node closes it and the client sees a 502 from a healthy relay.
  // Render's proxy reuses a connection for up to 120 s and its guidance is to
  // set both of these to 120 s; staying open longer than the proxy removes the
  // window entirely. headersTimeout must exceed keepAliveTimeout, or Node
  // closes the idle connection first anyway.
  server.keepAliveTimeout = 125_000;
  server.headersTimeout = 130_000;
  server.requestTimeout = 30_000;

  return {
    server,
    namespace,
    listen: (port = 0, host = "0.0.0.0") =>
      new Promise((resolve) => server.listen(port, host, () => resolve(server.address().port))),
    close: () =>
      new Promise((resolve) => {
        namespace.close();
        server.close(() => resolve());
        server.closeIdleConnections?.();
      }),
  };
}

async function main() {
  const e = process.env;
  const relay = createRelay({
    secrets: { PUSH_TOKEN: e.PUSH_TOKEN, READ_TOKEN: e.READ_TOKEN, MASTER_SECRET: e.MASTER_SECRET },
    dataDir: e.DATA_DIR || null,
    maxStreams: Number(e.MAX_STREAMS) || 2000,
    idleDays: Number(e.IDLE_DAYS) || 90,
    logAll: e.LOG_REQUESTS === "1",
    preclaimed: parsePreclaimed(e.PRECLAIMED, console.log).map,
  });
  configureUpdateCheck({ enabled: e.UPDATE_CHECK !== "off" });

  const port = await relay.listen(Number(e.PORT) || 8080);
  const st = relay.namespace.status();
  console.log(`[relay] listening on :${port}`);
  console.log(`[relay] claims ${st.durable
    ? `persisted in ${e.DATA_DIR}`
    : "NOT persisted (no DATA_DIR, or it is not writable) - a restart forgets every pairing claim"}`);
  const pinned = relay.namespace.size().pinned;
  if (pinned) console.log(`[relay] ${pinned} stream(s) pre-claimed from PRECLAIMED: protected from the first request after any restart`);
  if (!st.durable && !pinned && !e.DATA_DIR) {
    console.log("[relay] tip: on a host with no disk, pin your own streams with PRECLAIMED (node mint.mjs --preclaim <code>)");
  }
  // Render sets this. The URL is what goes into the firmware, and the service
  // name is not always the subdomain - a taken name gets a suffix.
  if (e.RENDER_EXTERNAL_URL) console.log(`[relay] public URL ${e.RENDER_EXTERNAL_URL}  (firmware: RELAY_BASE_URL)`);
  console.log(`[relay] modes: pairing${e.MASTER_SECRET ? ", named streams" : ""}${e.PUSH_TOKEN && e.READ_TOKEN ? ", private" : ""}`);

  // The host stops the process with a signal and gives it a few seconds
  // (Render: SIGTERM, then SIGKILL after a grace period). Closing properly
  // flushes the activity times that make idle-sweeping accurate.
  let stopping = false;
  const stop = (sig) => {
    if (stopping) return;
    stopping = true;
    console.log(`[relay] ${sig}, shutting down`);
    setTimeout(() => process.exit(0), 8000).unref();
    relay.close().then(() => process.exit(0));
  };
  process.on("SIGTERM", () => stop("SIGTERM"));
  process.on("SIGINT", () => stop("SIGINT"));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main();
}
