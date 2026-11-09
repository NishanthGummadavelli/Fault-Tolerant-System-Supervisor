/* Lightweight SVG charts. No external library, so nothing can fail to load. */
(function (root) {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var W = 720, H = 280, M = { l: 52, r: 14, t: 12, b: 34 };
  var SERIES_CLASSES = ["s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7"];

  function svgEl(name, attrs, text) {
    var node = document.createElementNS(NS, name);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function newSvg() {
    return svgEl("svg", { viewBox: "0 0 " + W + " " + H, "class": "chart-svg",
                          preserveAspectRatio: "xMidYMid meet", "aria-hidden": "true" });
  }

  function tip(node, text) {
    node.appendChild(svgEl("title", {}, text));
    return node;
  }

  /* Choose "nice" axis ticks covering [0, max]. */
  function niceTicks(max, count) {
    if (!(max > 0)) return { max: 1, ticks: [0, 1] };
    var raw = max / (count || 4);
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var step = [1, 2, 5, 10].map(function (m) { return m * mag; })
      .filter(function (s) { return s >= raw; })[0];
    var ticks = [];
    for (var v = 0; v <= max + step * 0.999; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
    return { max: ticks[ticks.length - 1], ticks: ticks };
  }

  function fmtTime(ms, spanMs) {
    var d = new Date(ms);
    var hm = ("0" + d.getUTCHours()).slice(-2) + ":" + ("0" + d.getUTCMinutes()).slice(-2);
    if (spanMs > 36 * 3600 * 1000) {
      return ("0" + (d.getUTCMonth() + 1)).slice(-2) + "-" + ("0" + d.getUTCDate()).slice(-2) + " " + hm;
    }
    return hm;
  }

  function fullTime(iso) { return iso ? iso.replace("T", " ").replace("+00:00", " UTC") : ""; }

  function legend(container, items) {
    var ul = document.createElement("ul");
    ul.className = "legend";
    items.forEach(function (it) {
      var li = document.createElement("li");
      var sw = document.createElement("span");
      sw.className = "swatch " + it.cls;
      sw.setAttribute("aria-hidden", "true");
      li.appendChild(sw);
      li.appendChild(document.createTextNode(" " + it.label));
      ul.appendChild(li);
    });
    container.appendChild(ul);
  }

  function emptyMessage(container, text) {
    var p = document.createElement("p");
    p.className = "empty-state";
    p.textContent = text;
    container.appendChild(p);
  }

  function axes(svg, xTicks, yInfo, unit, plotW, plotH) {
    yInfo.ticks.forEach(function (v) {
      var y = M.t + plotH - (v / yInfo.max) * plotH;
      svg.appendChild(svgEl("line", { x1: M.l, x2: M.l + plotW, y1: y, y2: y, "class": "grid-line" }));
      svg.appendChild(svgEl("text", { x: M.l - 6, y: y + 4, "class": "axis-text", "text-anchor": "end" }, String(v)));
    });
    xTicks.forEach(function (t) {
      svg.appendChild(svgEl("text", { x: t.x, y: H - 12, "class": "axis-text", "text-anchor": "middle" }, t.label));
    });
    if (unit) svg.appendChild(svgEl("text", { x: 4, y: 10, "class": "axis-text" }, unit));
  }

  /* ---- Response-time line chart: 2xx points only; failures as markers ---- */
  function timeseries(container, data) {
    var series = (data.series || []).filter(function (s) { return s.points.length || s.failures.length; });
    if (!series.length) { emptyMessage(container, "No health checks recorded in this period."); return; }

    var t0 = Date.parse(data.range_start), t1 = Date.parse(data.range_end);
    var plotW = W - M.l - M.r, plotH = H - M.t - M.b;
    var maxV = 0;
    series.forEach(function (s) { s.points.forEach(function (p) { maxV = Math.max(maxV, p.v); }); });
    var single = series.length === 1 ? series[0] : null;
    if (single) maxV = Math.max(maxV, single.healthy_ms, single.unhealthy_ms);
    var y = niceTicks(maxV * 1.1, 4);
    var xs = function (iso) { return M.l + ((Date.parse(iso) - t0) / (t1 - t0)) * plotW; };
    var ys = function (v) { return M.t + plotH - (v / y.max) * plotH; };

    var xTicks = [];
    for (var i = 0; i <= 4; i++) {
      var tm = t0 + ((t1 - t0) * i) / 4;
      xTicks.push({ x: M.l + (plotW * i) / 4, label: fmtTime(tm, t1 - t0) });
    }
    var svg = newSvg();
    axes(svg, xTicks, y, "ms", plotW, plotH);

    if (single) {
      [[single.healthy_ms, "Healthy threshold"], [single.unhealthy_ms, "Unhealthy threshold"]].forEach(function (th) {
        svg.appendChild(tip(svgEl("line", { x1: M.l, x2: M.l + plotW, y1: ys(th[0]), y2: ys(th[0]), "class": "threshold-line" }),
                            th[1] + ": " + th[0] + " ms"));
      });
    }

    series.forEach(function (s, idx) {
      var cls = SERIES_CLASSES[idx % SERIES_CLASSES.length];
      var path = "", prev = null;
      s.points.forEach(function (p) {
        var x = xs(p.t), yy = ys(p.v);
        // Break the line where monitoring data is missing.
        var gap = prev && (Date.parse(p.t) - Date.parse(prev.t)) / 1000 > s.gap_seconds;
        path += (path === "" || gap ? "M" : "L") + x.toFixed(1) + " " + yy.toFixed(1) + " ";
        prev = p;
      });
      if (path) svg.appendChild(svgEl("path", { d: path, "class": "line " + cls, fill: "none" }));
      s.points.forEach(function (p) {
        svg.appendChild(tip(svgEl("circle", { cx: xs(p.t).toFixed(1), cy: ys(p.v).toFixed(1), r: 3, "class": "dot " + cls }),
          s.name + "\n" + fullTime(p.t) + "\n" + p.v + " ms (HTTP " + p.status + ", " + p.state.toLowerCase() + ")"));
      });
      s.failures.forEach(function (f) {
        var x = xs(f.t), yb = M.t + plotH;
        var label = s.name + "\n" + fullTime(f.t) + "\n" +
          (f.kind || "FAILURE").replace(/_/g, " ").toLowerCase() +
          (f.status !== null ? " (HTTP " + f.status + ")" : " (no response)") +
          (f.error_type ? "\n" + f.error_type : "");
        var g = svgEl("g", { "class": "fail-mark" });
        g.appendChild(svgEl("path", { d: "M" + (x - 4) + " " + (yb - 8) + "L" + (x + 4) + " " + yb + "M" + (x + 4) + " " + (yb - 8) + "L" + (x - 4) + " " + yb }));
        svg.appendChild(tip(g, label));
      });
    });
    container.appendChild(svg);

    var items = series.map(function (s, idx) { return { cls: SERIES_CLASSES[idx % SERIES_CLASSES.length], label: s.name }; });
    items.push({ cls: "fail", label: "\u00d7 failed request (no latency plotted)" });
    legend(container, items);
    if (data.truncated) emptyMessage(container, "Showing the most recent records only; older points in this range are not drawn.");
  }

  /* ---- Stacked bars: checks per period by health state ---- */
  function stackedbars(container, data) {
    var buckets = data.buckets || [];
    var maxTotal = 0;
    buckets.forEach(function (b) { maxTotal = Math.max(maxTotal, b.HEALTHY + b.DEGRADED + b.UNHEALTHY); });
    if (!maxTotal) { emptyMessage(container, "No health checks recorded in this period."); return; }

    var plotW = W - M.l - M.r, plotH = H - M.t - M.b;
    var y = niceTicks(maxTotal, 4);
    var slot = plotW / buckets.length, barW = Math.max(2, slot * 0.72);
    var span = buckets.length * data.bucket_seconds * 1000;
    var xTicks = [];
    var every = Math.max(1, Math.ceil(buckets.length / 6));
    buckets.forEach(function (b, i) {
      if (i % every === 0) xTicks.push({ x: M.l + slot * i + slot / 2, label: fmtTime(Date.parse(b.start), span) });
    });
    var svg = newSvg();
    axes(svg, xTicks, y, "checks", plotW, plotH);
    buckets.forEach(function (b, i) {
      var x = M.l + slot * i + (slot - barW) / 2, base = M.t + plotH;
      [["HEALTHY", "ok"], ["DEGRADED", "warn"], ["UNHEALTHY", "bad"]].forEach(function (pair) {
        var h = (b[pair[0]] / y.max) * plotH;
        if (h <= 0) return;
        base -= h;
        svg.appendChild(tip(svgEl("rect", { x: x.toFixed(1), y: base.toFixed(1), width: barW.toFixed(1), height: h.toFixed(1), "class": "bar tone-" + pair[1] }),
          fullTime(b.start) + "\n" + pair[0].toLowerCase() + ": " + b[pair[0]]));
      });
    });
    container.appendChild(svg);
    legend(container, [{ cls: "tone-ok", label: "Healthy" }, { cls: "tone-warn", label: "Degraded" }, { cls: "tone-bad", label: "Unhealthy" }]);
    if (data.truncated) emptyMessage(container, "Counts use the most recent records only.");
  }

  /* ---- Donut: share per category ---- */
  function donut(container, data) {
    var items = data.items || [];
    var total = items.reduce(function (a, b) { return a + b.value; }, 0);
    if (!total) { emptyMessage(container, "No services to chart yet."); return; }
    var size = 200, r = 74, c = 2 * Math.PI * r, offset = 0;
    var svg = svgEl("svg", { viewBox: "0 0 " + size + " " + size, "class": "chart-svg chart-svg--donut", "aria-hidden": "true" });
    items.forEach(function (it) {
      if (!it.value) return;
      var len = (it.value / total) * c;
      var seg = svgEl("circle", { cx: size / 2, cy: size / 2, r: r, fill: "none", "stroke-width": 26,
        "stroke-dasharray": len.toFixed(2) + " " + (c - len).toFixed(2), "stroke-dashoffset": (-offset).toFixed(2),
        transform: "rotate(-90 " + size / 2 + " " + size / 2 + ")", "class": "seg tone-" + it.tone });
      svg.appendChild(tip(seg, it.label + ": " + it.value));
      offset += len;
    });
    svg.appendChild(svgEl("text", { x: size / 2, y: size / 2 + 6, "text-anchor": "middle", "class": "donut-total" }, String(total)));
    container.appendChild(svg);
    legend(container, items.map(function (it) { return { cls: "tone-" + it.tone, label: it.label + " (" + it.value + ")" }; }));
  }

  /* ---- Horizontal bars ---- */
  function hbars(container, data) {
    var items = data.items || [];
    var max = data.max || items.reduce(function (a, b) { return Math.max(a, b.value); }, 0);
    if (!items.length || !max) { emptyMessage(container, data.max ? "No checks recorded in this period." : "No incidents recorded."); return; }
    var ul = document.createElement("ul");
    ul.className = "hbars";
    items.forEach(function (it) {
      var li = document.createElement("li");
      var label = document.createElement("span");
      label.textContent = it.label;
      var track = document.createElement("span");
      track.className = "hbar-track";
      var fill = document.createElement("span");
      fill.className = "hbar-fill tone-" + it.tone;
      fill.setAttribute("data-width", String(Math.round((it.value / max) * 100)));
      fill.className += " w" + Math.round((it.value / max) * 20) * 5;   // width classes in CSS (CSP-safe)
      track.appendChild(fill);
      var val = document.createElement("strong");
      val.textContent = String(it.value) + (it.suffix || "");
      li.appendChild(label); li.appendChild(track); li.appendChild(val);
      ul.appendChild(li);
    });
    container.appendChild(ul);
  }

  var RENDERERS = { timeseries: timeseries, stackedbars: stackedbars, donut: donut, hbars: hbars };

  function renderAll(scope) {
    var nodes = (scope || document).querySelectorAll(".chart[data-chart]");
    Array.prototype.forEach.call(nodes, function (node) {
      try {
        var data = JSON.parse(node.getAttribute("data-chart"));
        node.textContent = "";
        RENDERERS[node.getAttribute("data-chart-type")](node, data);
      } catch (e) {
        node.textContent = "";
        emptyMessage(node, "This chart could not be displayed.");
      }
    });
  }

  var api = { renderAll: renderAll, niceTicks: niceTicks, fmtTime: fmtTime };
  root.SupervisorCharts = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
