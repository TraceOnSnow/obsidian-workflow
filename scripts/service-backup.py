#!/usr/bin/env python3
"""Private daily Payload data and historical workspace backup; never stop writers."""
from datetime import datetime, timezone
from contextlib import closing
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('vault_backup_helpers', PROJECT / 'scripts/vault-backup.py')
safe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(safe)
BackupError = safe.BackupError
FORMAT = 'workflow-services-backup-v1'
NAME = re.compile(r'services-\d{8}T\d{12}Z-[a-f0-9]{8}')
DEFAULT_DATABASE = PROJECT.parent / 'homepage-deployment/data/database/payload.db'
DEFAULT_MEDIA = PROJECT.parent / 'homepage-deployment/data/media'
DEFAULT_WORKBENCH = PROJECT.parent / 'chatgpt-pipeline/runtime/workbench'
DEFAULT_ROOT = PROJECT / 'runtime/service-backups'
WORKBENCH_DATABASE_FILES = {'state.sqlite3', 'state.sqlite3-wal', 'state.sqlite3-shm', 'state.sqlite3-journal'}
COMPONENTS = {'payload-media': 'payload_media', 'workbench-files': 'workbench'}
DATABASES = {'payload.sqlite3': 'payload_database', 'workbench.sqlite3': 'workbench'}
EXPECTED = {'manifest.json', *COMPONENTS, *DATABASES}


def files_snapshot(root, *, workbench=False):
    """Enumerate only the explicitly selected media/workspace tree, not references."""
    safe.safe_path(root)
    found = {}

    def denied(unused_error):
        raise BackupError('Cannot enumerate the complete permitted source tree')

    for directory, names, files in os.walk(root, followlinks=False, onerror=denied):
        for name in sorted(names + files):
            filename = Path(directory) / name
            relative = filename.relative_to(root).as_posix()
            safe.valid_member(relative)
            mode = filename.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise BackupError('Source contains a symlink or special file')
            if name in {'runtime.env', '.env'}:
                raise BackupError('Unexpected credential filename inside the permitted source; backup refused')
            if workbench and relative in WORKBENCH_DATABASE_FILES:
                continue  # SQLite backup includes committed WAL content separately.
            if stat.S_ISDIR(mode):
                found[relative] = {'type': 'directory'}
            else:
                digest, size = safe.digest_file(filename)
                found[relative] = {'type': 'file', 'sha256': digest, 'size': size}
    return dict(sorted(found.items()))


def check_database(filename):
    # Only completed backup copies are immutable; never ignore a live source WAL.
    with closing(sqlite3.connect(filename.as_uri() + '?mode=ro&immutable=1', uri=True)) as database:
        if database.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise BackupError('Database backup failed integrity validation')
        if database.execute('PRAGMA foreign_key_check').fetchall():
            raise BackupError('Database backup failed foreign-key validation')


def copy_database(source, destination):
    safe.safe_path(source)
    for candidate in [source, *(Path(str(source) + suffix) for suffix in ('-wal', '-shm', '-journal'))]:
        if candidate.exists() or candidate.is_symlink():
            if not stat.S_ISREG(candidate.lstat().st_mode):
                raise BackupError('Database or SQLite sidecar is not a regular file')
    if not source.is_file():
        raise BackupError('Existing source database required')
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    deadline = time.monotonic() + 60

    def progress(*unused):
        if time.monotonic() > deadline:
            raise BackupError('Database snapshot exceeded its bounded time limit')

    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)) as original:
        with closing(sqlite3.connect(destination)) as target:
            original.backup(target, pages=256, progress=progress, sleep=0.05)
    descriptor = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    check_database(destination)
    digest, size = safe.digest_file(destination)
    return {'sha256': digest, 'size': size}


def component(source, entries, destination):
    destination.mkdir(mode=0o700)
    archive = destination / 'vault.tar.gz'
    safe.write_archive(source, entries, archive)
    digest, size = safe.digest_file(archive)
    manifest = {'format': safe.FORMAT, 'complete': True,
                'source': str(source), 'created_at': datetime.now(timezone.utc).isoformat(),
                'entries': entries, 'archive': {'name': 'vault.tar.gz', 'sha256': digest, 'size': size}}
    safe.write_json(destination / 'manifest.json', manifest)
    safe.verify(destination)
    safe.fsync_dir(destination)
    manifest_digest, manifest_size = safe.digest_file(destination / 'manifest.json')
    return {'manifest_sha256': manifest_digest, 'manifest_size': manifest_size}


