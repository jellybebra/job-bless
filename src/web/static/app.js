// Live task updates over SSE: append log lines, refresh the task panel.
(function () {
  const log = document.getElementById("log");
  const MAX_LINES = 400;
  let source = null;
  let reconnectTimer = null;
  let reloadTimer = null;
  let pageActive = true;

  function disconnect() {
    clearTimeout(reconnectTimer);
    clearTimeout(reloadTimer);
    reconnectTimer = null;
    reloadTimer = null;
    if (source) {
      source.onmessage = null;
      source.onerror = null;
      source.close();
      source = null;
    }
  }

  // Keep the console's size and visibility across navigation and task reloads.
  const consolePanel = document.getElementById("console-block");
  const consoleToggle = document.getElementById("console-toggle");
  const consoleResize = document.getElementById("console-resize");
  const CONSOLE_KEY = "job-bless.console";
  let consoleHeight = 200;
  let consoleCollapsed = false;

  try {
    const saved = JSON.parse(localStorage.getItem(CONSOLE_KEY));
    if (saved) {
      if (Number.isFinite(saved.height)) consoleHeight = saved.height;
      consoleCollapsed = saved.collapsed === true;
    }
  } catch (_) { /* Storage may be unavailable or contain an old value. */ }

  function saveConsole() {
    try {
      localStorage.setItem(CONSOLE_KEY, JSON.stringify({ height: consoleHeight, collapsed: consoleCollapsed }));
    } catch (_) { /* The controls still work without persistent storage. */ }
  }

  function renderConsole() {
    const maxHeight = Math.max(120, Math.floor(window.innerHeight * 0.75));
    consoleHeight = Math.round(Math.max(120, Math.min(consoleHeight, maxHeight)));
    consolePanel.style.height = consoleHeight + "px";
    consolePanel.classList.toggle("is-collapsed", consoleCollapsed);
    consoleToggle.textContent = consoleCollapsed ? "Развернуть" : "Свернуть";
    consoleToggle.setAttribute("aria-expanded", String(!consoleCollapsed));
    consoleResize.setAttribute("aria-valuemax", String(maxHeight));
    consoleResize.setAttribute("aria-valuenow", String(consoleHeight));
    document.body.style.setProperty("--console-space", consolePanel.getBoundingClientRect().height + "px");
  }

  if (consolePanel && consoleToggle && consoleResize) {
    renderConsole();
    consoleToggle.addEventListener("click", function () {
      consoleCollapsed = !consoleCollapsed;
      renderConsole();
      if (!consoleCollapsed) log.scrollTop = log.scrollHeight;
      saveConsole();
    });
    window.addEventListener("resize", renderConsole);
    new ResizeObserver(function () {
      document.body.style.setProperty("--console-space", consolePanel.getBoundingClientRect().height + "px");
    }).observe(consolePanel);

    let drag = null;
    consoleResize.addEventListener("pointerdown", function (event) {
      if (event.button !== 0 || drag) return;
      event.preventDefault();
      consoleResize.focus();
      consoleResize.setPointerCapture(event.pointerId);
      drag = { id: event.pointerId, y: event.clientY, height: consoleHeight };
      document.body.classList.add("console-resizing");
    });
    consoleResize.addEventListener("pointermove", function (event) {
      if (!drag || drag.id !== event.pointerId) return;
      consoleHeight = drag.height + drag.y - event.clientY;
      renderConsole();
    });
    function endResize(event) {
      if (!drag || drag.id !== event.pointerId) return;
      drag = null;
      document.body.classList.remove("console-resizing");
      if (consoleResize.hasPointerCapture(event.pointerId)) consoleResize.releasePointerCapture(event.pointerId);
      saveConsole();
    }
    consoleResize.addEventListener("pointerup", endResize);
    consoleResize.addEventListener("pointercancel", endResize);
    consoleResize.addEventListener("lostpointercapture", endResize);
    consoleResize.addEventListener("keydown", function (event) {
      if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      if (event.key === "Home") consoleHeight = 120;
      else if (event.key === "End") consoleHeight = window.innerHeight;
      else consoleHeight += event.key === "ArrowUp" ? 20 : -20;
      renderConsole();
      saveConsole();
    });
  }

  function appendLog(line) {
    if (!log) return;
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    log.textContent += (log.textContent ? "\n" : "") + line;

    const lines = log.textContent.split("\n");
    if (lines.length > MAX_LINES) {
      log.textContent = lines.slice(lines.length - MAX_LINES).join("\n");
    }
    if (atBottom) log.scrollTop = log.scrollHeight;
  }

  function refreshPanel() {
    if (window.htmx && document.getElementById("task-panel")) {
      window.htmx.ajax("GET", "/partials/status", { target: "#task-panel", swap: "outerHTML" });
    }
  }

  function updateProgress(task) {
    const bar = document.getElementById("task-bar");
    const counter = document.getElementById("task-counter");
    const message = document.getElementById("task-message");
    if (bar) bar.style.width = task.percent + "%";
    if (counter) counter.textContent = task.done + " / " + task.total;
    if (message && task.message) message.textContent = task.message;
  }

  function connect() {
    if (!pageActive || source) return;
    source = new EventSource("/actions/events");

    source.onmessage = function (event) {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (e) {
        return;
      }

      if (data.type === "snapshot") {
        // The server replays the running task's log right after connect —
        // drop what the page was rendered with so lines are not doubled.
        if (log) log.textContent = "";
        if (data.task) updateProgress(data.task);
      } else if (data.type === "log") {
        appendLog(data.line);
        if (data.task) updateProgress(data.task);
      } else if (data.type === "progress") {
        if (data.task) updateProgress(data.task);
        // The panel itself changes when a job starts waiting for confirmation.
        if (data.task && data.task.awaiting_confirmation) refreshPanel();
      } else if (data.type === "started" || data.type === "finished" || data.type === "stopping" || data.type === "dismissed") {
        refreshPanel();
        if (data.type === "finished") {
          // Numbers on the current page are stale once a job finishes.
          clearTimeout(reloadTimer);
          reloadTimer = setTimeout(function () { window.location.reload(); }, 1200);
        }
      }
    };

    source.onerror = function () {
      disconnect();
      if (pageActive) {
        reconnectTimer = setTimeout(function () {
          reconnectTimer = null;
          connect();
        }, 3000); // the server restarts during development
      }
    };
  }

  // Pages kept in the back/forward cache must release their HTTP connection.
  window.addEventListener("pagehide", function () {
    pageActive = false;
    disconnect();
  });
  window.addEventListener("pageshow", function (event) {
    pageActive = true;
    connect();
    if (event.persisted) refreshPanel();
  });

  connect();

  // Dialogs live outside the task panel so live updates preserve edits.
  const pipelineDialog = document.getElementById("pipeline-dialog");
  ["pipeline", "search", "apply"].forEach(function (kind) {
    const dialog = document.getElementById(kind + "-dialog");
    if (!dialog) return;
    const opener = "[data-open-" + kind + "]";
    document.addEventListener("click", function (event) {
      if (event.target.closest(opener)) {
        document.getElementById(kind + "-dialog-content").innerHTML = '<p class="muted" role="status">Загружаем настройки…</p>';
        if (!dialog.open) dialog.showModal();
      }
      if (event.target.closest("[data-close-" + kind + "]")) dialog.close();
      if (event.target === dialog) {
        const rect = dialog.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
      }
    });
    dialog?.addEventListener("close", function () {
      document.querySelector(opener)?.focus({ preventScroll: true });
      if (refreshAfterDialog) {
        refreshAfterDialog = false;
        window.location.reload();
      }
    });
    function requestError(event) {
      const element = event.detail.elt;
      if (element?.closest(opener)) {
        document.getElementById(kind + "-dialog-content").innerHTML = '<p class="error" role="alert">Не удалось загрузить настройки. Закройте окно и попробуйте ещё раз.</p>';
      } else if (element?.closest("#" + kind + "-dialog")) {
        const error = document.getElementById(kind + "-save-error");
        if (error) {
          error.textContent = "Не удалось сохранить. Проверьте соединение и попробуйте ещё раз.";
          error.hidden = false;
        }
      }
    }
    document.body.addEventListener("htmx:responseError", requestError);
    document.body.addEventListener("htmx:sendError", requestError);
  });
  document.body.addEventListener("pipelineSettingsSaved", function (event) {
    // Keep any settings form on this page in sync with the modal's changes.
    const matching = document.querySelector('.settings-form [name="matching.enabled"]');
    const applyMode = document.querySelector('.settings-form [name="apply.mode"]');
    if (matching) matching.checked = event.detail.matchingEnabled;
    if (applyMode) applyMode.value = event.detail.applyMode;
    pipelineDialog?.close();
    document.querySelector("[data-open-pipeline]")?.focus({ preventScroll: true });
  });
  document.body.addEventListener("applySettingsSaved", function () {
    document.getElementById("apply-dialog")?.close();
    document.querySelector("[data-open-apply]")?.focus({ preventScroll: true });
  });
  // Selection belongs to the current page; empty selection never starts a batch.
  const checkAll = document.getElementById("check-all");
  if (checkAll) {
    const boxes = Array.from(document.querySelectorAll(".row-check"));
    const submit = document.getElementById("apply-selected");
    const counter = document.getElementById("selection-count");
    function updateSelection() {
      const count = boxes.filter(box => box.checked).length;
      checkAll.checked = boxes.length > 0 && count === boxes.length;
      checkAll.indeterminate = count > 0 && count < boxes.length;
      if (counter) counter.textContent = "Выбрано: " + count;
      if (submit) submit.disabled = count === 0 || submit.dataset.unavailable === "1";
    }
    checkAll.addEventListener("change", function () {
      boxes.forEach(box => { box.checked = checkAll.checked; });
      updateSelection();
    });
    boxes.forEach(box => box.addEventListener("change", updateSelection));
    document.getElementById("vacancy-selection")?.addEventListener("submit", function (event) {
      if (!boxes.some(box => box.checked)) event.preventDefault();
    });
    updateSelection();
  }
})();
