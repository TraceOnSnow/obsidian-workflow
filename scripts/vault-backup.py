#!/usr/bin/env python3
"""Private complete Vault backups, verified restores and managed-note checkpoints."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import tempfile
from uuid import uuid4


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_VAULT = PROJECT.parent / 'personal-records-vault'
DEFAULT_BACKUPS = PROJECT / 'runtime/vault-backups'
FORMAT = 'markdown-vault-backup-v1'
BACKUP_NAME = re.compile(r'vault-\d{8}T\d{12}Z-[a-f0-9]{8}')


class BackupError(Exception):
    pass


def safe_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or '..' in path.parts or path == Path('/'):
        raise BackupError('An absolute non-root path without .. is required')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise BackupError('Symlink paths are not allowed')
    return path


def overlaps(left, right):
    return left == right or left in right.parents or right in left.parents


def private_dir(path):
    safe_path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.stat().st_uid != os.getuid():
        raise BackupError('Private directory must be owned by the current user')
    path.chmod(0o700)


def digest_stream(stream):
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def digest_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise BackupError('Only regular files can be archived')
        result = digest_stream(stream)
        after = os.fstat(stream.fileno())
        fields = ('st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(before, field) != getattr(after, field) for field in fields):
            raise BackupError('Vault changed while reading a file')
        return result


def snapshot(vault):
    """Include all hidden contents; never follow a link outside the Vault."""
    found = {}
    for directory, names, files in os.walk(vault, followlinks=False):
        for name in sorted(names + files):
            path = Path(directory) / name
            mode = path.lstat().st_mode
            relative = path.relative_to(vault).as_posix()
            valid_member(relative)
            if stat.S_ISDIR(mode):
                found[relative] = {'type': 'directory'}
            elif stat.S_ISREG(mode):
                digest, size = digest_file(path)
                found[relative] = {'type': 'file', 'sha256': digest, 'size': size}
            else:
                raise BackupError('Vault contains a symlink or special file; nothing was archived')
    return dict(sorted(found.items()))


def valid_member(name):
    if not isinstance(name, str):
        raise BackupError('Invalid archive path')
    path = PurePosixPath(name)
    if (not name or '\\' in name or '\0' in name or path.is_absolute()
            or '..' in path.parts or str(path) != name or name == '.'):
        raise BackupError('Unsafe archive path')
    return name


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def file_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as stream:
        os.fchmod(stream.fileno(), 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def library_for(vault):
    sys.path.insert(0, str(PROJECT / 'src'))
    from markdown_library import Library
    return Library(vault).validate()


def write_archive(vault, entries, destination):
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as output:
        with tarfile.open(fileobj=output, mode='w:gz', format=tarfile.PAX_FORMAT) as archive:
            for name, entry in entries.items():
                info = tarfile.TarInfo(name)
                info.mode = 0o700 if entry['type'] == 'directory' else 0o600
                if entry['type'] == 'directory':
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                else:
                    info.size = entry['size']
                    descriptor = os.open(vault / name, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(descriptor, 'rb') as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise BackupError('Vault file changed type during backup')
                        archive.addfile(info, stream)
        output.flush()
        os.fsync(output.fileno())


def read_manifest(directory):
    directory = safe_path(directory)
    if not directory.is_dir():
        raise BackupError('Backup directory does not exist')
    manifest_path = directory / 'manifest.json'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise BackupError('Backup has no valid manifest')
    try:
        manifest = json.loads(manifest_path.read_bytes())
        if (manifest['format'] != FORMAT or manifest['complete'] is not True
                or not isinstance(manifest['entries'], dict)
                or manifest['archive']['name'] != 'vault.tar.gz'
                or not re.fullmatch('[a-f0-9]{64}', manifest['archive']['sha256'])
                or type(manifest['archive']['size']) is not int):
            raise ValueError()
        safe_path(manifest['source'])
        for name, entry in manifest['entries'].items():
            valid_member(name)
            if entry['type'] == 'file':
                if (type(entry['size']) is not int or entry['size'] < 0
                        or not re.fullmatch('[a-f0-9]{64}', entry['sha256'])):
                    raise ValueError()
            elif entry['type'] != 'directory':
                raise ValueError()
            parent = str(PurePosixPath(name).parent)
            if parent != '.' and manifest['entries'].get(parent) != {'type': 'directory'}:
                raise ValueError()
        return manifest
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise BackupError('Backup manifest is invalid') from exc


def inspect_archive(directory, manifest, restore_to=None):
    archive_path = directory / 'vault.tar.gz'
    expected = manifest['archive']
    if digest_file(archive_path) != (expected['sha256'], expected['size']):
        raise BackupError('Archive SHA256 or size mismatch')
    found = set()
    with tarfile.open(archive_path, mode='r:gz') as archive:
        for member in archive:
            name = valid_member(member.name)
            if name in found or name not in manifest['entries']:
                raise BackupError('Duplicate or unlisted archive member')
            found.add(name)
            entry = manifest['entries'][name]
            # Tar extraction helpers are deliberately not used: links/devices and
            # path traversal are rejected even on Python versions before 3.14.
            if entry['type'] == 'directory':
                if not member.isdir():
                    raise BackupError('Archive member type mismatch')
                if restore_to:
                    (restore_to / name).mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                if not member.isfile() or member.size != entry['size']:
                    raise BackupError('Archive member type or size mismatch')
                with archive.extractfile(member) as stream:
                    if restore_to:
                        output_path = restore_to / name
                        output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(fd, 'wb') as output:
                            digest = hashlib.sha256()
                            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                                digest.update(chunk)
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                        actual = digest.hexdigest()
                    else:
                        actual = digest_stream(stream)[0]
                if actual != entry['sha256']:
                    raise BackupError('Archive member SHA256 mismatch')
    if found != set(manifest['entries']):
        raise BackupError('Archive is missing manifest members')
    if digest_file(archive_path) != (expected['sha256'], expected['size']):
        raise BackupError('Archive changed during verification')


def verify(directory):
    directory = safe_path(directory)
    manifest = read_manifest(directory)
    inspect_archive(directory, manifest)
    return manifest


class Backups:
    def __init__(self, vault=DEFAULT_VAULT, root=DEFAULT_BACKUPS):
        self.vault, self.root = safe_path(vault), safe_path(root)
        if overlaps(self.vault, self.root):
            raise BackupError('Backup root must be separate from the Vault')

    def create(self, keep=14, attempts=3):
        if keep < 1 or attempts < 1 or attempts > 10:
            raise BackupError('keep must be positive; attempts must be between 1 and 10')
        if not self.vault.is_dir():
            raise BackupError('Vault directory does not exist')
        private_dir(self.root)
        with file_lock(self.root / '.backup.lock'):
            # Cooperating writers stop here. Headless/Obsidian/Git writers are
            # detected by comparing before, archived and after content hashes.
            library = library_for(self.vault) if (self.vault / '.markdown-library').exists() else None
            failure = None
            for attempt in range(1, attempts + 1):
                staging = Path(tempfile.mkdtemp(prefix='.partial-', dir=self.root))
                try:
                    with file_lock(library._path('.markdown-library/lock')) if library else nullcontext():
                        before = snapshot(self.vault)
                        write_archive(self.vault, before, staging / 'vault.tar.gz')
                        after = snapshot(self.vault)
                        if before != after:
                            raise BackupError('Vault changed during backup; retrying')
                        digest, size = digest_file(staging / 'vault.tar.gz')
                        manifest = {'format': FORMAT, 'complete': True,
                                    'created_at': datetime.now(timezone.utc).isoformat(),
                                    'source': str(self.vault),
                                    'archive': {'name': 'vault.tar.gz', 'sha256': digest, 'size': size},
                                    'entries': before}
                        write_json(staging / 'manifest.json', manifest)
                        verify(staging)
                    fsync_dir(staging)
                    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                    destination = self.root / f'vault-{stamp}-{uuid4().hex[:8]}'
                    staging.rename(destination)
                    fsync_dir(self.root)
                    removed = self._prune(keep)
                    return {'state': 'complete', 'backup': str(destination), 'files':
                            sum(e['type'] == 'file' for e in before.values()),
                            'archive_sha256': digest, 'attempts': attempt, 'pruned': removed}
                except (BackupError, OSError, tarfile.TarError) as exc:
                    failure = exc
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            raise BackupError(f'No consistent backup after {attempts} attempts; previous backups retained') from failure

    def _prune(self, keep):
        candidates = []
        for directory in self.root.iterdir():
            if not BACKUP_NAME.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                continue
            try:
                manifest = verify(directory)
                if manifest['source'] == str(self.vault):
                    candidates.append(directory)
            except (BackupError, OSError, tarfile.TarError):
                continue
        removed = 0
        for directory in sorted(candidates, reverse=True)[keep:]:
            # Never remove extra user files accidentally placed in an archive dir.
            if {p.name for p in directory.iterdir()} == {'manifest.json', 'vault.tar.gz'}:
                for name in ('manifest.json', 'vault.tar.gz'):
                    (directory / name).unlink()
                directory.rmdir()
                removed += 1
        fsync_dir(self.root)
        return removed

    def status(self):
        if not self.root.exists():
            return {'state': 'no-backups', 'complete_backups': 0}
        valid, invalid = [], 0
        for directory in self.root.iterdir():
            if BACKUP_NAME.fullmatch(directory.name):
                try:
                    manifest = verify(directory)
                    if manifest['source'] == str(self.vault):
                        valid.append(directory)
                except (BackupError, OSError, tarfile.TarError):
                    invalid += 1
        return {'state': 'verified' if valid else 'no-valid-backups',
                'complete_backups': len(valid), 'invalid_backups': invalid,
                'latest': str(max(valid)) if valid else None}


def restore(directory, destination=None, apply=False):
    directory = safe_path(directory)
    manifest = verify(directory)
    result = {'state': 'preview', 'source': str(directory), 'entries': len(manifest['entries']),
              'archive_sha256': manifest['archive']['sha256']}
    if destination is not None:
        destination = safe_path(destination)
        source = safe_path(manifest['source'])
        if (destination.exists() or overlaps(destination, source)
                or overlaps(destination, directory.parent) or not destination.parent.is_dir()):
            raise BackupError('Restore requires a new separate directory with an existing parent')
        result['destination'] = str(destination)
    if not apply:
        return result
    if destination is None:
        raise BackupError('--destination is required with --apply')
    # mkdir(exist_ok=False) reserves the final path without an overwrite race.
    # No live Vault is overwritten; failed restores remove only this new tree.
    destination.mkdir(mode=0o700)
    try:
        inspect_archive(directory, manifest, destination)
        if snapshot(destination) != manifest['entries']:
            raise BackupError('Restored content does not match the manifest')
        fsync_dir(destination)
        fsync_dir(destination.parent)
    except Exception:
        shutil.rmtree(destination)
        raise
    return {**result, 'state': 'restored', 'connected_to_sync': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, default=DEFAULT_VAULT)
    parser.add_argument('--backup-root', type=Path, default=DEFAULT_BACKUPS)
    commands = parser.add_subparsers(dest='action', required=True)
    create = commands.add_parser('create')
    create.add_argument('--keep', type=int, default=14)
    create.add_argument('--attempts', type=int, default=3)
    commands.add_parser('status')
    commands.add_parser('checkpoint')
    check = commands.add_parser('verify')
    check.add_argument('backup', type=Path)
    recovering = commands.add_parser('restore')
    recovering.add_argument('backup', type=Path)
    recovering.add_argument('--destination', type=Path)
    recovering.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == 'create':
            result = Backups(args.vault, args.backup_root).create(args.keep, args.attempts)
        elif args.action == 'status':
            result = Backups(args.vault, args.backup_root).status()
        elif args.action == 'verify':
            manifest = verify(args.backup)
            result = {'state': 'verified', 'entries': len(manifest['entries'])}
        elif args.action == 'restore':
            result = restore(args.backup, args.destination, args.apply)
        else:
            notes = library_for(safe_path(args.vault)).list_notes()
            result = {'state': 'checkpointed', 'managed_notes': len(notes)}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Library parser exceptions may contain private note fragments. Logs
        # expose only our own bounded errors and the exception class otherwise.
        message = str(exc) if isinstance(exc, BackupError) else f'Operation failed ({type(exc).__name__}); inspect locally'
        print(json.dumps({'state': 'failed', 'error': message}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
