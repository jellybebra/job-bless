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
    if (window.htmx) {
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
      } else if (data.type === "started" || data.type === "finished" || data.type === "stopping") {
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

  // The task panel is re-rendered on every event, so remember what the user
  // picked in the runner and restore it after each swap.
  const RUNNER_KEY = "job-bless.runner-kind";

  function restoreRunnerChoice() {
    const saved = localStorage.getItem(RUNNER_KEY);
    if (!saved) return;
    const option = document.querySelector('.runner input[name="kind"][value="' + saved + '"]');
    if (option) option.checked = true;
  }

  document.addEventListener("change", function (event) {
    if (event.target.name === "kind") {
      localStorage.setItem(RUNNER_KEY, event.target.value);
    }
  });

  document.body.addEventListener("htmx:afterSwap", restoreRunnerChoice);
  restoreRunnerChoice();

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
