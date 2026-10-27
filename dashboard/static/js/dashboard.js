(function () {
  "use strict";
  var region = document.getElementById("live-region");
  if (!region) return;

  var seconds = parseInt(region.dataset.refreshSeconds, 10) || 0;
  var inFlight = false;
  var timer = null;
  var statusEl = document.getElementById("refresh-status");
  var lastEl = document.getElementById("last-refresh");
  var alertEl = document.getElementById("refresh-alert");
  var button = document.getElementById("refresh-btn");
  var idleText = statusEl ? statusEl.textContent : "";

  function stamp() {
    if (lastEl) lastEl.textContent = new Date().toLocaleTimeString();
  }

  function setAlert(message) {
    if (!alertEl) return;
    alertEl.textContent = message || "";
    alertEl.hidden = !message;
  }

  function partialUrl() {
    var url = new URL(window.location.href);
    url.searchParams.set("_partial", "1");
    return url.toString();
  }

  function schedule() {
    clearTimeout(timer);
    if (seconds > 0) timer = setTimeout(refresh, seconds * 1000);
  }

  function refresh() {
    if (inFlight) return;               // never overlap requests
    inFlight = true;
    region.setAttribute("aria-busy", "true");
    if (statusEl) statusEl.textContent = "Refreshing\u2026";
    var controller = new AbortController();
    var abortTimer = setTimeout(function () { controller.abort(); }, 10000);

    fetch(partialUrl(), { cache: "no-store", signal: controller.signal,
                          headers: { Accept: "text/html" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (html) {
        region.innerHTML = html;        // server-rendered, autoescaped HTML
        if (window.SupervisorCharts) window.SupervisorCharts.renderAll(region);
        setAlert("");
        stamp();
      })
      .catch(function () {
        // Keep the last good data on screen; only report the failure.
        setAlert("Could not refresh data. The values shown may be out of date.");
      })
      .finally(function () {
        clearTimeout(abortTimer);
        inFlight = false;
        region.removeAttribute("aria-busy");
        if (statusEl) statusEl.textContent = idleText;
        schedule();
      });
  }

  if (window.SupervisorCharts) window.SupervisorCharts.renderAll(region);
  stamp();
  if (button) button.addEventListener("click", refresh);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && seconds > 0) refresh();
  });
  schedule();
})();
