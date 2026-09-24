(function () {
  "use strict";

  var root = document.getElementById("skills-challenge");
  if (!root) return;

  var $ = function (role) { return root.querySelector('[data-role="' + role + '"]'); };
  var ui = {
    timer: $("timer"), progress: $("progress"), counter: $("counter"), subskill: $("subskill"),
    warning: $("warning"), intro: $("intro"), begin: $("begin"), stage: $("stage"),
    prompt: $("prompt"), choices: $("choices"), text: $("text"), submit: $("submit")
  };

  var current = null, selectedChoice = null, tick = null, busy = false, running = false, finished = false;
  var telemetry, lastInputAt, lastFocusLossAt = 0;

  function resetTelemetry() {
    telemetry = { keystrokes: 0, intervals: [], max_single_insert: 0, paste_attempts: 0, drop_attempts: 0 };
    lastInputAt = null;
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": root.dataset.csrf },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return { error: "bad response" }; });
    });
  }

  function finish(url) {
    finished = true;
    running = false;
    clearInterval(tick);
    window.location.replace(url);
  }

  function showWarning(msg) {
    ui.warning.textContent = msg;
    ui.warning.hidden = false;
  }

  function handle(payload) {
    busy = false;
    if (!payload) return;
    if (payload.result_url && payload.state !== "active") return finish(payload.result_url);
    if (payload.item) render(payload.item);
  }

  function render(item) {
    current = item;
    selectedChoice = null;
    resetTelemetry();
    ui.prompt.textContent = item.prompt;
    ui.counter.textContent = item.index + " / " + item.total;
    ui.subskill.textContent = item.sub_skill;
    ui.progress.style.width = Math.round(((item.index - 1) / item.total) * 100) + "%";
    ui.choices.innerHTML = "";
    ui.text.value = "";

    if (item.kind === "mcq") {
      ui.text.hidden = true;
      item.choices.forEach(function (label, idx) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "skills-choice";
        btn.textContent = label;
        btn.addEventListener("click", function () {
          selectedChoice = idx;
          Array.prototype.forEach.call(ui.choices.children, function (c) { c.classList.remove("is-selected"); });
          btn.classList.add("is-selected");
          ui.submit.disabled = false;
        });
        ui.choices.appendChild(btn);
      });
      ui.submit.disabled = true;
    } else {
      ui.text.hidden = false;
      ui.submit.disabled = false;
      ui.text.focus();
    }
    startTimer(item.remaining_seconds);
  }

  function startTimer(seconds) {
    clearInterval(tick);
    var deadline = Date.now() + seconds * 1000;
    var update = function () {
      var left = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
      ui.timer.textContent = left;
      ui.timer.parentElement.classList.toggle("is-urgent", left <= 10);
      if (left <= 0) {
        clearInterval(tick);
        submit(true);
      }
    };
    update();
    tick = setInterval(update, 250);
  }

  function submit(auto) {
    if (busy || !current || finished) return;
    if (!auto && current.kind === "mcq" && selectedChoice === null) return;
    busy = true;
    clearInterval(tick);
    ui.submit.disabled = true;
    post(root.dataset.answerUrl, {
      item_id: current.item_id,
      choice_index: current.kind === "mcq" ? selectedChoice : null,
      answer_text: current.kind === "case" ? ui.text.value : "",
      typing_stats: current.kind === "case" ? telemetry : {}
    }).then(handle).catch(function () { busy = false; next(); });
  }

  function next() {
    busy = true;
    post(root.dataset.nextUrl).then(handle).catch(function () {
      busy = false;
      showWarning("Connection problem. Retrying…");
      setTimeout(next, 2000);
    });
  }

  function report(type) {
    if (!running) return;
    post(root.dataset.eventUrl, { type: type }).then(function (p) {
      if (!p) return;
      if (p.result_url && p.state !== "active") return finish(p.result_url);
      if (type === "tab_hidden" || type === "window_blur") {
        showWarning("Warning " + p.focus_warnings + " of " + p.max_focus_warnings +
          ": stay on this tab. The next time you leave, the session ends.");
      }
    });
  }

  function focusLost(type) {
    var now = Date.now();
    if (now - lastFocusLossAt < 1500) return; // blur + visibilitychange fire together on one switch
    lastFocusLossAt = now;
    report(type);
  }

  // --- Anti-cheating controls ---
  ["copy", "cut", "paste", "contextmenu", "drop"].forEach(function (evt) {
    document.addEventListener(evt, function (e) {
      if (!running) return;
      e.preventDefault();
      if (evt === "paste") telemetry.paste_attempts++;
      if (evt === "drop") telemetry.drop_attempts++;
      report(evt);
    }, true);
  });
  document.addEventListener("dragstart", function (e) { if (running) e.preventDefault(); }, true);
  document.addEventListener("selectstart", function (e) {
    if (running && e.target !== ui.text) e.preventDefault();
  }, true);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") focusLost("tab_hidden");
  });
  window.addEventListener("blur", function () { focusLost("window_blur"); });
  window.addEventListener("beforeunload", function (e) {
    if (running && !finished) { e.preventDefault(); e.returnValue = ""; }
  });

  // --- Typing telemetry (keystroke dynamics) ---
  ui.text.addEventListener("beforeinput", function (e) {
    if (e.inputType === "insertFromPaste" || e.inputType === "insertFromDrop" || e.inputType === "insertFromYank") {
      e.preventDefault();
      telemetry.paste_attempts++;
      report("paste");
    }
  });
  ui.text.addEventListener("input", function (e) {
    var t = e.inputType || "";
    if (t !== "insertText" && t !== "insertCompositionText" && t !== "insertReplacementText") return;
    var now = performance.now();
    telemetry.keystrokes++;
    if (lastInputAt !== null && telemetry.intervals.length < 2000) {
      telemetry.intervals.push(Math.round(now - lastInputAt));
    }
    lastInputAt = now;
    if (t === "insertText" && e.data) {
      telemetry.max_single_insert = Math.max(telemetry.max_single_insert, e.data.length);
    }
  });

  ui.submit.addEventListener("click", function () { submit(false); });
  ui.begin.addEventListener("click", function () {
    running = true;
    ui.intro.hidden = true;
    ui.stage.hidden = false;
    next();
  });
})();
