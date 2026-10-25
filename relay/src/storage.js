/**
 * storage.js - what a Durable Object used to be, for a single Node process.
 *
 * A Durable Object gave the Worker three things, and each has to come from
 * somewhere now:
 *
 *   1. A storage object with get / put / delete / list.        -> MemoryStorage
 *   2. One request at a time per stream (the "input gate").    -> StoreNamespace._run
 *   3. State that survives a restart.                          -> claims.json
 *
 * (3) is deliberately NOT "everything". The only state that is a security
 * property is the trust-on-first-use claim - the hash of the first push and
 * read token each paired stream ever presented. Lose that and the next restart
 * hands every stream back to whoever asks first. Telemetry is the opposite: it
 * is replaced every few seconds, and a restart that shows "no telemetry yet"
 * for one push interval is honest, where a stale snapshot dressed up as live is
 * the failure a monitor must never have. So claims are written through to disk
 * and telemetry is not.
 *
 * This requires exactly ONE running instance. State lives in this process; a
 * second instance behind the same load balancer would receive the push on one
 * and the poll on the other, and every request would look valid from both ends.
 */

import fs from "node:fs";
import path from "node:path";
import { TelemetryStore } from "./index.js";

const CLAIMS_FILE = "claims.json";
const DURABLE_PREFIX = "auth_";          // the only keys written to disk
const PAIR_PREFIX = "pair:";             // the only namespace anyone can create unauthenticated

const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * The slice of DurableObjectStorage that TelemetryStore uses.
 *
 * Shaped like the real thing rather than like the calls made today: list()
 * returns a Map in sorted key order and delete() takes one key or an array. A
 * stub that implements only what is currently called turns the next feature
 * into a crash instead of a failing assertion.
 */
export class MemoryStorage {
  constructor(onDurableChange = () => {}) {
    this.map = new Map();
    this.onDurableChange = onDurableChange;
  }

  async put(a, b) {
    let durable = false;
    const set = (k, v) => {
      this.map.set(k, v);
      if (k.startsWith(DURABLE_PREFIX)) durable = true;
    };
    if (typeof a === "object") for (const [k, v] of Object.entries(a)) set(k, v);
    else set(a, b);
    // Synchronous on purpose. The claim is on disk before the response that
    // depends on it leaves, so "claimed" never means "claimed, unless we crash
    // in the next millisecond".
    if (durable) this.onDurableChange();
  }

  async get(k) {
    return this.map.get(k);
  }

  async delete(k) {
    const keys = Array.isArray(k) ? k : [k];
    let n = 0;
    let durable = false;
    for (const key of keys) {
      if (this.map.delete(key)) {
        n++;
        if (key.startsWith(DURABLE_PREFIX)) durable = true;
      }
    }
    if (durable) this.onDurableChange();
    return Array.isArray(k) ? n : n > 0;
  }

  async list({ prefix = "" } = {}) {
    return new Map(
      [...this.map]
        .filter(([key]) => key.startsWith(prefix))
        .sort((x, y) => (x[0] < y[0] ? -1 : x[0] > y[0] ? 1 : 0)),
    );
  }
}

const noop = () => {};

const relayFull = () =>
  new Response(
    JSON.stringify({
      error: "this relay is not accepting new paired streams",
      hint: "the operator has capped how many it will hold; existing streams are unaffected",
    }),
    { status: 503, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } },
  );

/**
 * Stands in for env.TELEMETRY, the Durable Object namespace binding.
 *
 *   idFromName(name) -> id          the name itself; there is nothing to look up
 *   get(id)          -> stub        { fetch(url, init) -> Promise<Response> }
 *
 * Plus status(), flush(), sweep() and close(), which only the server uses.
 */
