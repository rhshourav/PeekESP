/*
 * peekesp-card - the PeekESP device screen, on a Home Assistant dashboard.
 *
 * This is a port of the firmware's own dashboard (PeekESP.ino +
 * layout_metrics.h), not a redesign. Coordinates are the 240x135 reference
 * layout the T-Display was tuned on; colours are the firmware palette; the
 * formatters (fmt_rate, fmt_capacity, fmt_uptime, fmt_ago), the load colours
 * (amber at 75 %, red at 90 %) and the 500 ms ease-out sweep are the same
 * rules, so the card and the glass agree on what a reading looks like.
 *
 *   type: custom:peekesp-card
 *   entity: sensor.dietpi_screen
 *   name: optional label shown after "//"
 */
const VERSION = "0.1.0";

const C = {
  bg: "#05070E", panel: "#0B1220", track: "#1A2334",
  cyan: "#00E5FF", magenta: "#FF2E7E", amber: "#FFC145",
  green: "#35F2A0", red: "#FF4D6D", text: "#E6EDF7", dim: "#5C6B82",
};
const FONT = `Montserrat, "Segoe UI", Roboto, system-ui, -apple-system, sans-serif`;
const ANIM_MS = 500;

// ---- formatters: the firmware's, line for line ------------------------------
const fmtRate = (kbps) =>
  kbps >= 1000 ? `${(kbps / 1024).toFixed(1)}M` : `${Math.round(kbps)}K`;

const fmtCapacity = (gb) =>
  gb >= 1024 ? `${(gb / 1024).toFixed(1)}T` : gb >= 10 ? `${Math.round(gb)}G` : `${gb.toFixed(1)}G`;

function fmtUptime(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `up ${d}d ${h}h` : h ? `up ${h}h ${m}m` : `up ${m}m`;
}
// Not fmtUptime with the prefix cut off: a machine goes stale after seconds.
function fmtAgo(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : m ? `${m}m` : `${Math.floor(s)}s`;
}

// Accent stays on-brand until the reading gets uncomfortable (load_color()).
const loadColor = (base, tenths) => (tenths >= 900 ? C.red : tenths >= 750 ? C.amber : base);
const toTenths = (pct) => (Number.isFinite(pct) ? Math.max(0, Math.min(1000, Math.round(pct * 10))) : 0);

const STATES = {
  online: { label: "LINK OK", color: C.green, period: 1400 },
  stale:  { label: "STALE",   color: C.amber, period: 700 },
  nolink: { label: "NO LINK", color: C.red,   period: 300 },
};

/**
 * Pure: snapshot -> everything the screen shows. No DOM, so it is testable and
 * the same numbers drive the markup, the animation and the aria-label.
 */
function computeView(s, status, ageS, nameOverride) {
  const offline = status !== "online";
  const cpu = toTenths(s?.cpu_percent), ram = toTenths(s?.ram_percent), sto = toTenths(s?.storage_percent);
  const has = !!s;

  let foot = "waiting for host";
  if (has) foot = offline ? `offline ${fmtAgo(ageS)}` : fmtUptime(s.uptime_seconds ?? 0);

  const temp = has && Number.isFinite(s.cpu_temp_c) ? `${Math.round(s.cpu_temp_c)}\u00B0` : "--";
  const net = has
    ? `\u2193 ${fmtRate(s.rx_kbps ?? 0)}  \u2191 ${fmtRate(s.tx_kbps ?? 0)}`
    : "\u2193 --  \u2191 --";
  const ms = s?.latency_ms;
  const ping = !Number.isFinite(ms) ? "-- ms" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;

  return {
    host: `// ${nameOverride || s?.host || "dietpi"}`,
    ping,
    cpu, ram, sto,
    cpuColor: loadColor(C.cyan, cpu), ramColor: loadColor(C.magenta, ram), barColor: loadColor(C.cyan, sto),
    store: has && s.storage_total_gb > 0 ? `STORAGE  ${fmtCapacity(s.storage_free_gb ?? 0)} FREE` : "STORAGE",
    temp, net, foot,
    // Dim the numbers when they are old: four greyed readings say "this is the
    // last thing we heard" from across a desk; a footer line does not.
    ink: offline ? C.dim : C.text,
    hostColor: offline && has ? C.amber : C.dim,
    state: STATES[status],
    aria:
      `PeekESP ${s?.host ?? ""}: ` +
      (has
        ? `CPU ${Math.round(cpu / 10)} percent, RAM ${Math.round(ram / 10)} percent, ` +
          `storage ${Math.round(sto / 10)} percent used, ${offline ? "offline" : "online"}`
        : "waiting for data"),
  };
}

