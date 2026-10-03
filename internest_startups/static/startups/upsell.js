(function () {
  "use strict";
  var dialog = document.getElementById("upsell-dialog");
  if (!dialog) return;
  function open(e) {
    if (e) e.preventDefault();
    if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
  }
  document.querySelectorAll("[data-upsell]").forEach(function (el) { el.addEventListener("click", open); });
  dialog.addEventListener("click", function (e) { if (e.target === dialog) dialog.close(); });
  if (dialog.hasAttribute("data-autoopen")) open();
})();
