import json
from pathlib import Path
import tempfile
import unittest

from markdown_library.core import Library, LibraryError
from markdown_library.maintenance import maintain


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = Library.initialize(self.root / 'vault')
        (self.root / 'source.md').write_text('Initial instructions\n')
        (self.root / 'behavior.py').write_text('version = 1\n')
        self.config = {'version': 1, 'files': [{'source': 'source.md', 'target': 'AGENTS.md'}],
                       'groups': [{'name': 'rules', 'sources': ['source.md', 'behavior.py'],
                                   'review': ['templates/面试复盘.md'], 'checks': ['review related implementation']}]}
        self.save_manifest()

    def save_manifest(self):
        (self.root / 'manifest.json').write_text(json.dumps(self.config))

    def run_plan(self, **kwargs):
        return maintain(self.library, project=self.root, manifest='manifest.json', **kwargs)

    def apply_current(self, **kwargs):
        return self.run_plan(apply=True, expected_token=self.run_plan()['token'], **kwargs)

    def test_preview_writes_nothing_and_propagates_only_after_review(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        plan = self.run_plan()
        self.assertFalse(plan['current'])
        self.assertEqual(plan['impact_review'][0]['review'], ['templates/面试复盘.md'])
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.apply_current()
        self.assertTrue(self.run_plan()['current'])
        (self.root / 'source.md').write_text('Updated instructions\n')
        self.assertEqual(self.run_plan()['files'][0]['status'], 'update')
        self.apply_current()
        self.assertEqual(self.library._path('AGENTS.md').read_text(), 'Updated instructions\n')

    def test_local_edits_are_retained_until_reviewed_and_old_copy_is_recoverable(self):
        self.apply_current()
        target = self.library._path('AGENTS.md')
        target.write_text('User local edit\n')
        (self.root / 'source.md').write_text('New server rule\n')
        plan = self.run_plan()
        self.assertEqual(plan['files'][0]['status'], 'review-required')
        with self.assertRaises(LibraryError):
            self.run_plan(apply=True, expected_token=plan['token'])
        self.assertEqual(target.read_text(), 'User local edit\n')
        (self.root / 'source.md').write_text('Merged server rule and local edit\n')
        self.apply_current(accept_reviewed=True)
        history = self.library._path('.markdown-library/maintenance-history')
        self.assertIn('User local edit\n', [p.read_text() for p in history.glob('*.md')])

    def test_source_target_and_manifest_changes_invalidate_preview(self):
        self.apply_current()
        for kind in ('source', 'target', 'manifest', 'implementation'):
            plan = self.run_plan()
            if kind == 'source':
                (self.root / 'source.md').write_text('Next source\n')
            elif kind == 'target':
                self.library._path('AGENTS.md').write_text('Next local\n')
            elif kind == 'manifest':
                self.config['groups'][0]['review'].append('extra dependency')
                self.save_manifest()
            else:
                (self.root / 'behavior.py').write_text('version = 2\n')
            with self.assertRaises(LibraryError):
                self.run_plan(apply=True, expected_token=plan['token'], accept_reviewed=True)
            self.apply_current(accept_reviewed=True)

    def test_implementation_change_requires_review_even_when_documents_match(self):
        self.apply_current()
        (self.root / 'behavior.py').write_text('version = 2\n')
        plan = self.run_plan()
        self.assertEqual(plan['files'][0]['status'], 'current')
        self.assertFalse(plan['current'])
        self.assertEqual(len(plan['impact_review']), 1)
        (self.root / 'behavior.py').write_text('version = 3\n')
        with self.assertRaises(LibraryError):
            self.run_plan(apply=True, expected_token=plan['token'])

    def test_idea_pool_and_topic_directory_are_maintained_utility_targets(self):
        for target in ('05_想法池/索引.md', '笔记目录.md'):
            self.config['files'] = [{'source': 'source.md', 'target': target}]
            self.save_manifest()
            self.apply_current()
            self.assertEqual(self.library._path(target).read_text(), 'Initial instructions\n')

    def test_pending_client_feedback_stays_visible_and_is_never_auto_adopted(self):
        self.apply_current()
        target = self.library._path('config/规则变更待同步.md')
        target.parent.mkdir(exist_ok=True)
        target.write_text('User-confirmed request, awaiting server implementation\n')
        result = self.apply_current()
        self.assertTrue(result['pending_feedback'])
        self.assertFalse(result['current'])
        self.assertIn('awaiting server', target.read_text())

    def test_rejects_regular_notes_and_escaping_sources(self):
        for entry in ({'source': 'source.md', 'target': '10_个人记录/Note.md'},
                      {'source': '../secret.md', 'target': 'AGENTS.md'}):
            self.config['files'] = [entry]
            self.save_manifest()
            with self.assertRaises(LibraryError):
                self.run_plan()

    def test_policy_projection_excludes_history_and_resolves_offline_references(self):
        (self.root / 'source.md').write_text('# Rules\n\n[Server guide](guide.md) and [web](https://example.org).\n'
                                             '\n## 服务器附注\n\nServer-only details\n')
        self.config['files'][0]['render'] = 'policy'
        self.config['files'][0]['target'] = 'config/整理与发表规约.md'
        self.save_manifest()
        self.apply_current()
        text = self.library._path('config/整理与发表规约.md').read_text()
        self.assertIn('Server guide and [web](https://example.org)', text)
        self.assertNotIn('Server-only details', text)