// ---- geometry: the reference layout, in the firmware's own numbers ----------
const GAUGE_D = 66, ARC_W = 6;
const GAUGE_R = GAUGE_D / 2 - ARC_W / 2;           // LVGL strokes inside the box
const CIRC = 2 * Math.PI * GAUGE_R;
const SWEEP = 0.75 * CIRC;                          // 270 degrees of it
const BAR = { x: 7, y: 108, w: 226, h: 8 };

const gaugeSvg = (key, x, caption) => {
  const cx = x + GAUGE_D / 2, cy = 23 + GAUGE_D / 2;
  const common = `cx="${cx}" cy="${cy}" r="${GAUGE_R}" fill="none" stroke-width="${ARC_W}" stroke-linecap="round" transform="rotate(135 ${cx} ${cy})"`;
  return `
    <circle ${common} stroke="${C.track}" stroke-dasharray="${SWEEP.toFixed(2)} ${CIRC.toFixed(2)}"/>
    <circle ${common} data-k="${key}Arc" stroke="${C.cyan}" stroke-dasharray="0 ${CIRC.toFixed(2)}" stroke-opacity="0"/>
    <text data-k="${key}Val" x="${cx}" y="${cy - 5 + 6.5}" text-anchor="middle" font-size="20" fill="${C.text}">0</text>
    <text x="${cx}" y="${cy + 14 + 4}" text-anchor="middle" font-size="12" fill="${C.dim}">${caption}</text>`;
};

const skeleton = (uid) => `
<svg viewBox="0 0 240 135" xmlns="http://www.w3.org/2000/svg" font-family='${FONT}' text-rendering="geometricPrecision" aria-hidden="true">
  <defs>
    <linearGradient id="${uid}-bar"><stop data-k="barA" offset="0" stop-color="${C.cyan}"/><stop offset="1" stop-color="${C.magenta}"/></linearGradient>
    <clipPath id="${uid}-stat"><rect x="155" y="28" width="74" height="56"/></clipPath>
  </defs>
  <rect width="240" height="135" fill="${C.bg}"/>

  <text x="8" y="14" font-size="12" fill="${C.cyan}">PEEK</text>
  <text data-k="host" x="44" y="14" font-size="12" fill="${C.dim}">// dietpi</text>
  <text data-k="ping" x="206" y="14" text-anchor="end" font-size="12" fill="${C.dim}">-- ms</text>
  <rect x="8" y="19" width="224" height="1" fill="${C.cyan}" opacity=".3"/>
  <circle class="ring" cx="227" cy="10" r="5" fill="none" stroke-width="1.5" opacity="0"/>
  <circle class="dot" cx="227" cy="10" r="5"/>

  ${gaugeSvg("cpu", 6, "CPU %")}
  ${gaugeSvg("ram", 78, "RAM %")}

  <rect x="150.5" y="23.5" width="83" height="65" rx="6" fill="${C.panel}" stroke="${C.track}"/>
  <g clip-path="url(#${uid}-stat)">
    <text x="155" y="39" font-size="12" fill="${C.dim}">TEMP</text>
    <text data-k="temp" x="155" y="59" font-size="20" fill="${C.text}">--</text>
    <text data-k="net" x="155" y="79" font-size="12" fill="${C.dim}">\u2193 --  \u2191 --</text>
  </g>

  <text data-k="store" x="8" y="103" font-size="12" fill="${C.dim}">STORAGE</text>
  <text data-k="stoVal" x="232" y="103" text-anchor="end" font-size="12" fill="${C.text}">0%</text>
  <rect x="${BAR.x}" y="${BAR.y}" width="${BAR.w}" height="${BAR.h}" rx="4" fill="${C.track}"/>
  <rect data-k="bar" x="${BAR.x}" y="${BAR.y}" width="0" height="${BAR.h}" rx="4" fill="url(#${uid}-bar)"/>

  <text data-k="foot" x="8" y="129" font-size="12" fill="${C.dim}">waiting for host</text>
  <text data-k="state" x="232" y="129" text-anchor="end" font-size="12" fill="${C.dim}">NO LINK</text>
</svg>`;

