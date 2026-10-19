const { JSDOM } = require("jsdom");
const fs = require("fs");
const dom = new JSDOM("<!doctype html><div id=r></div>", { runScripts: "outside-only", pretendToBeVisual: true });
const w = dom.window;
w.__PEEKESP_TEST__ = {};
w.matchMedia = () => ({ matches: true });
w.eval(fs.readFileSync("custom_components/peekesp/frontend/peekesp-card.js", "utf8"));
const { computeView, PeekScreen } = w.__PEEKESP_TEST__.api;

const base = { host: "dietpi", cpu_percent: 12.5, ram_percent: 43.2, storage_percent: 61, storage_total_gb: 117.9,
  storage_free_gb: 46.0, cpu_temp_c: 48.3, rx_kbps: 128.4, tx_kbps: 12.9, uptime_seconds: 271830, latency_ms: 212, age_s: 2 };
const cases = {
  healthy: [base, "online", 2],
  loaded:  [{ ...base, host: "nas", cpu_percent: 82, ram_percent: 93.5, storage_percent: 95, storage_total_gb: 12000,
              storage_free_gb: 610, rx_kbps: 5400, tx_kbps: 880, cpu_temp_c: 71, uptime_seconds: 5400 }, "online", 2],
  stale:   [{ ...base, host: "laptop" }, "stale", 190],
};
for (const [name, [snap, status, age]] of Object.entries(cases)) {
  const root = w.document.getElementById("r"); root.innerHTML = "";
  w.SVGElement.prototype.getComputedTextLength = function(){ return this.textContent.length * 6.6; };
  const scr = new PeekScreen(root);
  scr.update(computeView(snap, status, age), { instant: true });
  fs.writeFileSync(`/tmp/prev_${name}.svg`, root.innerHTML.trim());
  console.log(name, JSON.stringify(computeView(snap, status, age), (k, v) => (k === "state" ? v.label : v)));
}
