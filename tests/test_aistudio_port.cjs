const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {portScript, enableMainWorld} = require('../src/aistudio/port-routing.cjs');

test('redirect only the upstream loopback WebSocket and preserve protocol arguments', () => {
  class WebSocket {
    static OPEN = 1;
    constructor(...args) { this.args = args; }
  }
  const scope = {window: {WebSocket}, URL, location: {href: 'https://preview.run.app/'}};
  vm.runInNewContext(portScript(58145).slice(3), scope);
  const Patched = scope.window.WebSocket;
  assert.equal(Patched.OPEN, 1);
  assert.deepEqual(new Patched('ws://localhost:9998/?authIndex=0', ['test']).args,
    ['ws://127.0.0.1:58145/?authIndex=0', ['test']]);
  assert.deepEqual(new Patched('ws://127.0.0.1:9998').args, ['ws://127.0.0.1:58145/']);
  for (const address of ['wss://localhost:9998/', 'ws://example.com:9998/', 'ws://localhost:9999/']) {
    assert.deepEqual(new Patched(address).args, [address]);
  }
  vm.runInNewContext(portScript(58145).slice(3), scope);
  assert.equal(scope.window.WebSocket, Patched);
  assert.throws(() => portScript(0));
});

test('enable explicit main-world evaluation while preserving Camoufox configuration', () => {
  const env = {CAMOU_CONFIG_1: '{"locale:language":', CAMOU_CONFIG_2: '"en"}', PATH: 'keep'};
  enableMainWorld(env);
  assert.deepEqual(JSON.parse(env.CAMOU_CONFIG_1), {'locale:language': 'en', allowMainWorld: true});
  assert.equal(env.CAMOU_CONFIG_2, undefined);
  assert.equal(env.PATH, 'keep');
});
