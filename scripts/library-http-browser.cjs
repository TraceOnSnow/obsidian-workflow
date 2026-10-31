// Optional real-browser consent check. Credentials arrive on stdin only;
// stdout is a private pipe to verify-library-http.py, never a terminal.
const fs = require('node:fs');
const http = require('node:http');

(async () => {
  if (process.stdin.isTTY || process.stdout.isTTY) throw new Error('Private pipes required');
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const { chromium } = require(input.module_path);
  const target = new URL(input.callback);
  if (target.protocol !== 'http:' || target.hostname !== '127.0.0.1' || !target.port)
    throw new Error('Native numeric loopback callback required');
  let resolveCallback;
  const callback = new Promise(resolve => { resolveCallback = resolve; });
  const receiver = http.createServer((request, response) => {
    if (request.method !== 'GET' || new URL(request.url, target.origin).pathname !== target.pathname) {
      response.writeHead(404).end(); return;
    }
    resolveCallback({location: target.origin + request.url, referer_absent: !request.headers.referer});
    response.writeHead(200, {'Content-Type': 'text/plain', 'Cache-Control': 'no-store'})
      .end('Browser consent verified');
  });
  await new Promise((resolve, reject) => {
    receiver.once('error', reject);
    receiver.listen(Number(target.port), '127.0.0.1', resolve);
  });
  let browser;
  try {
    browser = await chromium.launch({executablePath: input.executable,
      headless: true, args: ['--no-sandbox']});
    const page = await browser.newPage();
    const origin = new URL(input.login_url).origin;
    let postOrigin;
    page.on('request', request => {
      if (request.method() === 'POST' && request.url() === origin + '/library-auth/login')
        postOrigin = request.headers().origin;
    });
    const response = await page.goto(input.login_url);
    if (response.status() !== 200) throw new Error('Login page failed');
    const policies = (await response.headersArray()).filter(h => h.name.toLowerCase() === 'referrer-policy')
      .flatMap(h => h.value.split(',').map(v => v.trim()));
    if (!policies.length || policies.some(v => v !== 'same-origin')) throw new Error('Conflicting policy');
    await page.locator('input[name=password]').fill(input.password);
    const posted = page.waitForResponse(r => r.request().method() === 'POST' &&
      r.url() === origin + '/library-auth/login');
    await page.locator('button[value=allow]').click({noWaitAfter: true});
    if ((await posted).status() !== 303) throw new Error('Consent failed');
    let timer;
    const result = await Promise.race([callback, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('Callback timeout')), 10000);
      timer.unref();
    })]).finally(() => clearTimeout(timer));
    if (postOrigin !== origin || !result.referer_absent) throw new Error('Browser boundary failed');
    process.stdout.write(JSON.stringify({...result, canonical_post_origin: true,
      consistent_referrer_policy: true}));
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => receiver.close(resolve));
  }
})().catch(() => {
  // Do not print browser exceptions: they may include temporary OAuth URLs.
  process.stderr.write('Browser consent verification failed; no credentials printed.\n');
  process.exitCode = 1;
});
