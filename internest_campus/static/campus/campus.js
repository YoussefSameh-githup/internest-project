(function () {
  "use strict";

  function open(dialog) {
    if (!dialog) return;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  document.querySelectorAll("[data-campus-open]").forEach(function (btn) {
    btn.addEventListener("click", function () { open(document.getElementById(btn.dataset.campusOpen)); });
  });

  // Close when clicking the backdrop.
  document.querySelectorAll("dialog.campus-dialog").forEach(function (dialog) {
    dialog.addEventListener("click", function (e) { if (e.target === dialog) dialog.close(); });
  });

  // Auto-open: referral links, ?open=1, form errors, or a freshly generated campaign card.
  var auto = document.querySelector("dialog[data-autoopen]");
  if (auto) open(auto);

  document.querySelectorAll("[data-campus-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var label = btn.querySelector("span");
      var done = function () { label.textContent = btn.dataset.copiedLabel; };
      if (navigator.clipboard) {
        navigator.clipboard.writeText(btn.dataset.campusCopy).then(done);
      } else {
        var input = btn.closest("dialog").querySelector("input[readonly]");
        input.select();
        document.execCommand("copy");
        done();
      }
    });
  });
})();
