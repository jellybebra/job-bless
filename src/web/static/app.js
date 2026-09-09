// Live task updates over SSE: append log lines, refresh the task panel.
(function () {
  document.querySelectorAll('[data-exit-app]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const response = await fetch('/desktop/exit', {method: 'POST'});
      if (!response.ok) throw new Error('Exit failed');
      document.getElementById('mobile-navigation')?.close();
      document.querySelector('main').innerHTML = '<p role="status">job-bless завершает работу. Эту вкладку можно закрыть.</p>';
    } catch {
      button.disabled = false;
      button.textContent = 'Повторить завершение';
    }
  }));
  const mobileNavigation = document.getElementById('mobile-navigation');
  const mobileMenuToggle = document.querySelector('.mobile-menu-toggle');
  if (mobileNavigation && mobileMenuToggle) {
    const mobileViewport = window.matchMedia('(max-width: 760px)');
    mobileMenuToggle.addEventListener('click', () => {
      mobileNavigation.showModal();
      mobileMenuToggle.setAttribute('aria-expanded', 'true');
    });
    mobileNavigation.addEventListener('keydown', event => {
      if (event.key !== 'Tab') return;
      const controls = mobileNavigation.querySelectorAll('button, a[href]');
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
    mobileNavigation.addEventListener('click', event => {
      if (event.target.closest('.mobile-menu-close, a')) mobileNavigation.close();
      if (event.target === mobileNavigation) {
        const rect = mobileNavigation.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) mobileNavigation.close();
      }
    });
    mobileNavigation.addEventListener('close', () => {
      mobileMenuToggle.setAttribute('aria-expanded', 'false');
    });
    mobileViewport.addEventListener('change', event => {
      if (!event.matches && mobileNavigation.open) {
        mobileNavigation.close();
        document.querySelector('.desktop-navigation a.active')?.focus({ preventScroll: true });
      }
    });
  }
  const settingsForm = document.querySelector('.settings-form');
  if (settingsForm) {
    const saveBar = settingsForm.querySelector('.settings-save-bar');
    function fieldValues() {
      return new Map(Array.from(settingsForm.elements)
        .filter(field => field.name)
        .map(field => [field.name, field.type === 'checkbox' ? field.checked : field.value]));
    }
    const initialValues = fieldValues();
    let currentValues = new Map(initialValues);
    function warnBeforeLeaving(event) {
      event.preventDefault();
      event.returnValue = '';
    }
    function restoreField(field, value) {
      if (field.type === 'checkbox') {
        field.checked = value;
      } else {
        if (field.tagName === 'SELECT' && !Array.from(field.options).some(option => option.value === value)) {
          field.add(new Option(value || '—', value));
        }
        field.value = value;
      }
    }
    function updateSaveBar() {
      currentValues = fieldValues();
      const changed = Array.from(initialValues).some(([name, value]) => currentValues.get(name) !== value);
      saveBar.classList.toggle('is-visible', changed);
      saveBar.inert = !changed;
      saveBar.setAttribute('aria-hidden', String(!changed));
      if (changed) window.addEventListener('beforeunload', warnBeforeLeaving);
      else window.removeEventListener('beforeunload', warnBeforeLeaving);
    }
    updateSaveBar();
    saveBar.hidden = false;
    settingsForm.addEventListener('input', updateSaveBar);
    settingsForm.addEventListener('change', updateSaveBar);
    settingsForm.addEventListener('reset', () => requestAnimationFrame(updateSaveBar));
    settingsForm.querySelector('[data-discard-settings]').addEventListener('click', () => {
      Array.from(settingsForm.elements).forEach(field => {
        if (initialValues.has(field.name)) restoreField(field, initialValues.get(field.name));
      });
      // Move focus out of the bar before it becomes inert and slides away.
      (settingsForm.querySelector('.settings-section[open] > summary') || settingsForm.querySelector('summary'))?.focus({ preventScroll: true });
      updateSaveBar();
    });
    // A valid save is intentional navigation; invalid forms keep the guard.
    settingsForm.addEventListener('submit', () => window.removeEventListener('beforeunload', warnBeforeLeaving));
    window.addEventListener('pageshow', updateSaveBar);
    settingsForm.addEventListener('htmx:afterSwap', () => {
      // Loading or refreshing the model list must preserve the user's value,
      // including edits made while the request was in flight.
      settingsForm.querySelectorAll('.model-field [name]').forEach(field => {
        if (!currentValues.has(field.name)) return;
        restoreField(field, currentValues.get(field.name));
      });
      updateSaveBar();
    });
    function revealSetting(element) {
      for (let parent = element?.parentElement; parent && parent !== settingsForm; parent = parent.parentElement) {
        if (parent.tagName === 'DETAILS') parent.open = true;
      }
    }
    function revealSettingsHash() {
      let id;
      try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; }
      const target = document.getElementById(id);
      if (!target || !settingsForm.contains(target)) return;
      revealSetting(target);
      requestAnimationFrame(() => target.scrollIntoView({ block: 'center' }));
    }
    // Closed sections still submit their controls. Reveal invalid fields so
    // the browser can focus them and explain what needs correcting.
    settingsForm.addEventListener('invalid', event => revealSetting(event.target), true);
    window.addEventListener('hashchange', revealSettingsHash);
    revealSettingsHash();
  }
  const log = document.getElementById("log");
  const MAX_LINES = 400;
  let source = null;
  let reconnectTimer = null;
  let reloadTimer = null;
  let refreshAfterDialog = false;
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
          reloadTimer = setTimeout(function () {
            if (document.querySelector('.pipeline-dialog[open]')) refreshAfterDialog = true;
            else window.location.reload();
          }, 1200);
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

  document.body.addEventListener('aistudioChanged', refreshPanel);
  // Dialogs live outside the task panel so live updates preserve edits.
  ["pipeline", "search", "apply", "score", "profile", "activity", "resume_touch", "llm"].forEach(function (kind) {
    const dialog = document.getElementById(kind + "-dialog");
    if (!dialog) return;
    const opener = "[data-open-" + kind + "]";
    dialog.addEventListener('invalid', function (event) {
      for (let parent = event.target.parentElement; parent && parent !== dialog; parent = parent.parentElement) {
        if (parent.tagName === 'DETAILS') parent.open = true;
      }
    }, true);
    const pendingModels = new Map();
    dialog.addEventListener('htmx:beforeSwap', function (event) {
      const target = event.detail.target;
      if (!target?.matches('.model-field')) return;
      const field = target.querySelector('[name]');
      if (field) pendingModels.set(field.name, field.value);
    });
    dialog.addEventListener('htmx:afterSwap', function () {
      dialog.querySelectorAll('.model-field [name]').forEach(field => {
        if (!pendingModels.has(field.name)) return;
        const value = pendingModels.get(field.name);
        if (field.tagName === 'SELECT' && !Array.from(field.options).some(option => option.value === value)) {
          field.add(new Option(value || '—', value));
        }
        field.value = value;
        pendingModels.delete(field.name);
      });
    });
    document.body.addEventListener(kind + "SettingsSaved", function () {
      dialog.close();
    });
    document.addEventListener("click", function (event) {
      if (event.target.closest(opener)) {
        pendingModels.clear();
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
  // Selection belongs to the current page; empty selection never starts a batch.
  function setupVacancySelection() {
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
  }
  document.querySelectorAll('time[data-local-time]').forEach(function (element) {
    const date = new Date(element.dateTime);
    if (!Number.isNaN(date.getTime())) {
      element.textContent = date.toLocaleString('ru-RU', {
        day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit'
      });
    }
  });
  setupVacancySelection();
  document.body.addEventListener("htmx:afterSwap", function (event) {
    if (event.target.id === "vacancies-page") setupVacancySelection();
  });
  document.body.addEventListener("htmx:historyRestore", setupVacancySelection);
})();
