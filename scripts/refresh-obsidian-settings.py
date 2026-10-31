#!/usr/bin/env python3
"""Explicitly restart the existing client to rescan selected Obsidian settings files.

A receipt confirms a new ready client invocation, never remote byte equality.
Default invocation is read-only. Authentication and connection state stay with
server-sync.py; this module never opens them.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time


SETTINGS = ('.obsidian/daily-notes.json', '.obsidian/types.json')
SUPER_TABLE_SETTINGS = (
    '.obsidian/community-plugins.json',
    '.obsidian/plugins/super-table/manifest.json',
    '.obsidian/plugins/super-table/main.js',
    '.obsidian/plugins/super-table/styles.css',
)
UNIT = 'obsidian-vault-sync.service'


class RefreshError(Exception):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def load_manager():
    spec = importlib.util.spec_from_file_location('settings_refresh_server_sync',
                                                Path(__file__).with_name('server-sync.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Manager()


class SettingsRefresher:
    def __init__(self, manager, *, super_table=False, timeout=45, interval=0.25,
                 clock=time.monotonic, sleep=time.sleep):
        self.manager = manager
        self.vault = Path(manager.vault)
        self.runtime = Path(manager.project) / 'runtime/vault-sync'
        self.settings = SETTINGS + SUPER_TABLE_SETTINGS if super_table else SETTINGS
        self.receipt = self.runtime / ('settings-refresh-super-table.json'
                                       if super_table else 'settings-refresh.json')
        self.timeout, self.interval, self.clock, self.sleep = timeout, interval, clock, sleep
        if not 0 < timeout <= 120 or not 0 < interval <= timeout:
            raise RefreshError('invalid-wait-bounds')

    def snapshot(self):
        values = {}
        for relative in self.settings:
            path = self.vault / relative
            parent = self.vault
            for part in Path(relative).parts[:-1]:
                parent = parent / part
                if parent.is_symlink():
                    raise RefreshError('settings-path-is-symlink')
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                        raise RefreshError('settings-file-invalid')
                    content = stream.read(1024 * 1024 + 1)
                    if len(content) > 1024 * 1024:
                        raise RefreshError('settings-file-too-large')
            except OSError as exc:
                raise RefreshError('settings-file-unreadable') from exc
            values[relative] = hashlib.sha256(content).hexdigest()
        return values

    @staticmethod
    def invocation(status):
        value = status.get('invocation_id')
        if (status.get('manager') != 'systemd' or status.get('unit') != UNIT
                or not isinstance(value, str) or not re.fullmatch('[a-fA-F0-9]{32}', value)):
            raise RefreshError('expected-existing-systemd-invocation')
        return value

    def observe(self):
        first = self.status()
        invocation = self.invocation(first)
        settings = self.snapshot()
        last = self.status()
        if self.invocation(last) != invocation or last.get('pid') != first.get('pid'):
            raise RefreshError('service-changed-during-inspection')
        return settings, last

    def status(self):
        try:
            result = self.manager.status()
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except Exception as exc:
            raise RefreshError('service-status-failed') from exc

    def require_ready_invocation(self, invocation):
        current = self.status()
        if self.invocation(current) != invocation:
            raise RefreshError('service-changed-during-refresh')
        if (current.get('state') != 'running' or current.get('alive') is not True
                or current.get('ready') is not True):
            raise RefreshError('service-not-ready-after-refresh')

    def previous(self):
        if self.receipt.is_symlink():
            raise RefreshError('refresh-receipt-is-symlink')
        try:
            raw = self.receipt.read_bytes()
        except FileNotFoundError:
            return None, None
        try:
            row = json.loads(raw)
            if (row['schema_version'] != 1 or row['remote_verified'] is not False
                    or row['scan_restart_confirmed'] is not True
                    or not isinstance(row['settings'], dict)
                    or set(row['settings']) != set(self.settings)
                    or not all(isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value)
                               for value in row['settings'].values())
                    or row['settings_fingerprint'] != digest(row['settings'])
                    or not re.fullmatch('[a-fA-F0-9]{32}', row['invocation_id'])):
                raise ValueError()
        except (ValueError, TypeError, KeyError) as exc:
            raise RefreshError('invalid-refresh-receipt') from exc
        return row, raw

    def describe(self, settings, status):
        invocation = self.invocation(status)
        fingerprint = digest(settings)
        previous, _ = self.previous()
        noop = (previous is not None and previous['settings_fingerprint'] == fingerprint
                and previous['invocation_id'] == invocation)
        ready = (status.get('state') == 'running' and status.get('alive') is True
                 and status.get('ready') is True)
        token = digest({'version': 1, 'vault': str(self.vault), 'unit': UNIT,
                        'invocation_id': invocation, 'settings': settings})
        return {'action': 'noop' if noop else 'refresh-unconfirmed', 'expected_token': token,
                'reason': 'recorded-scan-matches' if noop else 'no-recorded-scan-for-current-settings-and-invocation',
                'settings': settings, 'settings_fingerprint': fingerprint,
                'invocation_id': invocation, 'ready': ready, 'can_apply': ready,
                'remote_verified': False}

    def plan(self):
        return self.describe(*self.observe())

    @contextmanager
    def lock(self):
        if self.runtime.is_symlink():
            raise RefreshError('runtime-path-is-symlink')
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.runtime / 'settings-refresh.lock'
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RefreshError('settings-refresh-busy') from exc
            yield

    def require_unchanged(self, expected):
        if self.snapshot() != expected:
            raise RefreshError('settings-changed-during-refresh')

    def save(self, row, settings, invocation, previous_bytes):
        """Commit only an unchanged snapshot; undo our receipt on a final race."""
        payload = (json.dumps(row, ensure_ascii=False, indent=2) + '\n').encode()
        fd, name = tempfile.mkstemp(prefix='.settings-refresh-', dir=self.runtime)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self.require_unchanged(settings)
            self.require_ready_invocation(invocation)
            os.replace(temporary, self.receipt)
            try:
                self.require_unchanged(settings)
                self.require_ready_invocation(invocation)
            except BaseException:
                # This lock serializes cooperating refreshers; never overwrite
                # another writer's receipt if it replaced ours independently.
                if self.receipt.read_bytes() == payload:
                    if previous_bytes is None:
                        self.receipt.unlink()
                    else:
                        temporary.write_bytes(previous_bytes)
                        temporary.chmod(0o600)
                        os.replace(temporary, self.receipt)
                raise
        finally:
            temporary.unlink(missing_ok=True)

    def apply(self, expected_token):
        if not expected_token:
            raise RefreshError('expected-token-required')
        with self.lock():
            settings, status = self.observe()
            plan = self.describe(settings, status)
            if plan['expected_token'] != expected_token:
                raise RefreshError('stale-expected-token')
            if not plan['ready']:
                raise RefreshError('sync-not-ready')
            if plan['action'] == 'noop':
                return {**plan, 'applied': False}
            _, previous_bytes = self.previous()
            old_invocation = plan['invocation_id']
            self.require_unchanged(settings)
            self.require_ready_invocation(old_invocation)
            try:
                stopped = self.manager.stop()
            except Exception as exc:
                raise RefreshError('service-stop-failed') from exc
            if (not isinstance(stopped, dict) or stopped.get('alive') is not False
                    or stopped.get('state') in ('mismatch', 'untracked')):
                raise RefreshError('service-did-not-stop')
            # Once stopped, attempt start even if the user edited settings in
            # between. Later checks refuse a receipt without leaving Sync down
            # solely because of that edit.
            try:
                current = self.manager.start()
            except Exception as exc:
                raise RefreshError('service-start-failed') from exc
            if not isinstance(current, dict):
                raise RefreshError('service-start-failed')
            deadline = self.clock() + self.timeout
            new_invocation = None
            while True:
                self.require_unchanged(settings)
                candidate = current.get('invocation_id')
                if candidate and candidate != old_invocation:
                    candidate = self.invocation(current)
                    if new_invocation is not None and candidate != new_invocation:
                        raise RefreshError('service-changed-during-refresh')
                    new_invocation = candidate
                    if (current.get('state') == 'running' and current.get('alive') is True
                            and current.get('ready') is True):
                        break
                if current.get('state') in ('failed', 'mismatch', 'untracked', 'stopped', 'inactive'):
                    raise RefreshError('service-startup-failed')
                if self.clock() >= deadline:
                    raise RefreshError('service-ready-timeout')
                self.sleep(min(self.interval, max(0, deadline - self.clock())))
                current = self.status()
            row = {'schema_version': 1, 'settings': settings,
                   'settings_fingerprint': plan['settings_fingerprint'],
                   'previous_invocation_id': old_invocation, 'invocation_id': new_invocation,
                   'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
                   'scan_restart_confirmed': True, 'remote_verified': False}
            self.save(row, settings, new_invocation, previous_bytes)
            return {**self.describe(settings, current), 'action': 'refreshed', 'applied': True,
                    'scan_restart_confirmed': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--super-table', action='store_true',
                        help='Also rescan the installed Super Table bundle and community plugin list')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-token')
    args = parser.parse_args()
    try:
        refresher = SettingsRefresher(load_manager(), super_table=args.super_table)
        result = refresher.apply(args.expected_token) if args.apply else refresher.plan()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (RefreshError, OSError) as exc:
        # No raw Manager exception/journal/config body enters this output.
        code = str(exc) if isinstance(exc, RefreshError) else 'settings-refresh-io-failed'
        print(json.dumps({'error': code, 'remote_verified': False}))
        return 1
    except Exception:
        print(json.dumps({'error': 'settings-refresh-failed', 'remote_verified': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
