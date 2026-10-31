// Some hosts time out on Obsidian API POSTs through Node's built-in fetch,
// while node:https works. Keep this workaround inside the project launcher;
// credentials, token storage, MFA and Sync remain owned by the official CLI.
import { request as httpsRequest } from 'node:https';

const API_ORIGIN = 'https://api.obsidian.md';

export function createApiFetch(fallback, request = httpsRequest) {
  return function apiFetch(input, options = {}) {
    if (typeof input !== 'string' && !(input instanceof URL)) return fallback(input, options);
    let url;
    try { url = new URL(input); } catch { return fallback(input, options); }
    const method = (options.method || 'GET').toUpperCase();
    if (url.origin !== API_ORIGIN || url.username || url.password
      || !['POST', 'OPTIONS'].includes(method)
      || (options.body !== undefined && typeof options.body !== 'string')) {
      return fallback(input, options);
    }

    const headers = new Headers(options.headers);
    // node:https does not transparently decompress fetch responses.
    headers.delete('accept-encoding');
    if (options.body !== undefined) headers.set('content-length', String(Buffer.byteLength(options.body)));
    const timeout = AbortSignal.timeout(30_000);
    const signal = options.signal ? AbortSignal.any([options.signal, timeout]) : timeout;
    if (signal.aborted) return Promise.reject(signal.reason);

    return new Promise((resolve, reject) => {
      const req = request(url, { method, headers: Object.fromEntries(headers), signal }, (res) => {
        const chunks = [];
        res.on('data', (chunk) => chunks.push(Buffer.from(chunk)));
        res.on('error', reject);
        res.on('aborted', () => reject(new Error('Obsidian API response was interrupted')));
        res.on('end', () => {
          try {
            const responseHeaders = new Headers();
            for (let index = 0; index < res.rawHeaders.length; index += 2) {
              responseHeaders.append(res.rawHeaders[index], res.rawHeaders[index + 1]);
            }
            const status = res.statusCode;
            const body = [204, 205, 304].includes(status) ? null : Buffer.concat(chunks);
            const response = new Response(body, { status, statusText: res.statusMessage, headers: responseHeaders });
            Object.defineProperty(response, 'url', { value: url.href });
            resolve(response);
          } catch (error) { reject(error); }
        });
      });
      req.on('error', reject);
      req.end(options.body);
    });
  };
}

// Escape hatch for a future upstream fix; never changes global Node settings.
if (process.env.OBSIDIAN_HEADLESS_NATIVE_FETCH !== '1') {
  globalThis.fetch = createApiFetch(globalThis.fetch);
}