/** Owns the SVG: builds it once, then only mutates attributes. */
class PeekScreen {
  constructor(root, { reducedMotion = false } = {}) {
    const uid = `pk${Math.random().toString(36).slice(2, 8)}`;
    root.innerHTML = skeleton(uid);
    this.el = {};
    root.querySelectorAll("[data-k]").forEach((n) => (this.el[n.dataset.k] = n));
    this.dot = root.querySelector(".dot");
    this.ring = root.querySelector(".ring");
    this.root = root;
    this.reduced = reducedMotion;
    this.shown = { cpu: 0, ram: 0, sto: 0 };
    this.raf = {};
    this._lastState = null;
  }

  _text(k, v) { if (this.el[k].textContent !== v) this.el[k].textContent = v; }

  // Deliberate departure from the glass: the device clips the throughput line
  // at the panel edge ("\u2193 5.3M  \u2191 88" for 880K). On a dashboard that is
  // data silently missing, so when - and only when - the line overflows, the
  // type shrinks to fit (never below 9 px). Measured at the nominal size every
  // time, because a shrunk label reports its shrunk width and could never grow
  // back. Font size rather than textLength: it behaves the same everywhere.
  _fit(k, max, nominal = 12) {
    const el = this.el[k];
    el.setAttribute("font-size", nominal);
    const w = typeof el.getComputedTextLength === "function" ? el.getComputedTextLength() : 0;
    if (w > max) el.setAttribute("font-size", Math.max(9, (nominal * max) / w).toFixed(2));
  }
  _fill(k, v) { this.el[k].setAttribute("fill", v); }

  _paintGauge(k, tenths) {
    this.shown[k] = tenths;
    const f = tenths / 1000;
    const arc = this.el[`${k}Arc`];
    arc.setAttribute("stroke-dasharray", `${(SWEEP * f).toFixed(2)} ${CIRC.toFixed(2)}`);
    arc.setAttribute("stroke-opacity", f < 0.0005 ? "0" : "1");  // a zero-length round cap is a dot
    this._text(`${k}Val`, String(Math.round(tenths / 10)));
  }
  _paintBar(tenths) {
    this.shown.sto = tenths;
    this.el.bar.setAttribute("width", ((BAR.w * tenths) / 1000).toFixed(2));
    this._text("stoVal", `${Math.round(tenths / 10)}%`);
  }

  // Drives the easing by hand, as the firmware does, rather than leaning on CSS
  // transitions: the number, the arc and the bar then move as one thing.
  _sweep(k, to, paint, instant) {
    cancelAnimationFrame(this.raf[k]);
    const from = this.shown[k];
    if (from === to) return paint(to);
    if (instant || this.reduced) return paint(to);
    const t0 = performance.now();
    const step = (now) => {
      const p = Math.min(1, (now - t0) / ANIM_MS);
      paint(Math.round(from + (to - from) * (1 - Math.pow(1 - p, 3))));
      if (p < 1) this.raf[k] = requestAnimationFrame(step);
    };
    this.raf[k] = requestAnimationFrame(step);
  }

