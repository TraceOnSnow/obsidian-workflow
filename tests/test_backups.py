import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from markdown_library import Library


spec = importlib.util.spec_from_file_location('vault_backup', Path(__file__).parents[1] / 'scripts/vault-backup.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class BackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.vault = self.base / 'vault'
        self.library = Library.initialize(self.vault)
        self.library.create('# Managed\nA retained draft\n', title='Managed')
        (self.vault / 'ordinary.md').write_bytes('未纳管正文\r\n'.encode())
        (self.vault / '.git/objects').mkdir(parents=True)
        (self.vault / '.git/objects/history').write_bytes(b'git-history\x00')
        (self.vault / '.obsidian').mkdir()
        (self.vault / '.obsidian/settings.json').write_text('{"unrelated": true}')
        (self.vault / 'empty directory').mkdir()
        self.manager = backup.Backups(self.vault, self.base / 'backups')

    def create(self, **kwargs):
        return Path(self.manager.create(**kwargs)['backup'])

    def test_complete_roundtrip_and_default_preview(self):
        directory = self.create()
        manifest = backup.verify(directory)
        self.assertIn('.git/objects/history', manifest['entries'])
        self.assertTrue(any(p.startswith('.markdown-library/objects/') for p in manifest['entries']))
        self.assertNotIn('login-token', manifest['entries'])
        destination = self.base / 'recovered'
        self.assertEqual(backup.restore(directory, destination)['state'], 'preview')
        self.assertFalse(destination.exists())
        result = backup.restore(directory, destination, apply=True)
        self.assertEqual(result['state'], 'restored')
        self.assertFalse(result['connected_to_sync'])
        self.assertEqual(backup.snapshot(destination), manifest['entries'])
        for path in [self.manager.root, directory, destination]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        for path in [directory / 'manifest.json', directory / 'vault.tar.gz', destination / 'ordinary.md']:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_external_credentials_never_enter_archive(self):
        (self.base / 'login-token').write_text('synthetic-secret-not-in-vault')
        directory = self.create()
        with tarfile.open(directory / 'vault.tar.gz') as archive:
            self.assertTrue(all('login-token' not in name for name in archive.getnames()))
            for item in archive:
                if item.isfile():
                    self.assertNotIn(b'synthetic-secret-not-in-vault', archive.extractfile(item).read())

    def test_noncooperating_edit_retries_and_keeps_final_revision(self):
        original = backup.write_archive
        calls = []

        def racing(vault, entries, destination):
            original(vault, entries, destination)
            calls.append(True)
            if len(calls) == 1:
                (vault / 'ordinary.md').write_text('native edit during backup')

        with patch.object(backup, 'write_archive', side_effect=racing):
            result = self.manager.create()
        self.assertEqual(result['attempts'], 2)
        restored = self.base / 'retried-recovery'
        backup.restore(result['backup'], restored, apply=True)
        self.assertEqual((restored / 'ordinary.md').read_text(), 'native edit during backup')

    def test_edit_during_read_is_detected_by_archive_member_hash(self):
        original = backup.write_archive

        def racing(vault, entries, destination):
            old = (vault / 'ordinary.md').read_bytes()
            (vault / 'ordinary.md').write_bytes(b'X' * len(old))
            original(vault, entries, destination)
            # Before/after hashes match, but the archived intermediate edit does
            # not. The independent tar member verification must reject it.
            (vault / 'ordinary.md').write_bytes(old)

        with patch.object(backup, 'write_archive', side_effect=racing):
            with self.assertRaises(backup.BackupError):
                self.manager.create(attempts=2)
        self.assertFalse(list(self.manager.root.glob('vault-*')))

    def test_failed_attempts_preserve_last_complete_backup(self):
        directory = self.create()
        original_sha = backup.digest_file(directory / 'vault.tar.gz')
        with patch.object(backup, 'write_archive', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(backup.BackupError):
                self.manager.create(keep=1, attempts=2)
        self.assertEqual(backup.digest_file(directory / 'vault.tar.gz'), original_sha)
        self.assertEqual(self.manager.status()['complete_backups'], 1)
        self.assertFalse(list(self.manager.root.glob('.partial-*')))

    def test_retention_only_removes_verified_owned_backups(self):
        first = self.create()
        malformed = self.manager.root / 'vault-20000101T000000000000Z-11111111'
        malformed.mkdir()
        (malformed / 'manifest.json').write_text('{}')
        unrelated = self.manager.root / 'manual-full-copy'
        unrelated.mkdir()
        with_user_file = self.create()
        (with_user_file / 'keep-my-file').write_text('retention must leave this tree')
        second = self.create(keep=1)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(with_user_file.exists())
        self.assertTrue(malformed.exists())
        self.assertTrue(unrelated.exists())

    def test_corruption_rejected_before_restore_creates_destination(self):
        directory = self.create()
        with (directory / 'vault.tar.gz').open('ab') as output:
            output.write(b'tampered')
        destination = self.base / 'must-not-exist'
        with self.assertRaises(backup.BackupError):
            backup.restore(directory, destination, apply=True)
        self.assertFalse(destination.exists())

    def test_restore_never_overwrites_existing_or_live_vault(self):
        directory = self.create()
        before = backup.snapshot(self.vault)
        for destination in (self.vault, self.vault / 'nested-recovery', self.manager.root / 'new'):
            with self.assertRaises(backup.BackupError):
                backup.restore(directory, destination, apply=True)
        self.assertEqual(backup.snapshot(self.vault), before)

    def test_symlinks_and_special_files_rejected_without_following(self):
        outside = self.base / 'outside'
        outside.write_text('external synthetic secret')
        (self.vault / 'link').symlink_to(outside)
        with self.assertRaises(backup.BackupError):
            self.manager.create(attempts=1)
        (self.vault / 'link').unlink()
        os.mkfifo(self.vault / 'pipe')
        with self.assertRaises(backup.BackupError):
            self.manager.create(attempts=1)

    def rewrite_tar(self, directory, members):
        archive_path = directory / 'vault.tar.gz'
        with tarfile.open(archive_path, 'w:gz') as archive:
            for info, payload in members:
                archive.addfile(info, io.BytesIO(payload) if payload is not None else None)
        manifest = json.loads((directory / 'manifest.json').read_text())
        digest, size = backup.digest_file(archive_path)
        manifest['archive'].update(sha256=digest, size=size)
        (directory / 'manifest.json').write_text(json.dumps(manifest))

    def test_malicious_tar_paths_links_duplicates_and_devices_rejected(self):
        attacks = []
        traversal = tarfile.TarInfo('../escaped')
        attacks.append([(traversal, b'')])
        absolute = tarfile.TarInfo('/tmp/escaped')
        attacks.append([(absolute, b'')])
        link = tarfile.TarInfo('ordinary.md')
        link.type, link.linkname = tarfile.SYMTYPE, '/tmp/outside'
        attacks.append([(link, None)])
        hardlink = tarfile.TarInfo('ordinary.md')
        hardlink.type, hardlink.linkname = tarfile.LNKTYPE, '/tmp/outside'
        attacks.append([(hardlink, None)])
        device = tarfile.TarInfo('ordinary.md')
        device.type = tarfile.CHRTYPE
        attacks.append([(device, None)])
        duplicate = tarfile.TarInfo('.git')
        duplicate.type = tarfile.DIRTYPE
        attacks.append([(duplicate, None), (duplicate, None)])
        for index, members in enumerate(attacks):
            with self.subTest(index=index):
                directory = self.create()
                self.rewrite_tar(directory, members)
                with self.assertRaises(backup.BackupError):
                    backup.restore(directory, self.base / f'attack-{index}', apply=True)
                self.assertFalse((self.base / f'attack-{index}').exists())

    def test_native_checkpoint_preserves_content_identity_and_sync_baseline(self):
        note = self.library.list_notes()[0]
        path = self.vault / note['path']
        edited = path.read_text() + '\nAn edit made by the native editor\n'
        path.write_text(edited)
        ordinary = (self.vault / 'ordinary.md').read_bytes()
        sync_state = self.vault / '.markdown-library/sync-state.json'
        baseline = b'{"baselines":{"sentinel":"must stay"}}'
        sync_state.write_bytes(baseline)
        observed = backup.library_for(self.vault).list_notes()
        self.assertEqual(len(observed), 1)
        self.assertEqual(path.read_text(), edited)
        self.assertEqual((self.vault / 'ordinary.md').read_bytes(), ordinary)
        self.assertEqual(sync_state.read_bytes(), baseline)
        state = json.loads((self.vault / '.markdown-library/state.json').read_text())
        history = state['notes'][note['id']]['history']
        self.assertGreaterEqual(len(history), 2)
        self.assertEqual(history[-1]['action'], 'observed')
        self.assertEqual((self.vault / '.markdown-library/objects' / (history[-1]['sha256'] + '.md')).read_text(), edited)


if __name__ == '__main__':
    unittest.main()
