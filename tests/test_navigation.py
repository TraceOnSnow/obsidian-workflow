"""Topic refresh must preserve independently managed Works and Post indexes."""
import json
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from markdown_library import Library

PROJECT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), PROJECT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


navigation = load_script('library-navigation')
website_index = load_script('library-index')

# Exercise the generated filter expressions, rather than duplicating the folder
# routing in a Python expected-result implementation. Bases follows JavaScript
# expression semantics; these adapters implement the documented file API only.
# This is a contract harness, not an Obsidian UI acceptance test.
BASE_EVALUATOR = r'''
const fs = require('fs');
const {configs, files} = JSON.parse(fs.readFileSync(0, 'utf8'));
String.prototype.contains = String.prototype.includes;
RegExp.prototype.matches = RegExp.prototype.test;
function filter(node, ctx) {
  if (typeof node === 'string') return !!new Function(...Object.keys(ctx), 'return (' + node + ')')(...Object.values(ctx));
  if (node.and) return node.and.every(value => filter(value, ctx));
  if (node.or) return node.or.some(value => filter(value, ctx));
  if (node.not) return !node.not.some(value => filter(value, ctx));
  throw Error('Unknown filter ' + JSON.stringify(node));
}
const result = {};
for (const [path, config] of Object.entries(configs)) {
  result[path] = files.filter(({path, properties}) => {
    const folder = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '';
    const file = {path, folder, ext: path.split('.').pop(),
      name: path.split('/').pop().replace(/\.[^.]+$/, ''),
      inFolder: value => folder === value || folder.startsWith(value + '/')};
    const ctx = {...properties, kind: properties.kind ?? null,
      title: properties.title ?? null, file,
      formula: {title: properties.title || file.name}};
    return filter(config.filters, ctx);
  }).map(file => file.path).sort();
}
process.stdout.write(JSON.stringify(result));
'''


def evaluate_bases(vault, paths, *, include_views=False):
    files = []
    for path in vault.rglob('*.md'):
        properties, _ = navigation._split(path.read_text())
        files.append({'path': path.relative_to(vault).as_posix(), 'properties': properties})
    configs = {path: yaml.safe_load((vault / path).read_text()) for path in paths}
    if include_views:
        configs = {
            f'{path}#{view["name"]}': {
                'filters': {'and': [config['filters'], view['filters']]}
                if 'filters' in view else config['filters']
            }
            for path, config in configs.items() for view in config['views']
        }
    process = subprocess.run(['node', '-e', BASE_EVALUATOR], input=json.dumps({'configs': configs, 'files': files}),
                             capture_output=True, text=True, check=True)
    return json.loads(process.stdout)


def install(vault, outputs):
    for path, content in outputs.items():
        destination = vault / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)


