import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/install-super-table.py'
spec = importlib.util.spec_from_file_location('super_table_install', SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class SuperTableInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        contents = {
            'main.js': b'// synthetic test plugin\n',
            'styles.css': b'/* synthetic */\n',
            'manifest.json': json.dumps({'id': 'super-table', 'version': '1.1.2',
                                         'minAppVersion': '1.13.0'}).encode(),
        }
        self.pin = self.root / 'pin.json'
        self.pin.write_text(json.dumps({'tag': '1.1.2', 'commit': 'synthetic',
            'plugin_id': 'super-table', 'min_app_version': '1.13.0', 'assets': {
                name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                for name, data in contents.items()}}))
        for name, data in contents.items():
            (self.bundle / name).write_bytes(data)
        self.skill = self.root / 'SKILL.md'
        self.skill.write_text('Synthetic fixture skill\n')
        self.patch_pin = patch.object(installer, 'PIN', self.pin)
        self.patch_skill = patch.object(installer, 'SKILL', self.skill)
        self.patch_pin.start()
        self.patch_skill.start()
        self.addCleanup(self.patch_pin.stop)
        self.addCleanup(self.patch_skill.stop)

    def install(self, **kwargs):
        return installer.install(self.vault, self.bundle, **kwargs)

    def apply(self, **kwargs):
        preview = self.install(**kwargs)
        return self.install(apply=True, expected_token=preview['token'], **kwargs)

    def test_preview_has_no_side_effects_and_apply_is_idempotent(self):
        preview = self.install(enable=True, with_skill=True)
        self.assertEqual(list(self.vault.iterdir()), [])
        self.install(enable=True, with_skill=True, apply=True, expected_token=preview['token'])
        self.assertEqual(self.install(enable=True, with_skill=True)['changes'], [])

    def test_preserves_other_plugins_and_plugin_settings(self):
        (self.vault / '.obsidian/plugins/super-table').mkdir(parents=True)
        settings = self.vault / '.obsidian/plugins/super-table/data.json'
        settings.write_text('{"user": true}\n')
        community = self.vault / '.obsidian/community-plugins.json'
        community.write_text('["copilot", "other-plugin"]\n')
        self.apply(enable=True, with_skill=True)
        self.assertEqual(json.loads(community.read_text()), ['copilot', 'other-plugin', 'super-table'])
        self.assertEqual(settings.read_text(), '{"user": true}\n')
        self.assertTrue((self.vault / 'copilot/skills/super-table/SKILL.md').is_file())

    def test_stale_preview_refuses_after_community_change(self):
        (self.vault / '.obsidian').mkdir()
        community = self.vault / '.obsidian/community-plugins.json'
        community.write_text('["copilot"]')
        preview = self.install(enable=True)
        community.write_text('["copilot", "user-added"]')
        with self.assertRaisesRegex(installer.InstallError, 'token'):
            self.install(enable=True, apply=True, expected_token=preview['token'])
        self.assertEqual(json.loads(community.read_text()), ['copilot', 'user-added'])
        self.assertFalse((self.vault / '.obsidian/plugins').exists())

    def test_tampered_bundle_refused_without_destination_writes(self):
        (self.bundle / 'main.js').write_bytes(b'changed')
        with self.assertRaisesRegex(installer.InstallError, 'checksum'):
            self.install()
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_existing_different_skill_or_plugin_never_overwritten(self):
        for relative in ['copilot/skills/super-table/SKILL.md', '.obsidian/plugins/super-table/main.js']:
            target = self.vault / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('user modified')
            with self.assertRaisesRegex(installer.InstallError, 'retained'):
                self.install(with_skill=True)
            self.assertEqual(target.read_text(), 'user modified')
            target.unlink()

    def test_symlink_destination_or_bundle_cannot_escape(self):
        external = self.root / 'external'
        external.mkdir()
        (self.vault / '.obsidian').symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(installer.InstallError, 'Symlink'):
            self.install()
        self.assertEqual(list(external.iterdir()), [])
        (self.vault / '.obsidian').unlink()
        data = (self.bundle / 'main.js').read_bytes()
        (external / 'main.js').write_bytes(data)
        (self.bundle / 'main.js').unlink()
        (self.bundle / 'main.js').symlink_to(external / 'main.js')
        with self.assertRaisesRegex(installer.InstallError, 'Symlink'):
            self.install()

    def test_apply_requires_matching_preview_token(self):
        with self.assertRaisesRegex(installer.InstallError, 'token'):
            self.install(apply=True)
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_invalid_community_list_retained(self):
        (self.vault / '.obsidian').mkdir()
        community = self.vault / '.obsidian/community-plugins.json'
        community.write_text('{"not": "an array"}')
        with self.assertRaises(installer.InstallError):
            self.install(enable=True)
        self.assertEqual(community.read_text(), '{"not": "an array"}')
