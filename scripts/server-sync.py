#!/usr/bin/env python3
"""Manage this project's existing official Headless continuous sync process."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time


PROJECT = Path(__file__).resolve().parents[1]
VAULT = Path('/home/trace/projects/personal-records-vault')
SYSTEMD_UNIT = 'obsidian-vault-sync.service'


class SyncError(Exception):
    pass


class Manager:
    def __init__(self, project=PROJECT, vault=VAULT, proc_root=Path('/proc'), *, use_systemd=None):
        self.project = Path(project).resolve()
        self.vault = Path(vault).resolve()
        self.proc_root = Path(proc_root)
        self.runtime = self.project / 'runtime/vault-sync'
        self.record = self.runtime / 'process.json'
        self.log = self.runtime / 'sync.log'
        self.cli = self.project / 'tools/obsidian-headless/node_modules/obsidian-headless/cli.js'
        # Temporary projects/fake /proc tests must never inspect or control the
        # real user's globally installed unit. Real defaults discover it first.
        self.use_systemd = (self.project == PROJECT and self.vault == VAULT
                            and self.proc_root == Path('/proc')) if use_systemd is None else use_systemd

    def systemd(self):
        if not self.use_systemd:
            return None
        properties = ('LoadState', 'ActiveState', 'SubState', 'MainPID', 'InvocationID',
                      'UnitFileState', 'Result', 'NRestarts')
        try:
            checked = subprocess.run(
                ['systemctl', '--user', 'show', SYSTEMD_UNIT, '--no-pager',
                 '--property=' + ','.join(properties)], capture_output=True, text=True,
                stdin=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SyncError('Cannot inspect the user systemd manager; refusing a second sync process') from exc
        values = dict(line.split('=', 1) for line in checked.stdout.splitlines() if '=' in line)
        if values.get('LoadState') == 'not-found':
            return None
        if checked.returncode or values.get('LoadState') != 'loaded':
            raise SyncError('Cannot verify the installed sync unit; inspect systemctl --user locally')
        try:
            values['MainPID'] = int(values['MainPID'])
            if values['MainPID'] < 0 or values['MainPID'] == 1:
                raise ValueError()
            invocation = values.get('InvocationID', '')
            if invocation and not re.fullmatch('[a-fA-F0-9]{32}', invocation):
                raise ValueError()
        except (ValueError, KeyError) as exc:
            raise SyncError('User systemd returned invalid process metadata') from exc
        return values

    @staticmethod
    def is_activity(line):
        # Official Headless 0.0.14 emits these messages at the start of a
        # transfer/change operation. Anchor the status prefix and its separator;
        # a filename such as Uploading.md or "Downloaded Connecting....md"
        # must not be interpreted as a new state transition.
        lower = line.lower()
        return (lower in ('connecting...', 'connection successful. detecting changes...',
                          'waiting to connect to server')
                or re.match(r'^(?:uploading(?: file)?|downloading|checking|'
                            r'deleting(?: remote (?:file|folder))?|restoring|reverting|'
                            r'merging conflicted file|renaming conflicted file|'
                            r'removing local-only (?:file|folder)|accepted|skipped|'
                            r'rejected server change)(?:$|[ \t])', lower) is not None)

    @staticmethod
    def readiness(lines):
        ready, last_error = False, None
        for line in lines:
            lower = line.lower()
            if lower.strip() == 'fully synced' or lower.startswith('watching '):
                ready, last_error = True, None
            elif Manager.is_activity(line):
                ready = False
            if re.match(r'^(?:sync (?:error|failed)|error\b|failed\b|disconnected\b)', lower):
                ready, last_error = False, line
        return ready, last_error

    def journal_status(self, invocation):
        if not invocation:
            return None, None, 'systemd invocation has not started'
        checked = subprocess.run(
            ['journalctl', '--user', '-u', SYSTEMD_UNIT,
             '_SYSTEMD_INVOCATION_ID=' + invocation, '--output=json', '--no-pager', '--quiet', '-n', '2000'],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15)
        if checked.returncode:
            return None, None, 'current systemd journal is unavailable'
        try:
            rows = [json.loads(line) for line in checked.stdout.splitlines() if line.strip()]
            # Filter defensively even when journalctl is already filtering. A
            # previous invocation's Fully synced must never mark a new PID ready.
            lines = [row['MESSAGE'] for row in rows if row.get('_SYSTEMD_INVOCATION_ID') == invocation
                     and isinstance(row.get('MESSAGE'), str)]
        except (ValueError, TypeError, AttributeError) as exc:
            raise SyncError('Current systemd journal returned invalid JSON') from exc
        if not lines:
            return None, None, 'no messages available for the current invocation'
        ready, error = self.readiness(lines)
        if not ready and error is None and not any(self.is_activity(line) for line in lines):
            return None, None, 'no readiness marker in the latest invocation messages'
        # Avoid copying arbitrary journal bodies (which can contain note names
        # or authentication error detail) into public status output.
        return ready, 'Official sync reported an error; inspect the private user journal' if error else None, None

    def systemd_status(self, unit):
        pid = unit['MainPID']
        proc = self.process(pid) if pid else None
        matches = proc is not None and self.command_matches(proc)
        mismatch = pid != 0 and not matches
        state = unit.get('ActiveState', 'unknown')
        alive = matches and state in ('active', 'activating', 'deactivating')
        ready, error, issue = self.journal_status(unit.get('InvocationID')) if alive else (False, None, None)
        return {'state': 'running' if alive else ('mismatch' if mismatch else state),
                'alive': alive, 'ready': ready, 'pid': pid or None,
                'reason': 'systemd PID does not match the expected official client' if mismatch else None,
                'last_error': error, 'log_issue': issue, 'vault': str(self.vault),
                'manager': 'systemd', 'unit': SYSTEMD_UNIT,
                'invocation_id': unit.get('InvocationID') or None,
                'autostart': unit.get('UnitFileState') in ('enabled', 'enabled-runtime'),
                'restart_count': unit.get('NRestarts'), 'result': unit.get('Result'),
                'active_state': state, 'sub_state': unit.get('SubState')}

    def systemd_action(self, action, unit):
        if unit['MainPID']:
            proc = self.process(unit['MainPID'])
            if proc is None or not self.command_matches(proc):
                raise SyncError('Refusing to control a unit whose PID does not match this Vault client')
        if action == 'start':
            # An old detached instance must be stopped explicitly before systemd
            # takes over. Do not create two clients writing the same sync index.
            extra = set(self.matching_processes()) - {unit['MainPID']}
            if extra:
                raise SyncError('Detached sync process still exists; complete the controlled handoff first')
        checked = subprocess.run(['systemctl', '--user', action, SYSTEMD_UNIT],
                                 capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=45)
        if checked.returncode:
            raise SyncError(f'User systemd {action} failed; inspect the private user journal')
        return self.status()

    def metadata(self):
        if not self.record.exists():
            return None
        try:
            data = json.loads(self.record.read_text())
            if (type(data['pid']) is not int or data['pid'] <= 1
                    or not str(data['start_ticks']).isdigit()
                    or data['vault'] != str(self.vault)
                    or data.get('continuous') is not True
                    or data.get('mode') != 'bidirectional'
                    or data.get('conflict_strategy') != 'conflict'
                    or data.get('log') != str(self.log)):
                raise ValueError('unexpected identity or configuration')
            offset = data.get('log_offset')
            if offset is not None and (type(offset) is not int or offset < 0):
                raise ValueError('invalid log_offset')
            return data
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SyncError(f'Invalid process metadata: {exc}') from exc

    def process(self, pid):
        root = self.proc_root / str(pid)
        try:
            raw = (root / 'stat').read_text()
            fields = raw[raw.rindex(')') + 2:].split()
            if fields[0] == 'Z':
                return None
            return {'start_ticks': fields[19], 'uid': root.stat().st_uid,
                    'argv': (root / 'cmdline').read_bytes().decode().rstrip('\0').split('\0'),
                    'cwd': str((root / 'cwd').resolve(strict=True))}
        except (FileNotFoundError, ProcessLookupError):
            return None
        except (OSError, ValueError, IndexError, UnicodeError) as exc:
            raise SyncError(f'Cannot inspect process {pid}: {exc}') from exc

    def command_args_match(self, argv):
        try:
            pos = argv.index(str(self.cli))
        except ValueError:
            return False
        return (Path(argv[0]).name in ('node', 'nodejs')
                and argv[pos + 1:] == ['sync', '--path', str(self.vault), '--continuous'])

    def command_matches(self, proc):
        return (proc['uid'] == os.getuid() and self.command_args_match(proc['argv'])
                and proc['cwd'] == str(self.project))

    def identity(self, data):
        proc = self.process(data['pid'])
        if proc is None:
            return False, 'process missing or exited'
        if proc['start_ticks'] != str(data['start_ticks']):
            return False, 'PID reused: start time differs'
        if not self.command_matches(proc):
            return False, 'command, cwd, vault, or continuous mode differs'
        return True, None

    def matching_processes(self):
        found = []
        for entry in self.proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                # Same-UID protected daemons may deny cwd even though they are
                # unrelated. Read only uid/argv first; inspect cwd/start time
                # for actual candidate clients, never every process on the host.
                if entry.stat().st_uid != os.getuid():
                    continue
                argv = (entry / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')
                if not self.command_args_match(argv):
                    continue
                proc = self.process(int(entry.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
            except SyncError:
                # Other users' protected /proc entries cannot belong to our process.
                if entry.stat().st_uid == os.getuid():
                    raise
                continue
            if proc and self.command_matches(proc):
                found.append(int(entry.name))
        return found

    def log_status(self, data):
        if 'log_offset' not in data:
            return None, None, 'startup log boundary unavailable'
        try:
            with self.log.open('rb') as handle:
                if os.fstat(handle.fileno()).st_size < data['log_offset']:
                    return None, None, 'log was truncated'
                if data.get('log_inode') and os.fstat(handle.fileno()).st_ino != data['log_inode']:
                    return None, None, 'log was replaced'
                handle.seek(data['log_offset'])
                lines = handle.read().decode('utf-8', errors='replace').splitlines()
        except OSError as exc:
            return None, None, str(exc)
        ready, last_error = self.readiness(lines)
        return ready, last_error, None

    def status(self):
        unit = self.systemd()
        if unit is not None:
            return self.systemd_status(unit)
        data = self.metadata()
        if data is None:
            found = self.matching_processes()
            return {'state': 'untracked' if found else 'stopped', 'alive': bool(found),
                    'ready': None, 'pids': found, 'needs_review': bool(found)}
        alive, reason = self.identity(data)
        ready, last_error, log_issue = self.log_status(data) if alive else (False, None, None)
        return {'state': 'running' if alive else 'stale', 'alive': alive,
                'ready': ready, 'pid': data['pid'], 'reason': reason,
                'last_error': last_error, 'log_issue': log_issue,
                'vault': str(self.vault), 'autostart': False}

    @contextmanager
    def lock(self):
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runtime.chmod(0o700)
        with (self.runtime / 'lifecycle.lock').open('a') as handle:
            os.chmod(handle.name, 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def check_configuration(self, previous=None):
        command = [str(self.project / 'scripts/obsidian-sync')]
        checked = subprocess.run(command + ['sync-config', '--path', str(self.vault), '--json'],
                                 cwd=self.project, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=15)
        if checked.returncode:
            raise SyncError('Read-only sync configuration check failed; inspect setup locally')
        try:
            config = json.loads(checked.stdout)
            if (config['vaultPath'] != str(self.vault) or config['syncMode'] != 'bidirectional'
                    or config['conflictStrategy'] != 'conflict' or not config['vaultId']):
                raise ValueError('expected bidirectional/conflict configuration')
        except (KeyError, ValueError, TypeError) as exc:
            raise SyncError('Sync configuration is not ready; expected bidirectional/conflict configuration') from exc
        # These official queries omit credentials; validate stored key presence
        # internally without exposing or prompting for the key.
        checked = subprocess.run(command + ['sync-status', '--path', str(self.vault), '--json'],
                                 cwd=self.project, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=15)
        if checked.returncode:
            raise SyncError('Official sync-status reports incomplete setup; inspect setup locally')
        if previous and config['vaultId'] != previous.get('remote_vault_id'):
            raise SyncError('Remote vault changed since the previous process record; review required')
        return config

    def check(self):
        """systemd ExecStartPre: no backgrounding or lifecycle lock recursion."""
        if self.matching_processes():
            raise SyncError('An existing sync client must be stopped before systemd starts another')
        self.check_configuration(self.metadata())
        return {'state': 'configuration-verified', 'mode': 'bidirectional', 'conflict_strategy': 'conflict'}

    def start(self):
        with self.lock():
            unit = self.systemd()
            if unit is not None:
                return self.systemd_action('start', unit)
            data = self.metadata()
            if data:
                alive, reason = self.identity(data)
                if alive:
                    return self.status()
                if self.process(data['pid']) is not None:
                    raise SyncError(f'Refusing to replace metadata: {reason}')
            found = self.matching_processes()
            if found:
                raise SyncError(f'Untracked matching process requires review: {found}')
            command = [str(self.project / 'scripts/obsidian-sync')]
            config = self.check_configuration(data)
            with self.log.open('ab') as output:
                os.chmod(self.log, 0o600)
                offset = output.tell()
                child = subprocess.Popen(command + ['sync', '--path', str(self.vault), '--continuous'],
                                         cwd=self.project, stdin=subprocess.DEVNULL,
                                         stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                proc = None
                for _ in range(100):
                    proc = self.process(child.pid)
                    if proc and self.command_matches(proc):
                        break
                    if child.poll() is not None:
                        raise SyncError('Official sync exited during startup; inspect private log')
                    time.sleep(0.02)
                else:
                    raise SyncError(f'Cannot verify started process {child.pid}; review before retrying')
                record = {'pid': child.pid, 'start_ticks': proc['start_ticks'],
                          'started_at': datetime.now(timezone.utc).isoformat(),
                          'vault': str(self.vault), 'remote_vault_id': config['vaultId'],
                          'mode': 'bidirectional', 'conflict_strategy': 'conflict',
                          'continuous': True, 'log': str(self.log), 'log_offset': offset,
                          'log_inode': os.fstat(output.fileno()).st_ino, 'autostart': False}
            temporary = self.record.with_suffix('.tmp')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as handle:
                json.dump(record, handle, indent=2)
                handle.write('\n')
            temporary.replace(self.record)
            return self.status()

    def stop(self):
        with self.lock():
            unit = self.systemd()
            if unit is not None:
                return self.systemd_action('stop', unit)
            data = self.metadata()
            if not data:
                return self.status()
            alive, reason = self.identity(data)
            if not alive:
                return self.status()
            descriptor = None
            try:
                if self.proc_root == Path('/proc') and hasattr(os, 'pidfd_open'):
                    descriptor = os.pidfd_open(data['pid'])
                alive, reason = self.identity(data)
                if not alive:
                    raise SyncError(f'Process changed before stop: {reason}')
                if descriptor is not None:
                    signal.pidfd_send_signal(descriptor, signal.SIGINT)
                else:
                    os.kill(data['pid'], signal.SIGINT)
            except ProcessLookupError:
                return self.status()
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            for _ in range(100):
                if not self.identity(data)[0]:
                    return self.status()
                time.sleep(0.05)
            return {**self.status(), 'stop_requested': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'status', 'stop', 'check'))
    args = parser.parse_args()
    try:
        result = getattr(Manager(), args.action)()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (SyncError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
