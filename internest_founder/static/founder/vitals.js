/* Real-visitor Web Vitals for the Founder Performance Center. Loaded only for anonymous visitors on public pages,
   sampled (data-rate), no cookies, no ids: sends {page path, device class, metric values} once, when the tab is hidden.
   LCP, CLS (largest session window), INP (slowest interaction: an approximation of the official p98), FCP, TTFB. */
(function () {
  "use strict";
  var script = document.currentScript;
  var rate = parseFloat(script && script.getAttribute("data-rate")) || 0;
  if (!("PerformanceObserver" in window) || !navigator.sendBeacon || Math.random() >= rate) { return; }
  var endpoint = script.getAttribute("data-endpoint");
  var values = {};
  var observe = function (type, cb, opts) {
    try {
      var po = new PerformanceObserver(function (list) { list.getEntries().forEach(cb); });
      po.observe(Object.assign({ type: type, buffered: true }, opts || {}));
    } catch (e) { /* unsupported entry type */ }
  };
  observe("largest-contentful-paint", function (e) { values.LCP = e.startTime; });
  observe("paint", function (e) { if (e.name === "first-contentful-paint") { values.FCP = e.startTime; } });
  var session = 0, sessionStart = 0, last = 0;
  observe("layout-shift", function (e) {
    if (e.hadRecentInput) { return; }
    if (e.startTime - last > 1000 || e.startTime - sessionStart > 5000) { session = 0; sessionStart = e.startTime; }
    session += e.value; last = e.startTime;
    values.CLS = Math.max(values.CLS || 0, session);
  });
  observe("event", function (e) { if (e.interactionId) { values.INP = Math.max(values.INP || 0, e.duration); } }, { durationThreshold: 40 });
  var nav = performance.getEntriesByType && performance.getEntriesByType("navigation")[0];
  if (nav && nav.responseStart > 0) { values.TTFB = nav.responseStart; }
  var sent = false;
  var send = function () {
    if (sent || document.visibilityState !== "hidden") { return; }
    sent = true;
    var metrics = Object.keys(values).map(function (k) { return { n: k, v: Math.round(values[k] * 1000) / 1000 }; });
    if (!metrics.length) { return; }
    var device = window.matchMedia("(max-width: 767px)").matches ? "mobile" : "desktop";
    var body = JSON.stringify({ p: location.pathname, d: device, m: metrics });
    navigator.sendBeacon(endpoint, new Blob([body], { type: "application/json" }));
  };
  document.addEventListener("visibilitychange", send);
  window.addEventListener("pagehide", function () { if (document.visibilityState !== "hidden") { return; } send(); });
})();
