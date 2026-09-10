const provider = document.body.dataset.provider;
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const status = document.getElementById('screen-status');
const pasteButton = document.getElementById('screen-paste');
let RFB, rfb, closing = false, pollTimer, ready = false, zoomed = false, connected = false;

async function post(action) {
  const response = await fetch(`/accounts/${provider}/${action}`, {
    method: 'POST', headers: {'X-CSRF-Token': csrf},
  });
  if (!response.ok || response.redirected) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Войдите в панель и откройте окно снова.');
  }
}

function connect() {
  connected = false;
  pasteButton.disabled = true;
  if (rfb) rfb.disconnect();
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const current = new RFB(document.getElementById('remote-screen'), `${protocol}//${location.host}/accounts/${provider}/ws`);
  rfb = current;
  current.clipViewport = true;
  current.scaleViewport = !zoomed;
  current.dragViewport = zoomed;
  current.resizeSession = false;
  current.addEventListener('connect', () => {
    if (current !== rfb) return;
    connected = true;
    pasteButton.disabled = false;
    status.textContent = 'Браузер открыт. Войдите в аккаунт.';
    current.focus();
  });
  current.addEventListener('disconnect', () => {
    if (current === rfb) { connected = false; pasteButton.disabled = true; }
    if (!closing && !ready && current === rfb) status.textContent = 'Экран отключён. Можно подключиться снова.';
  });
  current.addEventListener('securityfailure', () => { status.textContent = 'Не удалось открыть экран браузера.'; });
}

async function poll() {
  try {
    const response = await fetch(`/accounts/${provider}/status`);
    if (response.redirected || !response.ok) throw new Error('Сессия панели истекла. Войдите снова.');
    const data = await response.json();
    ready = data.state === 'ready';
    if (data.message) status.textContent = data.message;
    if (ready && provider === 'google' && rfb) rfb.disconnect();
  } catch (error) {
    status.textContent = error.message;
  }
  if (!closing) pollTimer = setTimeout(poll, 2000);
}

async function close() {
  if (closing) return;
  closing = true;
  clearTimeout(pollTimer);
  if (rfb) rfb.disconnect();
  try { await post('close'); } catch { /* The session may already have expired. */ }
  if (window.parent !== window) window.parent.postMessage({type: 'job-bless-screen-closed'}, location.origin);
  else location.assign('/actions');
}

document.getElementById('screen-close').addEventListener('click', close);
document.getElementById('screen-zoom').addEventListener('click', event => {
  zoomed = !zoomed;
  event.currentTarget.setAttribute('aria-pressed', String(zoomed));
  event.currentTarget.textContent = zoomed ? 'Весь экран' : 'Крупнее';
  if (rfb) { rfb.scaleViewport = !zoomed; rfb.dragViewport = zoomed; }
  if (zoomed) status.textContent = 'Перетаскивайте экран, чтобы увидеть нужную часть. Нажмите на поле для ввода.';
});
document.getElementById('screen-reconnect').addEventListener('click', async () => {
  try { await post('open'); connect(); } catch (error) { status.textContent = error.message; }
});

// Hidden input opens a phone keyboard. Send characters as VNC key events;
// never persist or transmit them through the application HTTP API.
function sendCharacters(text) {
  for (const character of text) {
    const point = character.codePointAt(0);
    rfb.sendKey(point < 256 ? point : 0x01000000 | point, '');
  }
}

// VNC's legacy clipboard loses Cyrillic. Unicode key events preserve login text
// and keep clipboard contents out of application endpoints and storage.
function pasteText(text) {
  if (!connected) throw new Error('Подключитесь к браузеру снова.');
  if (!text) throw new Error('В буфере нет текста.');
  if (/[\x00-\x1f\x7f-\x9f\u2028\u2029]/u.test(text)) {
    throw new Error('Для поля входа вставьте одну строку без переносов и табуляции.');
  }
  sendCharacters(text);
  rfb.focus();
  status.textContent = 'Текст вставлен в выбранное поле браузера.';
}

const pasteDialog = document.getElementById('screen-paste-dialog');
const pasteInput = document.getElementById('screen-paste-text');
const pasteError = document.getElementById('screen-paste-error');
pasteButton.addEventListener('click', async () => {
  let text;
  try {
    // Available on HTTPS/localhost; denied permissions and LAN HTTP use the form.
    text = await navigator.clipboard.readText();
  } catch {
    pasteError.textContent = '';
    pasteDialog.showModal();
    pasteInput.focus();
    return;
  }
  try { pasteText(text); } catch (error) { status.textContent = error.message; }
});
document.getElementById('screen-paste-form').addEventListener('submit', event => {
  event.preventDefault();
  try {
    pasteText(pasteInput.value);
    pasteDialog.close();
  } catch (error) { pasteError.textContent = error.message; }
});
document.getElementById('screen-paste-cancel').addEventListener('click', () => pasteDialog.close());
pasteDialog.addEventListener('close', () => {
  pasteInput.value = '';
  pasteError.textContent = '';
  rfb?.focus();
});

const keyboard = document.getElementById('remote-keyboard');
document.getElementById('screen-keyboard').addEventListener('click', () => keyboard.focus());
keyboard.addEventListener('input', () => {
  if (!rfb) return;
  sendCharacters(keyboard.value);
  keyboard.value = '';
});
keyboard.addEventListener('keydown', event => {
  if (event.key === 'Backspace' || event.key === 'Enter') {
    event.preventDefault();
    rfb?.sendKey(event.key === 'Enter' ? 0xff0d : 0xff08, event.code);
  }
});
window.addEventListener('pagehide', () => {
  if (!closing) {
    closing = true;
    clearTimeout(pollTimer);
    const data = new URLSearchParams({csrf_token: csrf});
    navigator.sendBeacon(`/accounts/${provider}/close`, data);
  }
});
window.addEventListener('message', event => {
  if (event.origin === location.origin && event.source === window.parent && event.data?.type === 'job-bless-screen-close') close();
});

try {
  const library = await import('/remote-static/core/rfb.js');
  RFB = library.default;
  await post('open'); connect(); poll();
}
catch (error) { status.textContent = error.message; }
