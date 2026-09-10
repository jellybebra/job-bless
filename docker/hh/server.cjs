// Use the exact Playwright version pinned for the Python client.
const fs = require('node:fs');
const {firefox} = require(process.argv[2]);
const options = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
fs.unlinkSync(process.argv[3]);

async function main() {
  const server = await firefox.launchServer({...options, host: '0.0.0.0', port: 3000, wsPath: 'hh'});
  let stopping = false;
  const stop = async () => {
    if (stopping) return;
    stopping = true;
    const deadline = setTimeout(() => process.exit(1), 15000);
    deadline.unref();
    await server.close();
    process.exit(0);
  };
  process.on('SIGTERM', stop);
  process.on('SIGINT', stop);
  server.on('close', () => { if (!stopping) process.exit(1); });
  console.log('HH Camoufox is ready on the private Playwright endpoint.');
}
main().catch(error => { console.error(error); process.exit(1); });
