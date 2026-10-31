import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { Readable } from 'node:stream';
import test from 'node:test';
import { createApiFetch } from '../scripts/obsidian-api-transport.mjs';

function fixture({ status = 200, body = '{"ok":true}', error, interrupted = false } = {}) {
  const calls = [];
  const request = (url, options, receive) => {
    const req = new EventEmitter();
    req.end = (sent) => {
      calls.push({ url: url.href, options, body: sent });
      queueMicrotask(() => {
        if (error) return req.emit('error', error);
        const res = Readable.from(interrupted ? [] : [body]);
        Object.assign(res, { statusCode: status, statusMessage: 'fixture', rawHeaders: ['Content-Type', 'application/json'] });
        receive(res);
        if (interrupted) res.emit('aborted');
      });
    };
    return req;
  };
  return { calls, request };
}

test('API transport preserves authentication payload, Origin, UA and normal JSON responses', async () => {
  const upstream = fixture({ body: '{"token":"fixture-only-token"}' });
  const fetch = createApiFetch(() => assert.fail('must use HTTPS'), upstream.request);
  const body = JSON.stringify({ email: 'fixture@example.test', password: 'fixture-only', mfa: '123456' });
  const response = await fetch('https://api.obsidian.md/user/signin', { method: 'POST', body,
    headers: { Origin: 'https://obsidian.md', 'User-Agent': 'obsidian-headless/0.0.14', 'Content-Type': 'application/json' } });
  assert.equal(response.ok, true);
  assert.equal(response.url, 'https://api.obsidian.md/user/signin');
  assert.deepEqual(await response.json(), { token: 'fixture-only-token' });
  assert.equal(upstream.calls[0].body, body);
  assert.equal(upstream.calls[0].options.headers.origin, 'https://obsidian.md');
  assert.equal(upstream.calls[0].options.headers['user-agent'], 'obsidian-headless/0.0.14');
  assert.equal(upstream.calls[0].options.headers['content-length'], String(Buffer.byteLength(body)));
});

test('preflight, HTTP failures and API-level errors retain official CLI handling', async () => {
  for (const [status, body] of [[204, ''], [504, 'gateway timeout'], [200, '{"error":"MFA required"}']]) {
    const upstream = fixture({ status, body });
    const response = await createApiFetch(() => assert.fail(), upstream.request)('https://api.obsidian.md/user/signin', { method: 'OPTIONS' });
    assert.equal(response.status, status);
    assert.equal(await response.text(), body);
    assert.equal(upstream.calls.length, 1, 'no automatic retries');
  }
});

test('unrelated hosts, insecure URLs, GET and unfamiliar bodies keep native fetch', async () => {
  const delegated = [];
  const fetch = createApiFetch((input, options) => { delegated.push([input, options]); return 'native'; }, () => assert.fail());
  for (const [input, options] of [
    ['https://example.test/', { method: 'POST', body: '{}' }],
    ['https://api.obsidian.md.example.test/user/signin', { method: 'POST', body: '{}' }],
    ['http://api.obsidian.md/user/signin', { method: 'POST', body: '{}' }],
    ['https://api.obsidian.md/user/signin', { method: 'GET' }],
    ['https://api.obsidian.md/user/signin', { method: 'POST', body: new Uint8Array([1]) }],
  ]) assert.equal(fetch(input, options), 'native');
  assert.equal(delegated.length, 5);
});

test('network failures, interrupted responses and aborted requests fail without retry', async () => {
  for (const config of [{ error: new Error('fixture network error') }, { interrupted: true }]) {
    const upstream = fixture(config);
    await assert.rejects(createApiFetch(() => assert.fail(), upstream.request)('https://api.obsidian.md/user/info', { method: 'POST', body: '{}' }));
    assert.equal(upstream.calls.length, 1);
  }
  const controller = new AbortController(); controller.abort(new Error('fixture cancellation'));
  const upstream = fixture();
  await assert.rejects(createApiFetch(() => assert.fail(), upstream.request)('https://api.obsidian.md/user/info', { method: 'POST', signal: controller.signal }), /fixture cancellation/);
  assert.equal(upstream.calls.length, 0);
});