class NavigationTests(unittest.TestCase):
    def test_migrated_catalog_navigation_keeps_table_and_other_bases(self):
        from markdown_library.catalog_migration import migrate_work_catalog, BASE_ARCHIVE
        from markdown_library.work_catalog import DEFAULT_PATH, parse_catalog, build_catalog
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / 'vault'
            library = Library.initialize(vault)
            library.create('My own review.', title='Reviewed work', kind='work',
                           properties={'type': 'game', 'rating': 0})
            install(vault, website_index.generate(library)[0])
            post_base = vault / website_index.POST_BASE_PATH
            post_base.write_text(post_base.read_text() + '\n# My article widths and layout\n')
            topic_base = vault / '10_个人记录/实时目录.base'
            topic_base.parent.mkdir(parents=True, exist_ok=True)
            topic_base.write_text('filters: kind == "note"\nviews: []\n# My personal layout\n')
            plan = migrate_work_catalog(library)
            migrate_work_catalog(library, apply=True, expected_token=plan['token'])
            catalog_path = vault / DEFAULT_PATH
            catalog = parse_catalog(catalog_path.read_text())
            catalog.document['fields'][0]['width'] = 413
            catalog.document['views'][0]['name'] = '我的作品视图'
            catalog_path.write_text(build_catalog(catalog.document['records'], catalog))
            watched = [catalog_path, post_base, topic_base, vault / BASE_ARCHIVE]
            before = {path: path.read_bytes() for path in watched}

            outputs, counts = website_index.generate(library)
            self.assertEqual(counts['work'], 1)
            self.assertIn(f'[作品目录]({DEFAULT_PATH})', outputs['网站内容.md'])
            self.assertIn(f'![[{DEFAULT_PATH}]]', outputs['works/索引.md'])
            self.assertNotIn(website_index.BASE_PATH, outputs)
            self.assertNotIn(DEFAULT_PATH, outputs)
            self.assertNotIn('作品表格.base', outputs['works/索引.md'])
            for _ in range(2):
                subprocess.run([sys.executable, str(PROJECT / 'scripts/library-index.py'),
                                '--vault', str(vault), '--apply'], capture_output=True, text=True, check=True)
            self.assertFalse((vault / website_index.BASE_PATH).exists())
            self.assertFalse((vault / website_index.LEGACY_PATH).exists())
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)

    def test_catalog_navigation_preview_is_readonly_and_respects_configured_path(self):
        from markdown_library.work_catalog import build_catalog
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / 'vault'
            library = Library.initialize(vault)
            catalog_path = 'works/我的作品总表.md'
            state = library._read_state()
            state['work_catalog'] = {'schema_version': 1, 'path': catalog_path, 'rows': {}}
            library._atomic(catalog_path, build_catalog([]))
            library._save(state)
            # Even if a user retains an old Base, navigation must leave it alone.
            old_base = vault / website_index.BASE_PATH
            old_base.write_text('views: []\n# Manually retained old layout\n')
            before = {path.relative_to(vault).as_posix(): path.read_bytes()
                      for path in vault.rglob('*') if path.is_file()}
            outputs, _ = website_index.generate(library)
            self.assertIn(f'[作品目录]({catalog_path})', outputs['网站内容.md'])
            self.assertIn(f'![[{catalog_path}]]', outputs['works/索引.md'])
            self.assertNotIn(website_index.BASE_PATH, outputs)
            subprocess.run([sys.executable, str(PROJECT / 'scripts/library-index.py'),
                            '--vault', str(vault)], capture_output=True, text=True, check=True)
            after = {path.relative_to(vault).as_posix(): path.read_bytes()
                     for path in vault.rglob('*') if path.is_file()}
            self.assertEqual(after, before)

    def test_refresh_preserves_native_work_index_even_with_review_note(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault=Path(temporary)/'vault'
            library=Library.initialize(vault)
            library.create('User checklist',title='Review',path='works/review/Review.md')
            library.create('Private utility',title='Utility',path='发表稿/private/Utility.md')
            index=vault/'works/索引.md'
            index.write_text('<!-- markdown-library:index:v1 -->\n![[works/作品表格.base]]\n')
            base=vault/'works/作品表格.base'
            base.write_text('views:\n  - name: My layout\n    columnSize:\n      rating: 123\n')
            post_index=vault/'发表稿/索引.md'
            post_index.write_text('User post index\n')
            before={p:p.read_bytes() for p in [index,base,post_index]}
            result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'scripts/library-navigation.py'),
                '--vault',str(vault),'--apply','--adopt-existing'],capture_output=True,text=True,check=True)
            changed=json.loads(result.stdout)['changed']
            self.assertFalse(any(p.startswith(('works/','发表稿/')) for p in changed))
            for path,content in before.items(): self.assertEqual(path.read_bytes(),content)

    def test_reading_map_is_featured_without_rewriting_ordinary_notes(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault=Path(temporary)/'vault'
            library=Library.initialize(vault)
            route=library.create('Question route',title='Read here',properties={'tags':['主题导航']},
                                 path='20_项目与构想/Test/Route.md')
            ordinary=library.create('Original detail',title='Details',path='20_项目与构想/Test/Details.md')
            library.create('Only a route',title='Map only',properties={'tags':['主题导航']},
                           path='20_项目与构想/OnlyMap/Route.md')
            subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'scripts/library-navigation.py'),
                '--vault',str(vault),'--apply'],capture_output=True,text=True,check=True)
            topic=(vault/'20_项目与构想/Test/索引.md').read_text()
            self.assertLess(topic.index('Route|Read here'),topic.index('## 实时目录'))
            self.assertEqual(topic.count('Route|Read here'),1)
            self.assertIn('Route|Read here',(vault/'首页.md').read_text())
            self.assertNotIn('Details|Details',topic)
            self.assertNotIn('## 笔记',(vault/'20_项目与构想/OnlyMap/索引.md').read_text())
            self.assertEqual(library.get(route['id'])['sha256'],route['sha256'])
            self.assertEqual(library.get(ordinary['id'])['sha256'],ordinary['sha256'])

    def test_bases_follow_add_delete_move_and_rename_without_regeneration(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / 'vault'
            library = Library.initialize(vault)
            library.create('Body', title='Existing', path='10_个人记录/生活与思考/Existing.md')
            outputs = navigation.generate(vault, library.list_notes())
            install(vault, outputs)
            base = '10_个人记录/实时目录.base'
            child = '10_个人记录/生活与思考/实时目录.base'
            project = '20_项目与构想/实时目录.base'
            bases = [base, child, project, navigation.ALL_BASE, navigation.INBOX_BASE]
            before = {path: (vault / path).read_bytes() for path in bases}
            fresh = vault / '10_个人记录/生活与思考/New.md'
            fresh.write_text('A new ordinary note without identity or kind')
            rows = evaluate_bases(vault, bases)
            for path in [base, child, navigation.ALL_BASE]:
                self.assertIn(fresh.relative_to(vault).as_posix(), rows[path])
            moved = vault / '20_项目与构想/Future/Nested/Renamed.md'
            moved.parent.mkdir(parents=True)
            fresh.rename(moved)
            rows = evaluate_bases(vault, bases)
            self.assertNotIn('10_个人记录/生活与思考/New.md', rows[base])
            self.assertIn('20_项目与构想/Future/Nested/Renamed.md', rows[project])
            moved.unlink()
            self.assertNotIn('20_项目与构想/Future/Nested/Renamed.md', evaluate_bases(vault, bases)[project])
            for path, content in before.items():
                self.assertEqual((vault / path).read_bytes(), content)

    def test_inbox_untyped_nested_notes_and_root_notes_exclude_tools_indexes(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / 'vault'
            Library.initialize(vault)
            install(vault, navigation.generate(vault, []))
            for path in ['notes/New/Ordinary.md', '00_收件箱/New.md', 'Loose.md',
                         'notes/New/索引.md', 'notes/AGENTS.md', 'notes/.cache/Secret.md',
                         'copilot/skills/Tool.md', 'notes/templates/Template.md', 'works/Helper.md']:
                target = vault / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('Plain Markdown')
            rows = evaluate_bases(vault, [navigation.INBOX_BASE, navigation.ALL_BASE])
            for values in rows.values():
                self.assertEqual(set(values), {'notes/New/Ordinary.md', '00_收件箱/New.md', 'Loose.md'})

    def test_every_ordinary_folder_gets_an_independent_live_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / 'vault'
            Library.initialize(vault)
            for path in ['10_个人记录/日记/A.md', '20_项目与构想/方案/细节/B.md',
                         '30_知识与参考/算法与编程/模板/C.md', '90_归档/旧记录/D.md', '简历/面试复盘/E.md']:
                target = vault / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('Ordinary unmanaged note')
            outputs = navigation.generate(vault, [])
            install(vault, outputs)
            expected = ['10_个人记录/日记', '20_项目与构想/方案', '20_项目与构想/方案/细节',
                        '30_知识与参考/算法与编程/模板', '90_归档/旧记录', '简历/面试复盘']
            for folder in expected:
                self.assertIn(folder + '/索引.md', outputs)
                self.assertIn(folder + '/实时目录.base', outputs)
            self.assertIn('简历/索引', outputs['笔记目录.md'])
            bases = [folder + '/实时目录.base' for folder in expected]
            rows = evaluate_bases(vault, bases)
            self.assertTrue(all(len(values) == 1 for values in rows.values()))

    def test_native_post_routes_change_live_and_physical_move_wins(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / 'vault'
            library = Library.initialize(vault)
            post = library.create('Post body', title='Codeforces Round', kind='post', path='发表稿/Post.md')
            install(vault, navigation.generate(vault, library.list_notes()))
            cf = '30_知识与参考/算法与编程/Codeforces/实时目录.base'
            games = '10_个人记录/作品感想/实时目录.base'
            projects = '20_项目与构想/实时目录.base'
            bases = [cf, games, projects]
            self.assertEqual(evaluate_bases(vault, bases)[cf], ['发表稿/Post.md'])
            target = vault / '发表稿/Post.md'
            target.write_text(target.read_text().replace('title: Codeforces Round', 'title: Game impression'))
            rows = evaluate_bases(vault, bases)
            self.assertEqual(rows[cf], [])
            self.assertEqual(rows[games], ['发表稿/Post.md'])
            target.rename(vault / '20_项目与构想/Moved.md')
            rows = evaluate_bases(vault, bases)
            self.assertEqual(rows[games], [])
            self.assertEqual(rows[projects], ['20_项目与构想/Moved.md'])

    def test_all_existing_base_layouts_and_legacy_inbox_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / 'vault'
            library = Library.initialize(vault)
            install(vault, navigation.generate(vault, []))
            paths = [navigation.ALL_BASE, navigation.INBOX_BASE,
                     '00_收件箱/待整理.base', '20_项目与构想/实时目录.base', 'works/作品表格.base']
            for path in paths:
                target = vault / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('views:\n- name: User created\n  columnSize:\n    file.name: 444\n')
            before = {path: (vault / path).read_bytes() for path in paths}
            outputs = navigation.generate(vault, library.list_notes())
            self.assertTrue(set(paths).isdisjoint(outputs))
            result = subprocess.run([sys.executable, str(PROJECT / 'scripts/library-navigation.py'), '--vault', str(vault), '--apply'],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)['changed'], [])
            for path, content in before.items():
                self.assertEqual((vault / path).read_bytes(), content)

    def test_preflight_conflict_writes_no_partial_navigation(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / 'vault'
            Library.initialize(vault)
            (vault / '首页.md').write_text('User homepage')
            result = subprocess.run([sys.executable, str(PROJECT / 'scripts/library-navigation.py'), '--vault', str(vault), '--apply'],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((vault / '首页.md').read_text(), 'User homepage')
            self.assertFalse((vault / navigation.INBOX_BASE).exists())

    def test_preview_does_not_checkpoint_externally_edited_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / 'vault'
            library = Library.initialize(vault)
            note = library.create('Initial body', title='Preview')
            target = vault / note['path']
            target.write_text(target.read_text() + '\nExternal edit not yet checkpointed.\n')
            before = {path.relative_to(vault).as_posix(): path.read_bytes()
                      for path in vault.rglob('*') if path.is_file()}
            subprocess.run([sys.executable, str(PROJECT / 'scripts/library-navigation.py'), '--vault', str(vault)],
                           capture_output=True, text=True, check=True)
            after = {path.relative_to(vault).as_posix(): path.read_bytes()
                     for path in vault.rglob('*') if path.is_file()}
            self.assertEqual(after, before)

    def test_posts_native_membership_and_user_layout_survive_index_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / 'vault'
            library = Library.initialize(vault)
            library.create('Article', title='Existing article', kind='post', path='30_知识与参考/Post.md')
            outputs, _ = website_index.generate(library)
            install(vault, outputs)
            self.assertNotIn('Existing article', outputs['发表稿/索引.md'])
            self.assertIn('0–100', outputs['works/索引.md'])
            self.assertIn('85 即 85 分', outputs['works/索引.md'])
            self.assertIn('0 是有效评分', outputs['works/索引.md'])
            self.assertNotIn('0–10 的数字', outputs['works/索引.md'])
            base = website_index.POST_BASE_PATH
            fresh = vault / '发表稿/Fresh.md'
            fresh.write_text('New plain Markdown draft')
            self.assertEqual(set(evaluate_bases(vault, [base])[base]), {'发表稿/Fresh.md', '30_知识与参考/Post.md'})
            fresh.rename(vault / '发表稿/Renamed.md')
            self.assertIn('发表稿/Renamed.md', evaluate_bases(vault, [base])[base])
            (vault / '发表稿/Renamed.md').unlink()
            self.assertEqual(evaluate_bases(vault, [base])[base], ['30_知识与参考/Post.md'])
            for path in [website_index.BASE_PATH, base]:
                with (vault / path).open('a') as stream:
                    stream.write('\n# User preserved custom layout\n')
            before = {path: (vault / path).read_bytes() for path in [website_index.BASE_PATH, base]}
            result = subprocess.run([sys.executable, str(PROJECT / 'scripts/library-index.py'), '--vault', str(vault), '--apply'],
                                    capture_output=True, text=True, check=True)
            for path, content in before.items():
                self.assertEqual((vault / path).read_bytes(), content)

    def test_posts_index_exposes_publication_observation_and_archived_posts(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / 'vault'
            library = Library.initialize(vault)
            archived = library.create('Independent public copy', title='Archived post', kind='post',
                                      path='90_归档/已发表文章/Archived.md')
            original = library.create('Private original', title='Original note',
                                      path='10_个人记录/Original.md')
            outputs, _ = website_index.generate(library)
            install(vault, outputs)
            draft = vault / '发表稿/Draft.md'
            draft.write_text('Ordinary Markdown draft')
            base = website_index.POST_BASE_PATH
            members = evaluate_bases(vault, [base], include_views=True)
            self.assertEqual(members[f'{base}#全部文章'],
                             ['90_归档/已发表文章/Archived.md', '发表稿/Draft.md'])
            self.assertEqual(members[f'{base}#待写稿'], ['发表稿/Draft.md'])
            self.assertEqual(members[f'{base}#已发表存档'], ['90_归档/已发表文章/Archived.md'])
            index = outputs['发表稿/索引.md']
            self.assertLess(index.index('![[发布与同步状态#文章发表]]'), index.index(f'![[{base}]]'))
            self.assertIn('实际是否已发表以上方网站观察为准', index)
            self.assertIn('各自修改，互不自动覆盖', index)
            self.assertIn('不会撤回网站', index)

            custom_base = ('filters: kind == "post"\ncustomSetting: retained\nviews:\n'
                           '- type: table\n  name: My article layout\n  columnSize:\n'
                           '    file.name: 321\n  customViewKey: retained\n')
            (vault / base).write_text(custom_base)
            content_before = {note['path']: (vault / note['path']).read_bytes()
                              for note in [archived, original]}
            subprocess.run([sys.executable, str(PROJECT / 'scripts/library-index.py'),
                            '--vault', str(vault), '--apply'], capture_output=True, text=True, check=True)
            self.assertEqual((vault / base).read_text(), custom_base)
            for path, content in content_before.items():
                self.assertEqual((vault / path).read_bytes(), content)


if __name__ == '__main__':
    unittest.main()
