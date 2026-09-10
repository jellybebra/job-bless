// Small integration layer around the pinned, unmodified AIStudioToAPI release.
// Runtime data lives in cwd; executable code/dependencies live in the bundle.
const fs = require('node:fs');
const path = require('node:path');
const {configurePort, enableMainWorld} = require('./port-routing.cjs');
const {observeStartupErrors} = require('./startup-errors.cjs');
enableMainWorld(process.env);
const appRoot = process.env.JOB_BLESS_AISTUDIO_APP;
const ProxyServerSystem = require(path.join(appRoot, 'src/core/ProxyServerSystem'));
const server = new ProxyServerSystem();
const wsPort = Number(process.env.JOB_BLESS_AISTUDIO_WS_PORT);
if (!Number.isInteger(wsPort) || wsPort < 1 || wsPort > 65535) throw new Error('Invalid private WebSocket port');
server.config.wsPort = wsPort;

// The upstream Build App connects to localhost:9998. Redirect only this local
// socket inside our own browser contexts, so an existing proxy can keep 9998.
const ensureBrowser = server.browserManager._ensureBrowser.bind(server.browserManager);
server.browserManager._ensureBrowser = async function () {
  await ensureBrowser();
  const browser = this.browser;
  if (browser._jobBlessPortConfigured) return;
  browser._jobBlessPortConfigured = true;
  const newContext = browser.newContext.bind(browser);
  browser.newContext = async (...args) => {
    const context = await newContext(...args);
    observeStartupErrors(context, code => {
      startupError = true;
      startupErrorCode = code;
      writeStatus();
    });
    await configurePort(context, wsPort);
    return context;
  };
};

let startupError = false;
let startupErrorCode = '';
function writeStatus() {
  const index = server.browserManager.currentAuthIndex;
  const connection = server.connectionRegistry.getConnectionByAuth(index, false);
  const status = {
    run_id: process.env.JOB_BLESS_AISTUDIO_RUN_ID, pid: process.pid,
    connected: !!connection && connection.readyState === 1 && !!server.browserManager.browser,
    error: startupError,
    error_code: startupErrorCode,
  };
  const target = path.join(process.cwd(), 'bridge-status.json');
  try {
    fs.writeFileSync(target + '.tmp', JSON.stringify(status));
    fs.renameSync(target + '.tmp', target);
  } catch { /* A reader can hold a file briefly on Windows; retry next tick. */ }
  return status;
}
const timer = setInterval(writeStatus, 1000);
let closing = false;
async function shutdown() {
  if (closing) return;
  closing = true;
  clearInterval(timer);
  const deadline = setTimeout(() => process.exit(0), 3000);
  deadline.unref();
  try { await server.shutdown(); } finally { process.exit(0); }
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
process.stdin.on('end', shutdown);
process.stdin.resume();
server.start(0).then(() => {
  // Upstream resolves start() even when every browser context failed.
  // A listening HTTP server alone must not leave the UI in "starting".
  if (!writeStatus().connected) {
    startupError = true;
    writeStatus();
  }
}).catch(error => {
  startupError = true;
  writeStatus();
  console.error('AIStudioToAPI startup failed:', error.message);
  shutdown();
});
