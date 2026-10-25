# PeekESP relay

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/rhshourav/PeekESP)

A small HTTPS service, built for **Render's free tier**, that lets the dashboard
work when **nothing can accept an inbound connection** — the host behind CGNAT,
on a network you don't administer, or anywhere a port forward isn't an option.

```
host   ──POST /ingest/<stream>    (Bearer push token)──▶  relay  (one Render service)
ESP32  ──GET  /telemetry/<stream> (Bearer read token)──▶  relay
```

Both ends only ever dial **out** over HTTPS. One deployment serves any number of
independent `<stream>`s. There are **no dependencies** — no `npm install` step to
fail, nothing to compile.

## Deploy from GitHub

**1. Put it in your repo.** `render.yaml` goes at the **repository root** (Render
requires that); everything else goes in `relay/`.

```bash
git mv cloudflare relay                 # if your old folder was called cloudflare/
# unzip over it:  render.yaml and .github/ at the root, the rest inside relay/
git rm relay/wrangler.toml .github/workflows/cloudflare-worker.yml   # the old deploy
git add -A && git commit -m "Move relay to Render" && git push
```

The old Cloudflare workflow has to go: left in place it would keep trying to
deploy to Cloudflare on every relay change and fail.

**2. Connect it.** Either click the button at the top of this file (it points at
`github.com/rhshourav/PeekESP` — edit the link if your repo is elsewhere), or in
the Render dashboard: **New → Blueprint → paste your repo's GitHub URL**. Render
reads `render.yaml`, shows you one free web service, and you click *Apply*. The
button needs `render.yaml` already pushed to the default branch.

**3. Find your URL.** The service is named `peek-relay`, but names are global —
if it's taken Render adds a suffix, so *read the real URL from the dashboard*. The
boot log also prints it:

```
[relay] public URL https://peek-relay-x1y2.onrender.com  (firmware: RELAY_BASE_URL)
```

```bash
curl https://<your-url>/health
# {"ok":true,"configured":false,"streams":false,"durable":false,...}
```

`"durable":false` is **expected** on Render's free tier — see below. Everything
works; it is telling you the claims live in memory.

**4. Point the firmware at it** — see *Migrating from the Cloudflare Worker*, which
matters more than it looks.

**5. Pin your stream (recommended; 1 minute).** Once your device shows its pairing
code, on **your own machine**:

```bash
node mint.mjs --preclaim K7M2-P4QX-9R
```

Paste the JSON line it prints into the Render dashboard: **service → Environment →
Add → `PRECLAIMED`**. Saving restarts the service once. Why this is worth doing is
the next section.

You can check the blueprint at any time with the Render CLI:
`render blueprints validate`.

## What the free tier means for this design

Read this once; it is the whole trade.

**It will not sleep while you use it.** Render spins a free service down after 15
minutes with no inbound traffic. A powered display polls every few seconds, so it
never gets there. If *everything* is off for 15 minutes the next request wakes it,
which takes on the order of a minute; the device shows `NO LINK` and recovers by
itself.

**It fits the monthly hours — if it's your only free service.** One always-on
service is about 744 hours against 750 free instance hours. A second free service
in the same workspace and Render suspends them all until the month turns.

**It restarts, and a restart forgets claims.** A free service has no disk, Render
may restart it at any time, and every deploy restarts it. Paired streams use
*trust on first use* — the first token to claim a stream owns it — and that record
lives in memory. After a restart your stream is unclaimed for the few seconds until
your device and PC next poll, and in that window someone who **already knew your
16-hex stream id** could claim it first.

**`PRECLAIMED` closes that window.** Environment variables survive a restart when
memory does not, so the relay re-applies your claim from the environment at every
boot. Tested: a stranger is refused on the very first request, including right
after a restart, and the idle sweep never forgets a pinned stream. What the
variable contains is the stream id and a SHA-256 of each token — not the pairing
code and not a working token. Treat it as something to keep private rather than
publish: the code carries only about 50 bits, so a hash is not a secret you can
hand out. Anyone who can read your service's environment can read it.

You only need `PRECLAIMED` for streams you want protected; anything else behaves as
before. Re-pairing gives a new code, so run `--preclaim` again and replace the value.

**Deploys are gated.** `render.yaml` sets `autoDeployTrigger: checksPass`, so
Render waits for the GitHub Actions run to go green and a broken commit never
replaces a working relay. Because it also sets `rootDir`, commits outside `relay/`
(your firmware) never redeploy it, and a `buildFilter` ignores tests, docs and the
mint tool inside `relay/`. Each of those matters because **every deploy restarts
the process**. If you turn Actions off, change the trigger to `commit` or Render
will wait forever.

