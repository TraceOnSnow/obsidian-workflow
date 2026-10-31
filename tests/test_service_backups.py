from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('service_backup', Path(__file__).parents[1] / 'scripts/service-backup.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class ServiceBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.media = self.base / 'homepage/data/media'
        self.media.mkdir(parents=True)
        (self.media / 'empty').mkdir()
        (self.media / 'image.bin').write_bytes(b'synthetic-media\0')
        self.database = self.base / 'homepage/data/database/payload.db'
        self.database.parent.mkdir()
        self.workbench = self.base / 'pipeline/runtime/workbench'
        self.workbench.mkdir(parents=True)
        (self.workbench / 'config.json').write_text('{"budget_usd":"2"}')
        (self.workbench / 'jobs/job/selection').mkdir(parents=True)
        (self.workbench / 'jobs/job/selection/response.raw').write_text('synthetic private response')
        (self.workbench / '.hidden').write_text('internal history')
        self.writers = []
        for filename in [self.database, self.workbench / 'state.sqlite3']:
            connection = sqlite3.connect(filename)
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA wal_autocheckpoint=0')
            connection.execute('CREATE TABLE records (id INTEGER PRIMARY KEY, text TEXT)')
            connection.execute("INSERT INTO records VALUES (1,'initial')")
            connection.commit()
            # Keep the writer open and the latest committed content in WAL.
            connection.execute("INSERT INTO records VALUES (2,'latest committed WAL content')")
            connection.commit()
            self.writers.append(connection)
            self.addCleanup(connection.close)
        self.manager = backup.Backups(self.database, self.media, self.workbench, self.base / 'backups')

    def create(self, **options):
        return Path(self.manager.create(**options)['backup'])

    def test_wal_safe_complete_backup_and_independent_restore(self):
        self.assertTrue(Path(str(self.database) + '-wal').stat().st_size)
        directory = self.create()
        manifest = backup.verify(directory)
        for filename in ('payload.sqlite3', 'workbench.sqlite3'):
            with closing(sqlite3.connect((directory / filename).as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
                self.assertEqual(db.execute('SELECT text FROM records WHERE id=2').fetchone()[0], 'latest committed WAL content')
        workspace_manifest = backup.safe.verify(directory / 'workbench-files')
        self.assertIn('.hidden', workspace_manifest['entries'])
        self.assertNotIn('state.sqlite3', workspace_manifest['entries'])
        self.assertNotIn('state.sqlite3-wal', workspace_manifest['entries'])
        destination = self.base / 'recovered'
        self.assertEqual(backup.restore(directory, destination)['state'], 'preview')
        self.assertFalse(destination.exists())
        self.assertEqual(backup.restore(directory, destination, True)['state'], 'restored')
        self.assertEqual((destination / 'payload/media/image.bin').read_bytes(), b'synthetic-media\0')
        self.assertEqual((destination / 'workbench/jobs/job/selection/response.raw').read_text(), 'synthetic private response')
        self.assertTrue((destination / 'payload/media/empty').is_dir())
        for path in directory.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
        for path in destination.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        self.assertEqual((destination / 'workbench/state.sqlite3').stat().st_mode & 0o777, 0o600)
        self.assertEqual(manifest['format'], backup.FORMAT)

    def test_external_configuration_credentials_and_exports_never_opened(self):
        secret = self.base / 'homepage/runtime.env'
        secret.write_text('SYNTHETIC_SECRET')
        (self.base / 'exports').mkdir()
        (self.base / 'exports/raw.json').write_text('raw source outside the workspace')
        original = backup.safe.digest_file

        def guarded(filename):
            self.assertNotEqual(Path(filename), secret)
            self.assertNotIn(self.base / 'exports', Path(filename).parents)
            return original(filename)

        with patch.object(backup.safe, 'digest_file', side_effect=guarded):
            directory = self.create()
        for component in backup.COMPONENTS:
            with backup.safe.tarfile.open(directory / component / 'vault.tar.gz') as archive:
                for member in archive:
                    if member.isfile():
                        self.assertNotIn(b'SYNTHETIC_SECRET', archive.extractfile(member).read())

    def test_activity_retries_without_altering_any_job_or_budget(self):
        original = backup.component
        changed = False
        before = self.writers[1].execute('SELECT * FROM records').fetchall()

        def racing(source, entries, destination):
            nonlocal changed
            result = original(source, entries, destination)
            if source == self.workbench and not changed:
                (source / 'jobs/new-response.raw').write_text('completed by the existing worker')
                changed = True
            return result

        with patch.object(backup, 'component', side_effect=racing):
            result = self.manager.create()
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(before, self.writers[1].execute('SELECT * FROM records').fetchall())
        self.assertEqual(json.loads((self.workbench / 'config.json').read_text())['budget_usd'], '2')

    def test_failed_snapshot_retains_previous_verified_backup(self):
        directory = self.create()
        digest = backup.safe.digest_file(directory / 'payload.sqlite3')
        with patch.object(backup, 'copy_database', side_effect=sqlite3.OperationalError('private schema detail')):
            with self.assertRaises(backup.BackupError):
                self.manager.create(attempts=2, keep=1)
        self.assertEqual(backup.safe.digest_file(directory / 'payload.sqlite3'), digest)
        self.assertEqual(self.manager.status()['complete_backups'], 1)
        self.assertFalse(list(self.manager.root.glob('.partial-*')))

    def test_retention_preserves_unknown_invalid_and_extra_user_files(self):
        first = self.create()
        unknown = self.manager.root / 'manual-backup'
        unknown.mkdir()
        invalid = self.manager.root / 'services-20000101T000000000000Z-11111111'
        invalid.mkdir()
        (invalid / 'manifest.json').write_text('{}')
        extra = self.create()
        (extra / 'operator-note').write_text('preserve')
        latest = self.create(keep=1)
        self.assertFalse(first.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(invalid.exists())
        self.assertTrue(extra.exists())
        self.assertTrue(latest.exists())

    def test_symlinks_special_files_and_credential_filename_are_rejected(self):
        outside = self.base / 'external-token'
        outside.write_text('SYNTHETIC_SECRET')
        link = self.media / 'linked-secret'
        link.symlink_to(outside)
        with self.assertRaises(backup.BackupError):
            self.manager.create(attempts=1)
        link.unlink()
        os.mkfifo(self.workbench / 'pipe')
        with self.assertRaises(backup.BackupError):
            self.manager.create(attempts=1)
        (self.workbench / 'pipe').unlink()
        (self.workbench / 'runtime.env').write_text('must not be opened')
        with self.assertRaises(backup.BackupError):
            self.manager.create(attempts=1)

    def test_source_database_symlink_is_rejected(self):
        self.writers[0].close()
        real = self.database.with_name('real.db')
        self.database.rename(real)
        self.database.symlink_to(real)
        with self.assertRaises(backup.BackupError):
            self.manager.create(attempts=1)

    def test_tampered_archive_or_database_cannot_be_restored(self):
        directory = self.create()
        with (directory / 'payload.sqlite3').open('ab') as stream:
            stream.write(b'tampered')
        destination = self.base / 'bad-recovery'
        with self.assertRaises(backup.BackupError):
            backup.restore(directory, destination, True)
        self.assertFalse(destination.exists())
        directory = self.create()
        with (directory / 'payload-media/vault.tar.gz').open('ab') as stream:
            stream.write(b'tampered')
        with self.assertRaises(backup.BackupError):
            backup.restore(directory, destination, True)
        self.assertFalse(destination.exists())

    def test_existing_live_or_backup_destination_cannot_be_overwritten(self):
        directory = self.create()
        for destination in (self.workbench, self.media / 'recovery', self.manager.root / 'recovery'):
            with self.subTest(destination=destination), self.assertRaises(backup.BackupError):
                backup.restore(directory, destination, True)

    def test_bounded_sqlite_copy_timeout(self):
        destination = self.base / 'timeout.sqlite3'
        with patch.object(backup.time, 'monotonic', side_effect=[0, 61]):
            with self.assertRaisesRegex(backup.BackupError, 'bounded time'):
                backup.copy_database(self.database, destination)

    def test_unreadable_subdirectory_cannot_be_silently_omitted(self):
        blocked = self.media / 'blocked'
        blocked.mkdir()
        (blocked / 'must-be-retained.bin').write_bytes(b'required')
        original = backup.os.scandir

        def denied(directory):
            if not isinstance(directory, int) and Path(directory) == blocked:
                raise PermissionError('synthetic protected directory')
            return original(directory)

        with patch.object(backup.os, 'scandir', side_effect=denied):
            with self.assertRaises(backup.BackupError):
                self.manager.create(attempts=1)
        self.assertFalse(list(self.manager.root.glob('services-*')))


if __name__ == '__main__':
    unittest.main()
