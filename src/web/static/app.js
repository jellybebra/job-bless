// Live task updates over SSE: append log lines, refresh the task panel.
(function () {
  const log = document.getElementById("log");
  const MAX_LINES = 400;
  let refreshAfterDialog = false;

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
    const source = new EventSource("/actions/events");

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
          setTimeout(function () {
            if (document.querySelector("dialog[open]")) refreshAfterDialog = true;
            else window.location.reload();
          }, 1200);
        }
      }
    };

    source.onerror = function () {
      source.close();
      setTimeout(connect, 3000); // the server restarts during development
    };
  }

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
  // "select all" checkbox on the vacancies page
  const checkAll = document.getElementById("check-all");
  if (checkAll) {
    checkAll.addEventListener("change", function () {
      document.querySelectorAll(".row-check").forEach(function (box) {
        box.checked = checkAll.checked;
      });
    });
  }
})();
