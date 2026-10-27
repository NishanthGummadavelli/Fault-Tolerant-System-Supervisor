// Runs the chart renderers against JSON on stdin using a minimal fake DOM.
const fs = require("fs");
function node(name) {
  return { name, attrs: {}, children: [], text: "", className: "",
    setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return this.attrs[k]; },
    appendChild(c) { this.children.push(c); return c; },
    set textContent(v) { this.text = v; if (v === "") this.children = []; }, get textContent() { return this.text; } };
}
global.document = { createElementNS: (_, n) => node(n), createElement: n => node(n),
  createTextNode: t => ({ name: "#text", text: t, children: [] }) };
const charts = require("../dashboard/static/js/charts.js");
const payload = JSON.parse(fs.readFileSync(0, "utf8"));
function count(n, name) { return (n.name === name ? 1 : 0) + n.children.reduce((a, c) => a + count(c, name), 0); }
const out = {};
for (const [type, data] of Object.entries(payload)) {
  const host = node("div");
  host.attrs["data-chart-type"] = type; host.attrs["data-chart"] = JSON.stringify(data);
  const scope = { querySelectorAll: () => [host] };
  charts.renderAll(scope);
  out[type] = { circles: count(host, "circle"), paths: count(host, "path"), rects: count(host, "rect"),
                li: count(host, "li"), error: JSON.stringify(host).includes("could not be displayed") };
}
console.log(JSON.stringify(out));
