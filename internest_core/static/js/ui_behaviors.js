/* Declarative behaviours that replace inline event handlers (onclick=, onsubmit=, onchange=).
 *
 * The Content-Security-Policy has no 'unsafe-inline' for scripts, so the browser refuses inline handlers. Templates
 * mark elements with data-* attributes instead, and these delegated listeners (one per event type, on document)
 * give them their behaviour, including elements added to the page later.
 *
 *   <form data-confirm="Delete this post?">…</form>   ask before submitting; Cancel stops the submission
 *   <button type="button" data-print>Print</button>     open the browser's print dialog
 *   <select data-autosubmit>…</select>                  submit the select's form when its value changes
 */
(function () {
    "use strict";

    // Capture phase: runs before any other submit listener, so a cancelled confirmation stops them all.
    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-confirm")) return;
        if (!window.confirm(form.getAttribute("data-confirm"))) {
            event.preventDefault();
            event.stopImmediatePropagation();
        }
    }, true);

    document.addEventListener("click", function (event) {
        var trigger = event.target instanceof Element ? event.target.closest("[data-print]") : null;
        if (!trigger) return;
        event.preventDefault();
        window.print();
    });

    document.addEventListener("change", function (event) {
        var field = event.target;
        if (!(field instanceof Element) || !field.hasAttribute("data-autosubmit") || !field.form) return;
        field.form.submit();  // the same as the former onchange="this.form.submit()"
    });
})();
