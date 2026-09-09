// Manual Google login in the bundled browser. Passwords and verification codes
// are entered on Google's page; job-bless never asks for them in its UI.
const fs = require('node:fs');
const path = require('node:path');
const appRoot = process.env.JOB_BLESS_AISTUDIO_APP;
const {firefox} = require(path.join(appRoot, 'node_modules/playwright'));
const {parseProxyFromEnv} = require(path.join(appRoot, 'src/utils/ProxyUtils'));

async function run() {
  const proxy = parseProxyFromEnv();
  const browser = await firefox.launch({
    executablePath: process.env.CAMOUFOX_EXECUTABLE_PATH, headless: false,
    firefoxUserPrefs: {'network.trr.mode': 5},
    ...(proxy ? {proxy} : {}),
  });
  try {
    const context = await browser.newContext(proxy ? {proxy} : {});
    const page = await context.newPage();
    await page.goto('https://aistudio.google.com/u/0/prompts/new_chat', {timeout: 60000, waitUntil: 'domcontentloaded'});
    const deadline = Date.now() + 600000;
    let stableChecks = 0;
    while (Date.now() < deadline && browser.isConnected() && !page.isClosed()) {
      try {
        const host = new URL(page.url()).hostname;
        // storageState() may open a temporary window for previously visited
        // origins. Poll cookies only so the login form keeps keyboard focus.
        const cookies = await context.cookies();
        const signedIn = cookies.some(cookie =>
          /(^|\.)google\.com$/.test(cookie.domain) && ['SID', '__Secure-1PSID', '__Secure-3PSID'].includes(cookie.name) && cookie.value);
        // A Google cookie alone is insufficient: wait until the user has
        // reached the AI Studio prompt UI and completed any consent screen.
        const editor = page.locator('textarea, [contenteditable="true"], ms-prompt-input').first();
        const usable = host === 'aistudio.google.com' && signedIn && await editor.isVisible().catch(() => false);
        stableChecks = usable ? stableChecks + 1 : 0;
        if (stableChecks >= 3) {
          const state = await context.storageState();
          const folder = path.join(process.cwd(), 'configs/auth');
          fs.mkdirSync(folder, {recursive: true});
          const target = path.join(folder, 'auth-0.json');
          fs.writeFileSync(target + '.tmp', JSON.stringify(state));
          fs.renameSync(target + '.tmp', target);
          return;
        }
      } catch { /* Navigation replaces the page context during login. */ }
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    throw new Error('Google login was closed or timed out');
  } finally {
    await browser.close().catch(() => {});
  }
}
run().then(() => process.exit(0)).catch(error => {
  console.error('Google login did not complete:', error.message);
  process.exit(1);
});
