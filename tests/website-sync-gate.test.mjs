import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, rmSync, utimesSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {
  MAX_SKIP_MS, canSkip, clearGate, readGate, remoteFingerprint, vaultFingerprint, writeGate,
} from '../scripts/website-sync-gate.mjs';

function temporaryVault() {
  const vault = mkdtempSync(path.join(tmpdir(), 'sync-gate-'));
  mkdirSync(path.join(vault, 'notes'));
  mkdirSync(path.join(vault, '.markdown-library/objects'), { recursive: true });
  mkdirSync(path.join(vault, '.git'));
  mkdirSync(path.join(vault, '.obsidian'));
  writeFileSync(path.join(vault, 'notes/a.md'), 'one');
  writeFileSync(path.join(vault, '.markdown-library/state.json'), '{}');
  return vault;
}

function remote(pages) {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url: new URL(url), options });
    const page = pages[new URL(url).pathname];
    return { ok: page !== undefined, json: async () => page };
  };
  return { calls, fetchImpl };
}

test('vault fingerprint follows notes and tool state but ignores history, Git and editor settings', () => {
  const vault = temporaryVault();
  try {
    const initial = vaultFingerprint(vault);
    assert.equal(vaultFingerprint(vault), initial);

    writeFileSync(path.join(vault, '.markdown-library/objects/blob'), 'retained');
    writeFileSync(path.join(vault, '.git/index'), 'git');
    writeFileSync(path.join(vault, '.obsidian/workspace.json'), '{}');
    assert.equal(vaultFingerprint(vault), initial);

    writeFileSync(path.join(vault, 'notes/a.md'), 'two!');
    const edited = vaultFingerprint(vault);
    assert.notEqual(edited, initial);

    // Same size, different modification time: still a change.
    utimesSync(path.join(vault, 'notes/a.md'), new Date(1_000_000), new Date(1_000_000));
    const touched = vaultFingerprint(vault);
    assert.notEqual(touched, edited);

    writeFileSync(path.join(vault, 'notes/b.md'), '');
    const added = vaultFingerprint(vault);
    assert.notEqual(added, touched);

    writeFileSync(path.join(vault, '.markdown-library/state.json'), '{"selected":1}');
    assert.notEqual(vaultFingerprint(vault), added);
  } finally { rmSync(vault, { recursive: true, force: true }); }
});

test('remote fingerprint asks for the newest draft including trash and changes with edits or counts', async () => {
  const pages = {
    '/api/works': { totalDocs: 2, docs: [{ id: 7, updatedAt: '2026-01-01T00:00:00.000Z' }] },
    '/api/posts': { totalDocs: 0, docs: [] },
  };
  const first = remote(pages);
  const initial = await remoteFingerprint('https://site.example', 'fixture-key', { fetchImpl: first.fetchImpl });
  assert.deepEqual(first.calls.map(call => call.url.pathname), ['/api/works', '/api/posts']);
  for (const call of first.calls) {
    assert.equal(call.url.searchParams.get('draft'), 'true');
    assert.equal(call.url.searchParams.get('trash'), 'true');
    assert.equal(call.url.searchParams.get('sort'), '-updatedAt');
    assert.equal(call.url.searchParams.get('limit'), '1');
    assert.equal(call.options.headers.Authorization, 'content-clients API-Key fixture-key');
  }
  assert.equal(await remoteFingerprint('https://site.example', 'fixture-key', { fetchImpl: first.fetchImpl }), initial);

  pages['/api/works'] = { totalDocs: 2, docs: [{ id: 7, updatedAt: '2026-01-02T00:00:00.000Z' }] };
  const edited = await remoteFingerprint('https://site.example', 'k', remote(pages));
  assert.notEqual(edited, initial);
  pages['/api/posts'] = { totalDocs: 1, docs: [{ id: 1, updatedAt: '2025-01-01T00:00:00.000Z' }] };
  assert.notEqual(await remoteFingerprint('https://site.example', 'k', remote(pages)), edited);
});

test('remote fingerprint refuses failed or malformed replies instead of guessing', async () => {
  await assert.rejects(remoteFingerprint('https://site.example', 'k', remote({})), /remote-fingerprint-unavailable/);
  await assert.rejects(remoteFingerprint('https://site.example', 'k', remote({
    '/api/works': { docs: [] }, '/api/posts': { totalDocs: 0, docs: [] },
  })), /remote-fingerprint-unavailable/);
  await assert.rejects(remoteFingerprint('https://site.example', 'k', remote({
    '/api/works': { totalDocs: 1, docs: [{ id: 1 }] }, '/api/posts': { totalDocs: 0, docs: [] },
  })), /remote-fingerprint-unavailable/);
});

test('gate skips only an identical state seen by a recent full run', () => {
  const directory = mkdtempSync(path.join(tmpdir(), 'sync-gate-state-'));
  const file = path.join(directory, 'gate.json');
  try {
    assert.equal(readGate(file), null);
    assert.equal(canSkip(null, 'a'), false);
    const now = Date.parse('2026-06-01T12:00:00.000Z');
    writeGate(file, { fingerprint: 'a', full_run_at: '2026-06-01T11:00:00.000Z', exit_code: 1, needs_review: true });
    const gate = readGate(file);
    assert.equal(gate.exit_code, 1);
    assert.equal(gate.needs_review, true);
    assert.equal(canSkip(gate, 'a', now), true);
    assert.equal(canSkip(gate, 'b', now), false);
    assert.equal(canSkip(gate, 'a', Date.parse(gate.full_run_at) + MAX_SKIP_MS), false);
    assert.equal(canSkip(gate, 'a', Date.parse(gate.full_run_at) - 1), false);

    writeFileSync(file, '{"version":1,"fingerprint":"a"}');
    assert.equal(readGate(file), null);
    writeFileSync(file, 'not json');
    assert.equal(readGate(file), null);
    clearGate(file);
    clearGate(file);
    assert.equal(readGate(file), null);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});
