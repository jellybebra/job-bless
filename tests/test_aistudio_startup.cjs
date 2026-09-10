const {test} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {observeStartupErrors} = require('../src/aistudio/startup-errors.cjs');

test('report Google region denial without exporting response contents or unrelated errors', async () => {
  const context = new EventEmitter();
  const codes = [];
  observeStartupErrors(context, code => codes.push(code));
  const listener = context.listeners('response')[0];
  const response = (host, status, text) => ({url: () => `https://${host}/rpc`, status: () => status, text: async () => text});
  await listener(response('alkalimakersuite-pa.clients6.google.com', 403, '{"error":{"message":"Region not supported"},"private":"do-not-export"}'));
  await listener(response('example.com', 403, 'Region not supported'));
  await listener(response('alkalimakersuite-pa.clients6.google.com', 403, 'Unrelated error'));
  await listener(response('alkalimakersuite-pa.clients6.google.com', 200, 'Region not supported'));
  assert.deepEqual(codes, ['region_unsupported']);
});

for (const connected of [false, true]) {
  test(`bridge checks browser connection after upstream start resolves: connected=${connected}`, async () => {
    const statuses = [];
    class Server {
      constructor() {
        this.config = {};
        this.browserManager = {currentAuthIndex: connected ? 0 : -1, browser: connected ? {} : null, _ensureBrowser: async () => {}};
        this.connectionRegistry = {getConnectionByAuth: () => connected ? {readyState: 1} : null};
      }
      async start() {} // Upstream also resolves when every context fails.
    }
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/aistudio/bridge.cjs'), 'utf8'), {
      require: name => {
        if (name === 'node:fs') return {writeFileSync: (_, value) => statuses.push(JSON.parse(value)), renameSync() {}};
        if (name === 'node:path') return path;
        if (name === './port-routing.cjs') return {configurePort() {}, enableMainWorld() {}};
        if (name === './startup-errors.cjs') return {observeStartupErrors() {}};
        return Server;
      },
      process: {env: {JOB_BLESS_AISTUDIO_APP:'/bundle',JOB_BLESS_AISTUDIO_WS_PORT:'58181',JOB_BLESS_AISTUDIO_RUN_ID:'test'},
        pid: 123, cwd: () => '/data', on() {}, stdin: {on() {}, resume() {}}},
      setInterval() {}, console,
    });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(statuses.at(-1).connected, connected);
    assert.equal(statuses.at(-1).error, !connected);
  });
}
