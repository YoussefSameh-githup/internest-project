/* Internest async tasks: submit a heavy request with `Prefer: respond-async`, then poll its status URL.
 *
 *   InternestAsync.fetchJSON(url, {method: "POST", body, headers}) -> Promise<{ok, status, data}>
 *     Same shape whether the server answered synchronously or with 202 + polling, so callers stay simple.
 *   <form data-async-task data-async-busy="Preparing…"> ... </form>
 *     Submitted in the background; on success the browser follows result.redirect, errors are shown inline.
 *
 * Polling backs off from 400 ms to 2 s, stops after `timeoutMs` (default 120 s) and can be cancelled with an
 * AbortSignal. A busy form ignores repeated submits (double-submit protection).
 * A 429 (too many tasks for this user) or 503 (queue full) refusal resolves to {ok: false, status, data, retryAfter,
 * throttled: true}; forms show data.error and keep the button disabled until Retry-After has passed.
 */
(function () {
  "use strict";

  function sleep(ms, signal) {
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(resolve, ms);
      if (signal) signal.addEventListener("abort", function () { clearTimeout(timer); reject(new DOMException("Aborted", "AbortError")); }, {once: true});
    });
  }

  function readJSON(response) {
    return response.json().catch(function () { return {}; });
  }

  function poll(statusUrl, options) {
    options = options || {};
    var started = Date.now(), delay = 400, timeoutMs = options.timeoutMs || 120000;
    function step() {
      if (Date.now() - started > timeoutMs) {
        return Promise.resolve({ok: false, status: 504, data: {error: options.timeoutMessage || "timeout"}});
      }
      return sleep(delay, options.signal).then(function () {
        delay = Math.min(2000, Math.round(delay * 1.5));
        return fetch(statusUrl, {credentials: "same-origin", headers: {"Accept": "application/json"}, signal: options.signal});
      }).then(function (r) { return readJSON(r).then(function (d) { return {http: r.status, d: d}; }); })
        .then(function (res) { return settle(res.d) || step(); });
    }
    return step();
  }

  // A finished task → {ok, status, data}; an unfinished one → null (keep polling).
  function settle(task) {
    if (!task || !task.status || task.status === "queued" || task.status === "running") return null;
    var status = task.http_status || (task.status === "done" ? 200 : 500);
    return {ok: task.status === "done" && status < 400, status: status, data: task.result || {error: task.error || "failed"}};
  }

  // Seconds from a 429 / 503 refusal (Retry-After header, else the body's retry_after); 0 when absent.
  function retryAfter(response, data) {
    var value = parseInt(response.headers.get("Retry-After") || (data && data.retry_after) || "0", 10);
    return isNaN(value) ? 0 : Math.max(0, value);
  }

  function fetchJSON(url, init) {
    init = Object.assign({method: "POST", credentials: "same-origin"}, init || {});
    init.headers = Object.assign({"Prefer": "respond-async", "Accept": "application/json"}, init.headers || {});
    return fetch(url, init).then(function (r) {
      return readJSON(r).then(function (d) {
        if (r.status === 429 || r.status === 503) {
          // Too many tasks for this user / the queue is full: the server says when to try again.
          return {ok: false, status: r.status, data: d, retryAfter: retryAfter(r, d), throttled: true};
        }
        if (r.status !== 202 || !d.status_url) return {ok: r.ok, status: r.status, data: d};
        return settle(d) || poll(d.status_url, {signal: init.signal, timeoutMs: init.timeoutMs, timeoutMessage: init.timeoutMessage});
      });
    });
  }

  function csrfToken(form) {
    var field = form.querySelector("input[name=csrfmiddlewaretoken]");
    return field ? field.value : "";
  }

  function showError(form, message) {
    var box = form.querySelector("[data-async-error]");
    if (!box) {
      box = document.createElement("p");
      box.setAttribute("data-async-error", "");
      box.setAttribute("role", "alert");
      box.className = "ia-error";
      form.appendChild(box);
    }
    box.textContent = message;
  }

  function enhance(form) {
    form.addEventListener("submit", function (e) {
      if (!window.fetch || !window.Promise) return;  // very old browsers: normal form post
      e.preventDefault();
      if (form.dataset.asyncBusy === "1") return;
      form.dataset.asyncBusy = "1";
      var button = form.querySelector("[type=submit]"), label = button ? button.innerHTML : "";
      if (button) {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
        button.innerHTML = '<span class="ia-spinner" aria-hidden="true"></span> ' + (form.getAttribute("data-async-busy") || label);
      }
      var cooldown = 0;
      fetchJSON(form.action, {body: new FormData(form), headers: {"X-CSRFToken": csrfToken(form)},
                              timeoutMessage: form.getAttribute("data-async-timeout") || ""})
        .then(function (res) {
          if (res.throttled) {
            // 429 / 503: show why, keep the button disabled until Retry-After (max 60 s); never re-post automatically.
            showError(form, (res.data && res.data.error) || form.getAttribute("data-async-busy-error") || "Please try again in a moment.");
            cooldown = Math.min(res.retryAfter || 5, 60);
            throw new Error("done");
          }
          var redirect = res.data && res.data.redirect;
          if (res.ok && redirect) { window.location.assign(redirect); return; }
          if (res.data && res.data.error) showError(form, res.data.error);
          if (redirect) { setTimeout(function () { window.location.assign(redirect); }, 2500); return; }
          if (!res.data || !res.data.error) form.submit();  // unexpected answer: fall back to the classic post
          throw new Error("done");
        })
        .catch(function (err) {
          if (err && err.message === "done") return;
          form.submit();  // network trouble: fall back to the classic post, which still works
        })
        .finally(function () {
          function release() {
            form.dataset.asyncBusy = "";
            if (button) { button.disabled = false; button.removeAttribute("aria-busy"); button.innerHTML = label; }
          }
          if (!cooldown) { release(); return; }
          if (button) { button.removeAttribute("aria-busy"); button.innerHTML = label; }
          setTimeout(release, cooldown * 1000);
        });
    });
  }

  window.InternestAsync = {fetchJSON: fetchJSON, poll: poll};
  document.querySelectorAll("form[data-async-task]").forEach(enhance);
})();
