const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('Google login polls cookies without opening storage-state windows during input', async () => {
  let polls = 0;
  let saves = 0;
  let closed = false;
  const page = {
    goto: async () => {}, isClosed: () => false,
    url: () => polls < 2 ? 'https://accounts.google.com/login' : 'https://aistudio.google.com/prompts/new_chat',
    locator: () => ({first: () => ({isVisible: async () => true})}),
  };
  const context = {
    newPage: async () => page,
    cookies: async () => {
      assert.equal(saves, 0, 'must not export state while the user is signing in');
      assert.equal(closed, false);
      polls++;
      return polls < 3 ? [] : [{domain: '.google.com', name: 'SID', value: 'test'}];
    },
    storageState: async () => {
      assert.equal(polls, 5, 'wait for three successful checks before exporting');
      saves++;
      return {cookies: [], origins: []};
    },
  };
  let finish;
  const completed = new Promise(resolve => { finish = resolve; });
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/aistudio/login.cjs'), 'utf8'), {
    require: name => {
      if (name === 'node:path') return path;
      if (name === 'node:fs') return {mkdirSync() {}, writeFileSync() {}, renameSync() {}};
      if (name.endsWith('ProxyUtils')) return {parseProxyFromEnv: () => null};
      return {firefox: {launch: async () => ({
        newContext: async () => context, isConnected: () => true,
        close: async () => { closed = true; },
      })}};
    },
    process: {env: {JOB_BLESS_AISTUDIO_APP: '/bundle'}, cwd: () => '/data', exit: finish},
    URL, console, setTimeout: callback => callback(),
  });
  assert.equal(await completed, 0);
  assert.equal(saves, 1);
  assert.equal(closed, true);
});
