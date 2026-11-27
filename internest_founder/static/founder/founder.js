/* Founder Control Center: mobile menu + the overview health panel (fetch with loading / error states). */
(function () {
  "use strict";
  var side = document.getElementById("fc-side");
  var toggle = document.querySelector("[data-fc-menu]");
  if (side && toggle) {
    var setOpen = function (open) {
      side.classList.toggle("is-open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    };
    toggle.addEventListener("click", function () { setOpen(!side.classList.contains("is-open")); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") { setOpen(false); } });
    document.addEventListener("click", function (e) {
      if (side.classList.contains("is-open") && !side.contains(e.target) && !toggle.contains(e.target)) { setOpen(false); }
    });
  }

  var panel = document.querySelector("[data-fc-health]");
  if (!panel) { return; }
  var body = panel.querySelector("[data-fc-health-body]");
  var error = panel.querySelector("[data-fc-health-error]");
  fetch(panel.getAttribute("data-fc-health"), { credentials: "same-origin", headers: { "Accept": "application/json" } })
    .then(function (r) { if (!r.ok) { throw new Error(r.status); } return r.json(); })
    .then(function (data) {
      body.className = "";
      body.textContent = "";
      data.checks.forEach(function (c) {
        var row = document.createElement("div");
        row.className = "fc-health-row is-" + c.state;
        var dot = document.createElement("span"); dot.className = "fc-dot";
        var label = document.createElement("strong"); label.textContent = c.label;
        var detail = document.createElement("span"); detail.textContent = c.detail;
        row.appendChild(dot); row.appendChild(label); row.appendChild(detail);
        body.appendChild(row);
      });
    })
    .catch(function () { body.hidden = true; error.hidden = false; });
})();
