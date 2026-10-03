(function () {
  "use strict";

  // Compose modal
  var compose = document.getElementById("compose-dialog");
  function openDialog(d) { if (!d) return; if (d.showModal) d.showModal(); else d.setAttribute("open", ""); }
  document.querySelectorAll('[data-open-dialog="compose-dialog"]').forEach(function (b) {
    b.addEventListener("click", function () { openDialog(compose); });
  });
  if (compose) {
    compose.addEventListener("click", function (e) { if (e.target === compose) compose.close(); });
    if (compose.hasAttribute("data-autoopen")) openDialog(compose);
  }

  // Comment drawers (smooth expand/collapse)
  function setThread(btn, open) {
    var thread = document.getElementById(btn.dataset.toggleComments);
    if (!thread) return;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      thread.hidden = false;
      thread.classList.remove("is-open");
      void thread.offsetWidth; // restart the animation
      thread.classList.add("is-open");
      var input = thread.querySelector("textarea");
      if (input) input.focus({ preventScroll: true });
    } else {
      thread.hidden = true;
      thread.classList.remove("is-open");
    }
  }
  document.querySelectorAll("[data-toggle-comments]").forEach(function (btn) {
    btn.addEventListener("click", function () { setThread(btn, btn.getAttribute("aria-expanded") !== "true"); });
  });
  // Re-open the right thread after posting a comment (#c123) or jumping to a post.
  var target = location.hash && document.querySelector(location.hash);
  if (target) {
    var card = target.closest(".nw-card");
    var toggle = card && card.querySelector("[data-toggle-comments]");
    if (toggle && target.closest(".nw-thread")) setThread(toggle, true);
  }

  // Auto-grow comment boxes
  document.querySelectorAll(".nw-reply-form textarea").forEach(function (t) {
    t.addEventListener("input", function () { t.style.height = "auto"; t.style.height = t.scrollHeight + "px"; });
  });

  // Share: native share sheet on mobile, copy link elsewhere
  document.querySelectorAll("[data-share-url]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var url = btn.dataset.shareUrl;
      if (navigator.share) {
        navigator.share({ title: btn.dataset.shareTitle, url: url }).catch(function () {});
        return;
      }
      var label = btn.querySelector("span");
      var original = label.textContent;
      var done = function () { label.textContent = btn.dataset.copied; setTimeout(function () { label.textContent = original; }, 2000); };
      if (navigator.clipboard) navigator.clipboard.writeText(url).then(done);
      else { window.prompt("", url); }
    });
  });
})();
