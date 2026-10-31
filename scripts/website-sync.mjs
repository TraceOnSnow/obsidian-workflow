#!/usr/bin/env node
// Reuse the standard content-client credential file; never persist its value.
import { spawnSync } from 'node:child_process';
import { mkdirSync, statSync, lstatSync, readdirSync, readFileSync, unlinkSync, writeFileSync, renameSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const project = fileURLToPath(new URL('../', import.meta.url));
const vault = '/home/trace/projects/personal-records-vault';
const origin = 'https://traceonsnow.com';
const credentials = '/home/trace/.config/homepage-content/client.env';
const output = path.join(project, 'runtime/website-sync');
const flags = process.argv.slice(2);
if (flags.some(flag => !['--apply', '--accept-remote'].includes(flag)) || new Set(flags).size !== flags.length
    || (flags.includes('--accept-remote') && !flags.includes('--apply'))) {
  console.error('Use no arguments to preview, or --apply [--accept-remote].');
  process.exit(2);
}
process.umask(0o077);
try {
  // Fail before any remote/local sync mutation if receipts cannot be retained
  // privately. Discovering this after --apply would hide an executed write.
  mkdirSync(output, { recursive: true, mode: 0o700 });
  const directory = lstatSync(output);
  if (!directory.isDirectory() || directory.isSymbolicLink() || directory.uid !== process.getuid()
      || (directory.mode & 0o077)) throw Error('private-receipt-directory-required');
  const info = statSync(credentials);
  if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077)) throw Error('private-credentials-required');
  process.loadEnvFile(credentials);
  if (process.env.HOMEPAGE_URL !== origin || !process.env.HOMEPAGE_API_KEY) throw Error('configured-origin-or-key-invalid');
  const started = new Date().toISOString();
  const result = spawnSync(path.join(project, '.venv/bin/markdown-library'), [
    '--vault', vault, 'sync', '--url', origin, '--api-key-env', 'HOMEPAGE_API_KEY',
    '--works-drafts-and-versions', ...flags,
  ], { cwd: project, encoding: 'utf8', timeout: 240_000, maxBuffer: 32 * 1024 * 1024,
    env: { PATH: process.env.PATH, HOME: process.env.HOME, HOMEPAGE_API_KEY: process.env.HOMEPAGE_API_KEY } });
  // CLI replies contain private differences; only counts belong in the journal.
  let reply;
  try { reply = JSON.parse(result.stdout); } catch { throw Error('sync-did-not-return-a-receipt'); }
  if (result.error || result.status !== 0 || !reply || !Array.isArray(reply.results)) throw Error('sync-failed-no-success-receipt');
  const statuses = {};
  for (const row of reply.results) statuses[row.status] = (statuses[row.status] || 0) + 1;
  let needsReview = Boolean(reply.pending_ids?.length) || Boolean(reply.plan?.missing_local_ids?.length)
    || Boolean(reply.plan?.unmapped_remote_records?.length)
    || reply.results.some(row => Boolean(row.blocked_by?.length)
      || ['conflict', 'blocked', 'reconciliation-required', 'failed', 'adopt-review'].includes(row.status));
  const syncNeedsReview = needsReview;
  // A selected publication can be unchanged while its current working file has
  // new edits. Independently observe current/selection/latest/formal instead of
  // presenting the action count as proof that every draft equals the website.
  const childEnvironment = { PATH: process.env.PATH, HOME: process.env.HOME,
    HOMEPAGE_API_KEY: process.env.HOMEPAGE_API_KEY };
  let review = null;
  let inventory = null;
  let relations = null;
  let inventoryVerified = false;
  let reviewFile = null;
  try {
    if (flags.includes('--apply')) {
      const related = spawnSync(path.join(project, '.venv/bin/markdown-library'), [
        '--vault', vault, 'relations', '--apply',
      ], { cwd: project, encoding: 'utf8', timeout: 60_000, maxBuffer: 4 * 1024 * 1024, env: childEnvironment });
      if (related.error || related.status !== 0) throw Error('relationship-refresh-failed');
      relations = JSON.parse(related.stdout);
      if (relations.issues?.length) needsReview = true;
    }
    const observed = spawnSync(path.join(project, '.venv/bin/markdown-library'), [
      '--vault', vault, 'sync-review', '--url', origin, '--api-key-env', 'HOMEPAGE_API_KEY',
    ], { cwd: project, encoding: 'utf8', timeout: 180_000, maxBuffer: 32 * 1024 * 1024, env: childEnvironment });
    if (observed.error || observed.status !== 0) throw Error('independent-review-failed');
    review = JSON.parse(observed.stdout);
    reviewFile = path.join(output, `.review-${process.pid}.json`);
    writeFileSync(reviewFile, JSON.stringify(review), { flag: 'wx', mode: 0o600 });
    const rendered = spawnSync(path.join(project, '.venv/bin/python'), [
      path.join(project, 'scripts/library-dashboard.py'), '--vault', vault, '--review', reviewFile,
      '--observed-at', new Date().toISOString(), ...(flags.includes('--apply') ? ['--apply'] : []),
    ], { cwd: project, encoding: 'utf8', timeout: 60_000, maxBuffer: 4 * 1024 * 1024, env: childEnvironment });
    if (rendered.error || rendered.status !== 0) throw Error('inventory-render-failed');
    inventory = JSON.parse(rendered.stdout);
    inventoryVerified = true;
    if (inventory.counts?.conflicts || inventory.counts?.review) needsReview = true;
  } catch {
    needsReview = true;
  } finally {
    if (reviewFile) unlinkSync(reviewFile);
  }
  const receipt = { tool: 'markdown-website-sync-v1', started, finished: new Date().toISOString(), origin, vault,
    mode: flags.includes('--apply') ? 'apply' : 'preview', accept_remote: flags.includes('--accept-remote'),
    statuses, needs_review: needsReview, receipt: reply, review, inventory,
    inventory_verified: inventoryVerified, relations };
  const filename = `run-${started.replaceAll(/[^0-9TZ]/g, '')}-${process.pid}.json`;
  writeFileSync(path.join(output, filename), JSON.stringify(receipt, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
  const temporary = path.join(output, `latest-${process.pid}.tmp`);
  writeFileSync(temporary, JSON.stringify(receipt, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
  renameSync(temporary, path.join(output, 'latest.json'));
  // Bound routine polling receipts; preserve unrecognized files and directories.
  const runs = readdirSync(output).filter(name => /^run-\d{8}T\d{9}Z-\d+\.json$/.test(name)).sort();
  for (const name of runs.slice(0, Math.max(0, runs.length - 30))) {
    const candidate = path.join(output, name);
    const info = lstatSync(candidate);
    if (!info.isFile() || info.isSymbolicLink() || info.uid !== process.getuid() || (info.mode & 0o077)) continue;
    let previous;
    try { previous = JSON.parse(readFileSync(candidate, 'utf8')); } catch { continue; }
    if (previous.tool !== receipt.tool || previous.origin !== origin || previous.vault !== vault) continue;
    unlinkSync(candidate);
  }
  console.log(JSON.stringify({ origin, mode: receipt.mode, statuses, needs_review: needsReview,
    inventory_verified: inventoryVerified, inventory_counts: inventory?.counts,
    private_receipt: `runtime/website-sync/${filename}` }));
  // An accurately reported missing personal note is a review item, rather than
  // a failed website synchronization. Keep actual conflicts/pending/errors
  // distinguishable from this informational inventory condition.
  if (syncNeedsReview || !inventoryVerified || inventory?.counts?.conflicts || relations?.issues?.length) process.exitCode = 1;
} catch {
  // No environment values, remote bodies, child stderr or exception URLs.
  try {
    mkdirSync(output, { recursive: true, mode: 0o700 });
    const info = lstatSync(output);
    if (info.isDirectory() && !info.isSymbolicLink() && info.uid === process.getuid() && !(info.mode & 0o077)) {
      const temporary = path.join(output, `failure-${process.pid}.tmp`);
      writeFileSync(temporary, JSON.stringify({ tool: 'markdown-website-sync-v1', finished: new Date().toISOString(),
        origin, vault, needs_review: true, verified_success: false,
        error: 'No verified success receipt; check configuration and prior receipts.' }, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
      renameSync(temporary, path.join(output, 'latest.json'));
    }
  } catch { /* A private directory failure must not cause a public fallback. */ }
  console.error('Website sync failed; no verified success receipt. Check the private configuration and prior receipts.');
  process.exitCode = 1;
}
