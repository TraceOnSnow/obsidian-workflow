// Cheap "did anything change?" check for the periodic website sync.
//
// A full sync reads every managed note and every remote record. This gate
// fingerprints both sides from metadata only (file size/mtime in the Vault,
// newest updatedAt and record count per remote collection) so an idle tick
// can return without doing that work. It never decides what to sync and
// never writes to the Vault or the website.
import { createHash } from 'node:crypto';
import { lstatSync, readdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs';
import path from 'node:path';

export const GATE_VERSION = 1;
// Even with identical fingerprints, repeat the full sync at this interval so
// a change this gate cannot see is bounded rather than permanent.
export const MAX_SKIP_MS = 6 * 60 * 60 * 1000;

const SKIPPED_DIRECTORIES = new Set(['.git', '.obsidian', '.trash']);

/** Size and mtime of every Vault file that can affect a sync. */
export function vaultFingerprint(vault) {
  const hash = createHash('sha256');
  const walk = (directory, relative) => {
    const entries = readdirSync(directory, { withFileTypes: true })
      .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    for (const entry of entries) {
      const child = path.join(directory, entry.name);
      const name = relative ? `${relative}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        if (!relative && SKIPPED_DIRECTORIES.has(entry.name)) continue;
        // Tool state: selections and baselines live in state.json. Retained
        // objects are content-addressed history and never drive a sync.
        if (!relative && entry.name === '.markdown-library') {
          try {
            const info = lstatSync(path.join(child, 'state.json'), { bigint: true });
            hash.update(`${name}/state.json\0${info.size}\0${info.mtimeNs}\n`);
          } catch { hash.update(`${name}/state.json\0absent\n`); }
          continue;
        }
        walk(child, name);
      } else if (entry.isFile()) {
        const info = lstatSync(child, { bigint: true });
        hash.update(`${name}\0${info.size}\0${info.mtimeNs}\n`);
      }
    }
  };
  walk(vault, '');
  return hash.digest('hex');
}

/** Newest draft-or-published updatedAt and record count per collection. */
export async function remoteFingerprint(origin, apiKey, { fetchImpl = fetch, timeoutMs = 20_000 } = {}) {
  const parts = [];
  for (const collection of ['works', 'posts']) {
    const url = new URL(`/api/${collection}`, origin);
    for (const [key, value] of Object.entries({ draft: 'true', trash: 'true', depth: '0', limit: '1', sort: '-updatedAt' })) {
      url.searchParams.set(key, value);
    }
    const response = await fetchImpl(url, {
      headers: { Authorization: `content-clients API-Key ${apiKey}`, Accept: 'application/json' },
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!response.ok) throw Error('remote-fingerprint-unavailable');
    const body = await response.json();
    if (!Number.isInteger(body?.totalDocs) || !Array.isArray(body.docs)) throw Error('remote-fingerprint-unavailable');
    const newest = body.docs[0];
    if (body.totalDocs > 0 && (newest?.id === undefined || typeof newest.updatedAt !== 'string')) {
      throw Error('remote-fingerprint-unavailable');
    }
    parts.push(`${collection}\0${body.totalDocs}\0${newest?.id ?? ''}\0${newest?.updatedAt ?? ''}`);
  }
  return createHash('sha256').update(parts.join('\n')).digest('hex');
}

export function readGate(file) {
  try {
    const gate = JSON.parse(readFileSync(file, 'utf8'));
    if (gate?.version !== GATE_VERSION || typeof gate.fingerprint !== 'string'
        || !Number.isFinite(Date.parse(gate.full_run_at)) || !Number.isInteger(gate.exit_code)) return null;
    return gate;
  } catch { return null; }
}

export function writeGate(file, gate) {
  const temporary = `${file}.${process.pid}.tmp`;
  writeFileSync(temporary, JSON.stringify({ version: GATE_VERSION, ...gate }, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
  renameSync(temporary, file);
}

export function clearGate(file) {
  try { unlinkSync(file); } catch { /* Absent is already cleared. */ }
}

/** True only when the last full run saw exactly this state recently enough. */
export function canSkip(gate, fingerprint, now = Date.now()) {
  if (!gate || gate.fingerprint !== fingerprint) return false;
  const age = now - Date.parse(gate.full_run_at);
  return age >= 0 && age < MAX_SKIP_MS;
}
