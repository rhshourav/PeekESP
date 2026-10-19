const { JSDOM } = require("jsdom"); const fs = require("fs"); const assert = require("assert");
const dom = new JSDOM("<!doctype html><body></body>", { runScripts: "outside-only", pretendToBeVisual: true });
const w = dom.window; w.matchMedia = () => ({ matches: true });   // reduced motion: values land instantly
w.eval(fs.readFileSync("custom_components/peekesp/frontend/peekesp-card.js", "utf8"));
assert(w.customElements.get("peekesp-card") && w.customElements.get("peekesp-card-editor"));
assert(w.customCards.some(c => c.type === "peekesp-card"));

const card = w.document.createElement("peekesp-card"); w.document.body.appendChild(card);
assert.throws(() => card.setConfig({}), /entity/);          // a clear error, not a blank card
card.setConfig({ entity: "sensor.dietpi_screen" });
const txt = (k) => card.shadowRoot.querySelector(`[data-k="${k}"]`).textContent;
const attrs = { peekesp: true, host: "dietpi", cpu_percent: 82, ram_percent: 40, storage_percent: 50, storage_total_gb: 100,
  storage_free_gb: 50, cpu_temp_c: 55, rx_kbps: 10, tx_kbps: 2, uptime_seconds: 7200, age_s: 1, offline_after: 30, latency_ms: 90, fetched_at: 1 };

card.hass = { states: { "sensor.dietpi_screen": { state: "online", attributes: attrs } } };
assert.equal(txt("state"), "LINK OK"); assert.equal(txt("cpuVal"), "82"); assert.equal(txt("foot"), "up 2h 0m");
assert.match(card.shadowRoot.querySelector(".bezel").getAttribute("aria-label"), /CPU 82 percent.*online/);
assert.equal(card.shadowRoot.querySelector('[data-k="cpuArc"]').getAttribute("stroke"), "#FFC145");   // 82 % -> amber

// relay unreachable: entity goes unavailable and loses its attributes - the card must keep the last reading
card.hass = { states: { "sensor.dietpi_screen": { state: "unavailable", attributes: {} } } };
assert.equal(txt("state"), "NO LINK"); assert.equal(txt("cpuVal"), "82"); assert.match(txt("foot"), /^offline \d+s$/);

// fresh snapshot with an old age_s (host stopped pushing, relay still up): STALE, not NO LINK
card.hass = { states: { "sensor.dietpi_screen": { state: "offline", attributes: { ...attrs, age_s: 300, fetched_at: 2 } } } };
assert.equal(txt("state"), "STALE"); assert.equal(txt("foot"), "offline 5m");

// entity missing, then appearing (integration still loading)
card.hass = { states: {} };
assert.match(card.shadowRoot.querySelector(".glass").textContent, /was not found/);
card.hass = { states: { "sensor.dietpi_screen": { state: "online", attributes: { ...attrs, fetched_at: 3 } } } };
assert.equal(txt("state"), "LINK OK");                       // screen rebuilt after the message

// click opens more-info
let got = null; card.addEventListener("hass-more-info", e => (got = e.detail.entityId));
card.shadowRoot.querySelector(".bezel").click(); assert.equal(got, "sensor.dietpi_screen");

// stub config finds a PeekESP entity
const C = w.customElements.get("peekesp-card");
assert.equal(C.getStubConfig({ states: { "sensor.x": { attributes: {} }, "sensor.y": { attributes: { peekesp: true } } } }).entity, "sensor.y");
card.disconnectedCallback();
console.log("card behaviour: all assertions passed");