def verify(directory):
    directory = safe.safe_path(directory)
    manifest_path = directory / 'manifest.json'
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BackupError('Complete service backup manifest required')
    try:
        manifest = json.loads(manifest_path.read_bytes())
        if (manifest['format'] != FORMAT or manifest['complete'] is not True
                or set(manifest['sources']) != {'payload_database', 'payload_media', 'workbench'}
                or set(manifest['databases']) != set(DATABASES)
                or set(manifest['components']) != set(COMPONENTS)):
            raise ValueError()
        for source in manifest['sources'].values():
            safe.safe_path(source)
        for name, expected in manifest['databases'].items():
            if safe.digest_file(directory / name) != (expected['sha256'], expected['size']):
                raise BackupError('Database backup SHA256 or size mismatch')
            check_database(directory / name)
        for name, source_key in COMPONENTS.items():
            safe.safe_path(directory / name)
            expected = manifest['components'][name]
            if safe.digest_file(directory / name / 'manifest.json') != (expected['manifest_sha256'], expected['manifest_size']):
                raise BackupError('File archive manifest SHA256 or size mismatch')
            nested = safe.verify(directory / name)
            if nested['source'] != manifest['sources'][source_key]:
                raise BackupError('File archive source does not match the service manifest')
        return manifest
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise BackupError('Invalid service backup manifest') from exc


class Backups:
    def __init__(self, payload_database=DEFAULT_DATABASE, media=DEFAULT_MEDIA,
                 workbench=DEFAULT_WORKBENCH, root=DEFAULT_ROOT):
        self.database, self.media, self.workbench, self.root = map(
            safe.safe_path, (payload_database, media, workbench, root))
        self.sources = {'payload_database': str(self.database), 'payload_media': str(self.media),
                        'workbench': str(self.workbench)}
        if any(safe.overlaps(self.root, source) for source in (self.database, self.media, self.workbench)):
            raise BackupError('Backup root must be separate from all service sources')

    def create(self, keep=14, attempts=3):
        if keep < 1 or not 1 <= attempts <= 10:
            raise BackupError('keep must be positive; attempts must be between 1 and 10')
        if not self.media.is_dir() or not self.workbench.is_dir():
            raise BackupError('Existing media and workspace directories required')
        safe.private_dir(self.root)
        with safe.file_lock(self.root / '.backup.lock'):
            for attempt in range(1, attempts + 1):
                staging = Path(tempfile.mkdtemp(prefix='.partial-', dir=self.root))
                try:
                    media_before = files_snapshot(self.media)
                    workbench_before = files_snapshot(self.workbench, workbench=True)
                    databases = {'payload.sqlite3': copy_database(self.database, staging / 'payload.sqlite3'),
                                 'workbench.sqlite3': copy_database(self.workbench / 'state.sqlite3', staging / 'workbench.sqlite3')}
                    components = {'payload-media': component(self.media, media_before, staging / 'payload-media'),
                                  'workbench-files': component(self.workbench, workbench_before, staging / 'workbench-files')}
                    if (media_before != files_snapshot(self.media)
                            or workbench_before != files_snapshot(self.workbench, workbench=True)):
                        raise BackupError('Service files changed during backup')
                    manifest = {'format': FORMAT, 'complete': True, 'sources': self.sources,
                                'created_at': datetime.now(timezone.utc).isoformat(),
                                'databases': databases, 'components': components}
                    safe.write_json(staging / 'manifest.json', manifest)
                    verify(staging)
                    safe.fsync_dir(staging)
                    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                    destination = self.root / f'services-{stamp}-{uuid4().hex[:8]}'
                    staging.rename(destination)
                    safe.fsync_dir(self.root)
                    pruned = self.prune(keep)
                    return {'state': 'complete', 'backup': str(destination), 'attempts': attempt,
                            'media_files': sum(v['type'] == 'file' for v in media_before.values()),
                            'workspace_files': sum(v['type'] == 'file' for v in workbench_before.values()),
                            'databases_verified': 2, 'pruned': pruned}
                except (BackupError, OSError, sqlite3.Error, safe.tarfile.TarError):
                    pass
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            raise BackupError(f'No consistent service backup after {attempts} attempts; previous successful backups retained')

    def verified_backups(self):
        valid, invalid = [], 0
        if not self.root.exists():
            return valid, invalid
        for directory in self.root.iterdir():
            if not NAME.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                continue
            try:
                if verify(directory)['sources'] == self.sources:
                    valid.append(directory)
            except (BackupError, OSError, sqlite3.Error, safe.tarfile.TarError):
                invalid += 1
        return sorted(valid, reverse=True), invalid

    def prune(self, keep):
        valid, _ = self.verified_backups()
        removed = 0
        for directory in valid[keep:]:
            if {entry.name for entry in directory.iterdir()} != EXPECTED:
                continue
            if any({p.name for p in (directory / name).iterdir()} != {'manifest.json', 'vault.tar.gz'}
                   for name in COMPONENTS):
                continue
            shutil.rmtree(directory)
            removed += 1
        safe.fsync_dir(self.root)
        return removed

    def status(self):
        valid, invalid = self.verified_backups()
        return {'state': 'verified' if valid else 'no-valid-backups', 'complete_backups': len(valid),
                'invalid_backups': invalid, 'latest': str(valid[0]) if valid else None}