  update(v, { instant = false } = {}) {
    this._text("host", v.host);
    this._text("ping", v.ping);
    this._text("temp", v.temp);
    if (this.el.net.textContent !== v.net) { this._text("net", v.net); this._fit("net", 74); }
    this._text("store", v.store);
    this._text("foot", v.foot);
    this._text("state", v.state.label);

    this.el.cpuArc.setAttribute("stroke", v.cpuColor);
    this.el.ramArc.setAttribute("stroke", v.ramColor);
    this.el.barA.setAttribute("stop-color", v.barColor);  // end stop stays magenta, as on the device
    this._fill("cpuVal", v.ink); this._fill("ramVal", v.ink);
    this._fill("stoVal", v.ink); this._fill("temp", v.ink);
    this._fill("host", v.hostColor);

    this._sweep("cpu", v.cpu, (t) => this._paintGauge("cpu", t), instant);
    this._sweep("ram", v.ram, (t) => this._paintGauge("ram", t), instant);
    this._sweep("sto", v.sto, (t) => this._paintBar(t), instant);

    if (this._lastState !== v.state) {
      this._lastState = v.state;
      this.dot.setAttribute("fill", v.state.color);
      this.ring.setAttribute("stroke", v.state.color);
      this.root.style.setProperty("--p", `${v.state.period}ms`);
    }
  }
}

// ---- the card ---------------------------------------------------------------
const STYLE = `
  :host { display: block; }
  ha-card { background: none; border: 0; box-shadow: none; }
  .bezel {
    background: #000; padding: 10px; border-radius: 16px;
    box-shadow: inset 0 0 0 1px #1b2130, 0 1px 2px rgba(0,0,0,.35);
    cursor: pointer; outline: none; -webkit-tap-highlight-color: transparent;
  }
  .bezel:focus-visible { box-shadow: inset 0 0 0 1px #1b2130, 0 0 0 2px var(--primary-color, ${C.cyan}); }
  .glass { border-radius: 6px; overflow: hidden; line-height: 0; }
  svg { width: 100%; height: auto; display: block; }
  /* The status dot: a radar ping. Same four states and periods as apply_state(). */
  .dot  { animation: dot  var(--p, 900ms) ease-in-out infinite alternate; }
  .ring { animation: ring var(--p, 900ms) ease-in-out infinite alternate;
          transform-box: fill-box; transform-origin: center; }
  @keyframes dot  { from { opacity: .35; } to { opacity: 1; } }
  @keyframes ring { from { transform: scale(1); opacity: 1; } to { transform: scale(2.4); opacity: 0; } }
  @media (prefers-reduced-motion: reduce) { .dot, .ring { animation: none; } .ring { opacity: 0; } }
  .msg { padding: 16px; color: var(--secondary-text-color); line-height: 1.4; }
`;

class PeekESPCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._snap = null;       // last snapshot, kept so the card can say "no link" and still show it
    this._fetchedAt = null;  // server timestamp of that snapshot
    this._seenAt = 0;        // when this browser first saw it (performance.now())
    this._unavailable = false;
  }

  setConfig(config) {
    if (!config || !config.entity) throw new Error("Choose a PeekESP \u201CScreen\u201D entity");
    this._config = config;
    this._snap = null; this._fetchedAt = null;  // a different entity must not show the old one's numbers
    this._build();
    if (this._hass) this.hass = this._hass;     // re-ingest now rather than waiting for the next state push
  }

  // DOM only. Kept apart from setConfig so recovering from "entity not found"
  // can rebuild the screen without throwing away the snapshot it just received.
  _build() {
    const entity = this._config.entity;
    this.shadowRoot.innerHTML = `<style>${STYLE}</style><ha-card><div class="bezel" role="button" tabindex="0"><div class="glass"></div></div></ha-card>`;
    this._bezel = this.shadowRoot.querySelector(".bezel");
    this._screen = new PeekScreen(this.shadowRoot.querySelector(".glass"), {
      reducedMotion: matchMedia("(prefers-reduced-motion: reduce)").matches,
    });
    const open = () =>
      this.dispatchEvent(new CustomEvent("hass-more-info", { bubbles: true, composed: true, detail: { entityId: entity } }));
    this._bezel.addEventListener("click", open);
    this._bezel.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
  }

  set hass(hass) {
    this._hass = hass;
    const st = hass.states[this._config?.entity];
    if (!st) {
      this._missing = true;
    } else {
      this._missing = false;
      this._unavailable = st.state === "unavailable" || st.state === "unknown";
      const a = st.attributes;
      if (!this._unavailable && a.peekesp && a.fetched_at !== this._fetchedAt) {
        this._fetchedAt = a.fetched_at;
        this._seenAt = performance.now();
        this._snap = a;
      }
    }
    this._paint();
  }

  connectedCallback() { this._timer = setInterval(() => this._paint(), 1000); }
  disconnectedCallback() { clearInterval(this._timer); }

  _paint() {
    if (!this._config) return;
    if (this._missing) {
      // The entity can appear later (integration still loading), so remember
      // that the screen was replaced and rebuild it when the entity shows up.
      this._replaced = true;
      this.shadowRoot.querySelector(".glass").innerHTML = `<div class="msg">Entity <b>${this._config.entity}</b> was not found.</div>`;
      return;
    }
    if (this._replaced) { this._replaced = false; this._build(); }
    const s = this._snap;
    // The age is counted on our own clock from the moment a snapshot arrived,
    // so a frozen relay shows a growing "offline 3m", not a number that stopped.
    const age = s ? (s.age_s || 0) + (performance.now() - this._seenAt) / 1000 : Infinity;
    const status = this._unavailable ? "nolink" : age <= (s?.offline_after ?? 30) ? "online" : "stale";
    const v = computeView(s, status, age, this._config.name);
    this._screen.update(v);
    this._bezel.setAttribute("aria-label", v.aria);
  }

  getCardSize() { return 3; }
  getGridOptions() { return { columns: 6, rows: 4, min_columns: 3, min_rows: 2 }; }
  static getConfigElement() { return document.createElement("peekesp-card-editor"); }
  static getStubConfig(hass) {
    const id = Object.keys(hass?.states ?? {}).find((k) => hass.states[k].attributes?.peekesp === true);
    return { entity: id ?? "" };
  }
}

class PeekESPCardEditor extends HTMLElement {
  setConfig(config) { this._config = config; this._render(); }
  set hass(h) { this._hass = h; if (this._form) this._form.hass = h; }
  _render() {
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (s) => ({ entity: "Screen entity", name: "Label (optional)" }[s.name] ?? s.name);
      this._form.addEventListener("value-changed", (e) =>
        this.dispatchEvent(new CustomEvent("config-changed", { bubbles: true, composed: true, detail: { config: e.detail.value } })));
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.data = this._config;
    this._form.schema = [
      { name: "entity", required: true, selector: { entity: { integration: "peekesp", domain: "sensor" } } },
      { name: "name", selector: { text: {} } },
    ];
  }
}

if (typeof customElements !== "undefined") {
  if (!customElements.get("peekesp-card")) customElements.define("peekesp-card", PeekESPCard);
  if (!customElements.get("peekesp-card-editor")) customElements.define("peekesp-card-editor", PeekESPCardEditor);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "peekesp-card",
    name: "PeekESP screen",
    description: "The PeekESP device dashboard for one of your machines.",
    preview: false,
  });
  console.info(`%c PEEKESP-CARD %c ${VERSION} `, "background:#00E5FF;color:#05070E", "background:#0B1220;color:#E6EDF7");
}
if (typeof globalThis !== "undefined" && globalThis.__PEEKESP_TEST__) {
  globalThis.__PEEKESP_TEST__.api = { computeView, PeekScreen, fmtRate, fmtCapacity, fmtUptime, fmtAgo, loadColor, C };
}