export class StoreNamespace {
  /**
   * @param {object}  o
   * @param {string=} o.dir         where claims.json lives. Unset = memory only.
   * @param {number=} o.maxStreams  cap on paired streams (the unauthenticated kind)
   * @param {number=} o.idleDays    drop a paired stream nobody has touched for this long
   * @param {Function=} o.now       clock, injectable for tests
   * @param {Function=} o.log
   * @param {object=}   o.preclaimed  { "<16 hex stream>": { push: "<sha256>", read: "<sha256>" } }
   *        Claims supplied from outside, applied on every boot. For a host with
   *        no disk: environment variables outlive a restart when memory does not.
   */
  constructor({ dir = null, maxStreams = 2000, idleDays = 90, now = Date.now, log = console.log, preclaimed = {} } = {}) {
    this.dir = dir;
    this.maxStreams = maxStreams;
    this.idleMs = idleDays * DAY_MS;
    this.now = now;
    this.log = log;

    this.entries = new Map();     // name -> { store, storage, queue, seen }
    this.pairCount = 0;
    this.writeOk = null;          // null = not attempted, true/false = last attempt
    this.seenDirty = false;
    this.timer = null;
    this.lastWriteWarn = 0;
    this.pinnedCount = 0;

    if (this.dir) this.load();
    this.applyPreclaimed(preclaimed);

    if (this.dir) {
      // Prove the directory is writable NOW rather than on the first claim, so
      // a missing volume shows up in /health and the boot log, not as a silent
      // loss of every claim at the next deploy.
      this.persist();
      this.timer = setInterval(() => this.maintenance(), 10 * 60 * 1000);
      this.timer.unref();
    }
  }

  // ---- the binding ---------------------------------------------------------
  idFromName(name) {
    return name;
  }

  get(name) {
    let e = this.entries.get(name);
    if (!e) {
      // Anyone who can reach this relay can create a paired stream just by
      // presenting a bearer token, and on one small VM that is a way to fill
      // the disk and the heap. Named and private streams need the master
      // secret to exist, so only this namespace is capped.
      if (name.startsWith(PAIR_PREFIX) && this.pairCount >= this.maxStreams) {
        return { fetch: async () => relayFull() };
      }
      e = this.create(name, {}, this.now());
    }
    return { fetch: (url, init) => this._run(e, url, init) };
  }

  // One request at a time per store, in arrival order. A Durable Object does
  // this for free and TelemetryStore relies on it: claimOrVerify is a read
  // followed by a write, and two first requests with different tokens would
  // otherwise both see "unclaimed" and both win.
  _run(e, url, init) {
    e.seen = this.now();
    this.seenDirty = true;
    const job = () => e.store.fetch(new Request(url, init));
    const p = e.queue.then(job, job);
    e.queue = p.then(noop, noop);   // a failed request must not wedge the queue
    return p;
  }

  /**
   * Pin streams whose claim is known in advance.
   *
   * The environment wins over the file: if the two disagree the operator has
   * just changed it on purpose. A pinned stream is also exempt from the idle
   * sweep - forgetting it after 90 quiet days would silently reopen the exact
   * hole this exists to close.
   */
  applyPreclaimed(map) {
    let n = 0;
    for (const [stream, h] of Object.entries(map || {})) {
      const name = PAIR_PREFIX + stream;
      const durable = { auth_push: h.push, auth_read: h.read };
      const existing = this.entries.get(name);
      if (existing) {
        for (const [k, v] of Object.entries(durable)) existing.storage.map.set(k, v);
        existing.pinned = true;
      } else {
        this.create(name, durable, this.now()).pinned = true;
      }
      n++;
    }
    this.pinnedCount = n;
  }

  create(name, durable, seen) {
    const storage = new MemoryStorage(() => this.persist());
    for (const [k, v] of Object.entries(durable)) storage.map.set(k, v);
    const e = { store: new TelemetryStore({ storage }), storage, queue: Promise.resolve(), seen };
    this.entries.set(name, e);
    if (name.startsWith(PAIR_PREFIX)) this.pairCount++;
    return e;
  }