def restore(directory, destination=None, apply=False):
    directory = safe.safe_path(directory)
    manifest = verify(directory)
    result = {'state': 'preview', 'backup': str(directory), 'databases_verified': 2,
              'services_started': False}
    if destination is not None:
        destination = safe.safe_path(destination)
        if (destination.exists() or not destination.parent.is_dir()
                or safe.overlaps(destination, directory.parent)
                or any(safe.overlaps(destination, safe.safe_path(source)) for source in manifest['sources'].values())):
            raise BackupError('Restore requires a new separate directory with an existing parent')
        result['destination'] = str(destination)
    if not apply:
        return result
    if destination is None:
        raise BackupError('--destination is required with --apply')
    destination.mkdir(mode=0o700)
    try:
        (destination / 'payload').mkdir(mode=0o700)
        (destination / 'payload/database').mkdir(mode=0o700)
        safe.restore(directory / 'payload-media', destination / 'payload/media', apply=True)
        safe.restore(directory / 'workbench-files', destination / 'workbench', apply=True)
        for name, relative in [('payload.sqlite3', 'payload/database/payload.db'),
                               ('workbench.sqlite3', 'workbench/state.sqlite3')]:
            target = destination / relative
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            source_descriptor = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(source_descriptor, 'rb') as source, os.fdopen(descriptor, 'wb') as output:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            expected = manifest['databases'][name]
            if safe.digest_file(target) != (expected['sha256'], expected['size']):
                raise BackupError('Restored database SHA256 mismatch')
            check_database(target)
        safe.fsync_dir(destination)
        safe.fsync_dir(destination.parent)
    except Exception:
        shutil.rmtree(destination)
        raise
    return {**result, 'state': 'restored'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup-root', type=Path, default=DEFAULT_ROOT)
    commands = parser.add_subparsers(dest='action', required=True)
    create = commands.add_parser('create')
    create.add_argument('--keep', type=int, default=14)
    create.add_argument('--attempts', type=int, default=3)
    commands.add_parser('status')
    for name in ('verify', 'restore'):
        command = commands.add_parser(name)
        command.add_argument('backup', type=Path)
        if name == 'restore':
            command.add_argument('--destination', type=Path)
            command.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == 'create':
            result = Backups(root=args.backup_root).create(args.keep, args.attempts)
        elif args.action == 'status':
            result = Backups(root=args.backup_root).status()
        elif args.action == 'restore':
            result = restore(args.backup, args.destination, args.apply)
        else:
            verify(args.backup)
            result = {'state': 'verified', 'databases_verified': 2}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        # SQLite errors can contain private schema/row detail; never log them.
        error = str(exc) if isinstance(exc, BackupError) else f'Operation failed ({type(exc).__name__}); inspect locally'
        print(json.dumps({'state': 'failed', 'error': error}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
