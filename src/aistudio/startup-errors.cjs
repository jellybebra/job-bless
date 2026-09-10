// Export only a known error code; never retain Google response bodies or URLs.
function observeStartupErrors(context, report) {
  context.on('response', async response => {
    try {
      const url = new URL(response.url());
      if (response.status() !== 403 || url.hostname !== 'alkalimakersuite-pa.clients6.google.com') return;
      const text = await response.text();
      if (/region not supported|unsupported (?:country|region)|user location is not supported/i.test(text)) {
        report('region_unsupported');
      }
    } catch { /* Navigation or shutdown can discard a response body. */ }
  });
}

module.exports = {observeStartupErrors};