  // ---- persistence ---------------------------------------------------------
  load() {
    const file = path.join(this.dir, CLAIMS_FILE);
    let raw;
    try {
      raw = fs.readFileSync(file, "utf8");
    } catch (err) {
      if (err.code === "ENOENT") return;          // first boot
      this.log(`[storage] cannot read ${file}: ${err.message}`);
      return;
    }
    try {
      const doc = JSON.parse(raw);
      for (const [name, rec] of Object.entries(doc.streams || {})) {
        const durable = {};
        for (const k of Object.keys(rec)) if (k.startsWith(DURABLE_PREFIX)) durable[k] = rec[k];
        this.create(name, durable, Number(rec.seen) || this.now());
      }
      this.log(`[storage] restored ${this.entries.size} claimed stream(s)`);
    } catch (err) {
      // Starting empty reopens trust-on-first-use for every stream, which is
      // bad - but a relay that crash-loops on a damaged file is worse, and the
      // file is kept so nothing is lost for good.
      const aside = path.join(this.dir, `claims.corrupt-${this.now()}.json`);
      try { fs.renameSync(file, aside); } catch { /* best effort */ }
      this.entries.clear();
      this.pairCount = 0;
      this.log(`[storage] ${file} is damaged (${err.message}); moved to ${aside}, starting empty`);
    }
  }

  /** Atomic: write a sibling, fsync it, rename over the original. */
  persist() {
    if (!this.dir) return;
    const streams = {};
    for (const [name, e] of this.entries) {
      const rec = {};
      for (const [k, v] of e.storage.map) if (k.startsWith(DURABLE_PREFIX)) rec[k] = v;
      if (Object.keys(rec).length) streams[name] = { ...rec, seen: e.seen };
    }
    const file = path.join(this.dir, CLAIMS_FILE);
    const tmp = `${file}.${process.pid}.tmp`;
    try {
      fs.mkdirSync(this.dir, { recursive: true });
      const fd = fs.openSync(tmp, "w", 0o600);
      try {
        fs.writeSync(fd, JSON.stringify({ v: 1, streams }));
        fs.fsyncSync(fd);
      } finally {
        fs.closeSync(fd);
      }
      fs.renameSync(tmp, file);
      this.writeOk = true;
      this.seenDirty = false;
    } catch (err) {
      this.writeOk = false;
      // Keep serving. A relay that refuses telemetry because a disk is full is
      // a worse monitor than one that has lost durability and says so.
      if (this.now() - this.lastWriteWarn > 60_000) {
        this.lastWriteWarn = this.now();
        this.log(`[storage] cannot write claims to ${this.dir}: ${err.message}`);
      }
      try { fs.unlinkSync(tmp); } catch { /* may not exist */ }
    }
  }

  /** Periodic: forget abandoned streams, and record who has been active. */
  maintenance() {
    const swept = this.sweep();
    if (!swept && this.seenDirty) this.persist();
  }

  /**
   * Re-pairing is just a new code, which is a new stream - so every re-pair
   * leaves the old one behind. Without this the cap fills with streams nobody
   * will ever use again and eventually locks out real ones.
   * @returns {number} how many were dropped
   */
  sweep() {
    const cutoff = this.now() - this.idleMs;
    let n = 0;
    for (const [name, e] of this.entries) {
      if (!name.startsWith(PAIR_PREFIX) || e.pinned || e.seen > cutoff) continue;
      this.entries.delete(name);
      this.pairCount--;
      n++;
    }
    if (n) {
      this.log(`[storage] dropped ${n} paired stream(s) idle for over ${Math.round(this.idleMs / DAY_MS)} days`);
      this.persist();
    }
    return n;
  }

  // ---- for the server ------------------------------------------------------
  status() {
    return { durable: Boolean(this.dir) && this.writeOk !== false };
  }

  size() {
    return { streams: this.entries.size, paired: this.pairCount, pinned: this.pinnedCount };
  }

  /** Write now. Called on shutdown so recent activity times are not lost. */
  flush() {
    this.persist();
  }

  close() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.flush();
  }
}