**Keep-alive is set for Render's proxy.** It reuses connections for up to 120 s and
Node's default of 5 s makes it occasionally pick one Node has just closed — a 502
from a healthy service. The server holds connections open for 125 s, and a test
guards the value.

**What I couldn't confirm:** Render has suspended free services for traffic it
judged unusual and the limits aren't fully documented. This relay's traffic is
small, but I can't promise it won't be flagged. And **which CA signs
`*.onrender.com`** — `ca_certs.h` pins Google's GTS Root **R4** and ISRG Root X1
only, so check before flashing (the `openssl` command is in the migration section).

## Three modes

### Paired (the default, and no secrets at all)

The device shows a one-time code. You type it into the PeekESP app. Both sides
derive the same stream and token pair from it locally:

```
stream = SHA-256("peek-stream:" + CODE)  first 16 hex
push   = SHA-256("peek-push:"   + CODE)  first 48 hex
read   = SHA-256("peek-read:"   + CODE)  first 48 hex
```

The code never reaches this relay. Authentication is **trust on first use**: the
first push token and the first read token a stream ever sees are recorded (as
hashes), and everything afterwards must match. The guarantee is precisely *"once a
role is claimed, only that token works"* — not *"only the right token was ever
possible"*. That is the squatting window described above; `PRECLAIMED` removes it
for the streams you list. Paired streams are always 16 hex characters, which keeps
them in a separate namespace from named streams like `alice`.

### Shared (named streams) — stateless, so restarts cost nothing

```bash
MASTER_SECRET=... npm run mint -- alice --url https://<your-url>
```

Runs entirely **offline** — the deployment is never touched. Tokens are derived,
not stored:

```
push token = HMAC-SHA256(MASTER_SECRET, "<stream>:push")  truncated to 48 hex
read token = HMAC-SHA256(MASTER_SECRET, "<stream>:read")  truncated to 48 hex
```

Because nothing is remembered, a restart cannot reopen anything. This is the mode
to choose if you would rather not rely on `PRECLAIMED`; the cost is typing a URL and
token into the device portal instead of pairing from its screen. Set `MASTER_SECRET`
in the dashboard's Environment tab. **It is the crown jewel**: anyone holding it can
mint tokens for any stream. Never put it in GitHub, and run `mint.mjs` on your own
machine, never on the host. To revoke one tenant, move them to a new stream name;
rotating `MASTER_SECRET` invalidates everything.

### Private (just you)

`/ingest` and `/telemetry` with no stream work against `PUSH_TOKEN` and `READ_TOKEN`
environment variables. All three modes can be active at once.

## Endpoints

| Method | Path | Token | Purpose |
|---|---|---|---|
| `POST` | `/ingest/<16 hex>` | paired push | A paired PC publishes (claimed on first use) |
| `GET` | `/telemetry/<16 hex>` | paired read | A paired device reads |
| `POST` | `/command/<16 hex>` | paired push | Queue a command for the device |
| `POST` | `/ingest/<name>` | derived push | A named stream's host publishes |
| `GET` | `/telemetry/<name>` | derived read | A named stream's device reads |
| `POST` | `/ingest` | `PUSH_TOKEN` | Single-tenant publish |
| `GET` | `/telemetry` | `READ_TOKEN` | Single-tenant read |
| `GET` | `/health` | none | `configured`, `streams`, `durable` |

`/telemetry` returns the stored JSON plus **`age_s`**, seconds since the last push;
the firmware shows `STALE` above 30 s. Request bodies are limited to 32 KB.

## What survives a restart

| | Survives? |
|---|---|
| Claims on streams listed in `PRECLAIMED` | **Yes** — re-applied from the environment at every boot |
| Named-stream access | **Yes** — nothing is stored; it is recomputed |
| Claims on any other paired stream | No — reopened until the device and PC reconnect |
| The latest telemetry | No — the device sees `503 no telemetry yet` for up to one push interval, which is the honest answer rather than a stale snapshot dressed up as live |
| A queued command | No — commands expire after 5 minutes anyway |

(If you later move to a host with a disk, set `DATA_DIR` and claims for *every*
stream persist to `claims.json`; `/health` then reports `"durable":true`.)

## Limits

Defaults, all settable as environment variables:

| Variable | Default | What it bounds |
|---|---|---|
| `MAX_STREAMS` | 2000 | Paired streams. Anyone can create one just by presenting a bearer token, so this has to be capped. Past it *new* pairings get `503 not accepting new paired streams`; existing ones are unaffected. Measured: ~55 MB idle plus ~27 MB per 1,000 streams in the worst case (six machines each); the free instance has 512 MB, so there is room to raise it, though I only measured up to 1,000. |
| `IDLE_DAYS` | 90 | An unpinned paired stream untouched this long is forgotten. Every re-pair abandons one. |
| `UPDATE_CHECK` | on | `off` stops the relay asking GitHub for the newest release (the device's "update available" banner). |
| `LOG_REQUESTS` | off | `1` logs every request, not only failures. |

There is **no per-client rate limiting**. A determined abuser could fill the cap and
lock out *new* pairings (not existing ones). Fine for a relay shared with a few
people; for a public one, put it behind something that rate limits.

## Logs

Render's **Logs** tab shows failures only, not every poll. Paths are logged as
`/telemetry/4b90…` — **four characters of the stream id, never the whole thing**,
because the stream id is what resists squatting. Query strings, including the
discouraged `?token=` fallback, are never logged.

## Migrating from the Cloudflare Worker

The protocol is unchanged: same endpoints, same derivations, same JSON. What moves
is the URL, and in three places that is less obvious than it sounds.

**1. Devices already in the field will not follow a new `RELAY_BASE_URL`.** The
firmware writes `relay_base` into NVS on first boot, and NVS survives reflashing.
Changing the constant and reflashing a paired device does nothing; it keeps polling
`workers.dev`. There is no portal field for it. The fix is two lines in
`PeekESP.ino`, since nothing can edit that value anyway:

```diff
 // config_load()
-  prefs.getString("rbase",  cfg.relay_base,  sizeof cfg.relay_base);
 // config_save()
-  prefs.putString("rbase",  cfg.relay_base);
+  prefs.remove("rbase");
```

then set the base once at build time — `#define RELAY_BASE_URL "https://<your-url>"`
in `secrets.h`, or `-D RELAY_BASE_URL=\"...\"` in `platformio.ini`. (The alternative,
*erase all settings* on each device, also wipes its WiFi.)

**2. Check the certificate chain before you flash anything.**

```bash
openssl s_client -connect <your-host>:443 -showcerts </dev/null 2>/dev/null \
  | grep -E "^ *[0-9]+ s:|^ +i:"
```

If the chain ends at **GTS Root R4** or **ISRG Root X1** (or something they signed),
nothing to do. If it ends at another root — GTS Root R1, say — add that root to
`ca_certs.h`, or use the portal's *Verify TLS certificate* switch to get a device
running meanwhile. The comment at the top of `ca_certs.h` still describes
Cloudflare; update it.

**3. Every other place that holds the URL.** `RELAY_BASE` in `dietpi/peek-agent.py`
and the default base in `windows/peek_pair.py`. `live_pair.py` checks that the Linux
agent's default matches the relay you point it at.

Paired streams need no migration: the same code derives the same stream and tokens,
and the new relay sees them as unclaimed until the device and PC reconnect. You can
run both relays side by side while devices are reflashed.

## Test

```bash
npm test
```

**86 checks** in `test/worker.test.mjs` — routing, auth, isolation, multi-machine
slots, TTL, legacy upgrade, commands, and the cross-implementation vectors. This is
the original suite, unchanged but for one line that keeps it off the network.

**70 checks** in `test/server.test.mjs` — a real HTTP server on a real socket: body
limits (including chunked), twenty simultaneous first-claims producing exactly one
winner, claims surviving a restart, a damaged or unwritable data directory, the cap
and the idle sweep, that no log line contains a token or a full stream id, that a
pinned stream refuses a stranger on its first request and after a restart, and that
the keep-alive outlasts Render's proxy. Where a claim is meant to leave a hole
open, there is a control test showing the hole.

Tested on Node 22, which `render.yaml` selects via `NODE_VERSION`. **Not exercised
here: an actual deploy to Render.** I validated `render.yaml` against Render's
documented field names, not by applying it.

Against a live deployment:

```bash
python live_pair.py https://<your-url>
```

plays both the PC and the device from a fresh code. On a host that should have a
disk, `PEEK_REQUIRE_DURABLE=1` turns the persistence note into a failure.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Pushed to GitHub, nothing deployed | `checksPass` is waiting for Actions. Check the *Actions* tab is enabled and the run is green — or set `autoDeployTrigger: commit`. |
| Firmware commit didn't redeploy the relay | Working as designed: `rootDir` ignores commits outside `relay/`. |
| Device `NO LINK` for ~a minute, then fine | The service had slept or just restarted. |
| `401` on a stream that used to work | After re-pairing, `PRECLAIMED` still holds the old code's claim. Run `--preclaim` with the new code and replace the value. |
| The URL isn't `peek-relay.onrender.com` | The name was taken. Use the URL from the dashboard. |
| `/health` says `"durable":false` | Expected on Render free. |
