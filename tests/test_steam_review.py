import unittest
import csv
import json
from pathlib import Path
import subprocess
import tempfile

from markdown_library import Library

from markdown_library.core import LibraryError, _render, _split
from markdown_library.steam_review import parse_paste, resolve_date, review, replace_last_played


class SteamReviewTests(unittest.TestCase):
    def test_partial_paste_and_absent_vs_explicit_zero(self):
        text = '''所有游戏 (1864)
A
A
总游戏时间
42 小时
最后运行日期
今天
成就
0/12
我的游戏内容
B B
B B
总游戏时间
3.5 小时
最后运行日期
2023年8月17日
我的游戏内容
'''
        entries = parse_paste(text)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]['unlocked'], 0)
        self.assertIsNone(entries[1]['unlocked'])
        self.assertEqual(entries[0]['total'], 12)
        with self.assertRaises(LibraryError):
            parse_paste(text.replace('我的游戏内容', '', 1).split('B B')[0])

    def test_reference_dates_are_explicit_and_no_future_date_is_guessed(self):
        self.assertEqual(resolve_date('今天','2026-10-24'), '2026-10-24')
        self.assertEqual(resolve_date('昨天','2026-01-01'), '2025-12-31')
        self.assertEqual(resolve_date('5月17日','2026-10-24'), '2026-05-17')
        self.assertEqual(resolve_date('2023年8月17日','2026-10-24'), '2023-08-17')
        with self.assertRaises(LibraryError):
            resolve_date('12月17日','2026-10-24')

    def test_source_ids_and_prior_achievements_prevent_false_deletion_candidates(self):
        def note(id,title):
            props={'id':id,'kind':'work','type':'game','title':title,'experiences':[]}
            return {'id':id,'properties':props,'path':f'works/{id}.md','markdown':_render(props,'')}
        notes=[note('a','Canonical'),note('b','Unknown'),note('c','No achievements')]
        rows=[{'Payload_ID':'1','游戏名称':'Steam original','原始游玩时长':'42 hours',
               '已解锁成就':'3','成就总数':'20'},
              {'Payload_ID':'3','游戏名称':'No achievements','原始游玩时长':'2 hours',
               '已解锁成就':'','成就总数':''}]
        baseline={i:{'collection':'works','remote_id':n} for i,n in [('a',1),('c',3)]}
        entries=[{'steam_title':'Steam original','playtime':'42 小时','date_raw':'今天',
                  'unlocked':None,'total':None}]
        result=review(entries,notes,rows,baseline,{},'2026-10-24')
        self.assertEqual([x['id'] for x in result['keep']], ['a'])
        self.assertEqual([x['id'] for x in result['unknown']], ['b'])
        self.assertEqual([x['id'] for x in result['candidates']], ['c'])
        with self.assertRaises(LibraryError):
            review(entries,notes,rows,baseline,{'Steam original':'b'},'2026-10-24')

    def test_duplicate_or_ambiguous_identity_does_not_guess(self):
        props={'id':'a','kind':'work','type':'game','title':'One'}
        n={'id':'a','properties':props,'path':'works/a.md','markdown':_render(props,'')}
        e={'steam_title':'One','playtime':'1 小时','date_raw':'今天','unlocked':1,'total':2}
        with self.assertRaises(LibraryError):
            review([e,e],[n],[],{}, {},'2026-10-24')
        p2={**props,'id':'b'}
        n2={'id':'b','properties':p2,'path':'works/b.md','markdown':_render(p2,'')}
        r=review([e],[n,n2],[],{}, {},'2026-10-24')
        self.assertEqual(len(r['unmatched']),1)

    def test_single_property_edit_preserves_all_other_text_and_completion(self):
        markdown='---\r\nid: a\r\nkind: work\r\ntitle: "My title" # keep\r\nexperiences:\r\n- endedOn: 2022-11\r\nstatus: completed\r\n---\r\n私人原文。\r\n'
        updated=replace_last_played(markdown,'2026-10-24')
        self.assertEqual(updated.replace("lastPlayedOn: '2026-10-24'\r\n",''),markdown)
        updated2=replace_last_played(updated,'2026-10-25')
        self.assertEqual(updated2.count('lastPlayedOn:'),1)
        self.assertEqual(replace_last_played(updated2,'2026-10-24'),updated2)
        props,body=_split(updated2)
        self.assertEqual(props['lastPlayedOn'],'2026-10-25')
        self.assertEqual(props['status'],'completed')
        self.assertEqual(props['experiences'][0]['endedOn'],'2022-11')
        self.assertEqual(body,'私人原文。\r\n')

    def test_cli_applies_only_private_date_and_preserves_checklist_on_rerun(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            lib=Library.initialize(root/'vault')
            note=lib.create('私人记录。',title='Game',kind='work',properties={'type':'game','experiences':[]})
            (lib.private/'sync-state.json').write_text(json.dumps({'baselines':{
                note['id']:{'collection':'works','remote_id':17}}}))
            source=root/'paste.txt'
            source.write_text('Game\nGame\n总游戏时间\n42 小时\n最后运行日期\n今天\n我的游戏内容\n')
            legacy=root/'legacy.csv'
            with legacy.open('w') as stream:
                writer=csv.DictWriter(stream,fieldnames=['Payload_ID','游戏名称','原始游玩时长','已解锁成就','成就总数'])
                writer.writeheader()
                writer.writerow({'Payload_ID':17,'游戏名称':'Game','原始游玩时长':'42 hours'})
            command=[__import__('sys').executable,str(Path(__file__).resolve().parents[1]/'scripts/steam-library-review.py'),
                '--vault',str(lib.vault),'--paste',str(source),'--legacy-csv',str(legacy),
                '--reference-date','2026-10-24','--output',str(root/'evidence'),'--apply']
            result=subprocess.run(command,check=True,capture_output=True,text=True)
            self.assertEqual(json.loads(result.stdout)['date_updates'],1)
            updated=lib.get(note['id'])
            self.assertNotIn('lastPlayedOn', updated['properties'])
            self.assertEqual(updated['properties']['experiences'], [{'endedOn': '2026-10-24'}])
            from markdown_library.work_profile import website_properties
            self.assertEqual(website_properties(lib, updated)['lastPlayedOn'], '2026-10-24')
            self.assertEqual(_split(updated['markdown'])[1],_split(note['markdown'])[1])
            report=root/'vault/works/整理/Steam游戏清理待确认.md'
            report.write_text(report.read_text().replace('- [ ] **保留','- [x] **保留'))
            checked=report.read_bytes()
            rerun=subprocess.run(command,check=True,capture_output=True,text=True)
            self.assertEqual(json.loads(rerun.stdout)['date_updates'],0)
            self.assertEqual(report.read_bytes(),checked)


if __name__ == '__main__':
    unittest.main()
