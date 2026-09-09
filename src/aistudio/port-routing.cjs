// Camoufox isolates ordinary evaluation from the page's WebSocket global.
// Its explicit main-world evaluation is required for this local endpoint shim.
function portScript(port) {
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid private WebSocket port');
  return `mw:(() => {
    if (window.WebSocket.__jobBlessPort === ${port}) return;
    const NativeWebSocket = window.WebSocket;
    class LocalWebSocket extends NativeWebSocket {
      constructor(address, protocols) {
        let target = address;
        try {
          const url = new URL(address, location.href);
          if (url.protocol === 'ws:' && ['127.0.0.1', 'localhost'].includes(url.hostname) && url.port === '9998') {
            url.hostname = '127.0.0.1';
            url.port = '${port}';
            target = url.href;
          }
        } catch {}
        if (protocols === undefined) super(target);
        else super(target, protocols);
      }
    }
    LocalWebSocket.__jobBlessPort = ${port};
    window.WebSocket = LocalWebSocket;
  })()`;
}

function enableMainWorld(env) {
  let config = '';
  for (let i = 1; env[`CAMOU_CONFIG_${i}`]; i++) {
    config += env[`CAMOU_CONFIG_${i}`];
    delete env[`CAMOU_CONFIG_${i}`];
  }
  const value = JSON.stringify({...JSON.parse(config || '{}'), allowMainWorld: true});
  for (let i = 0; i < value.length; i += 30000) env[`CAMOU_CONFIG_${1 + i / 30000}`] = value.slice(i, i + 30000);
}

async function configurePort(context, port) {
  const script = portScript(port);
  const patch = async frame => {
    try {
      if (!new URL(frame.url()).hostname.endsWith('.run.app')) return;
      await frame.evaluate(script);
    } catch { /* Navigation can replace the execution context; retry below. */ }
  };
  context.on('page', page => {
    page.on('framenavigated', frame => { patch(frame); });
  });
  const timer = setInterval(() => {
    for (const page of context.pages()) for (const frame of page.frames()) patch(frame);
  }, 1000);
  context.on('close', () => clearInterval(timer));
}

module.exports = {configurePort, portScript, enableMainWorld};
