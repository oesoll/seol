#!/usr/bin/env python3
"""sm layout: tmux-like splits, sizes, focus and tabs kept as workspace data."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# The installed layout (session layer, peer and peer-hold in one folder): tests/run.sh assembles it in SEOL_BIN.
BIN = Path(os.environ.get('SEOL_BIN') or Path(__file__).resolve().parents[1] / 'bin')
sys.path.insert(0, str(BIN))
import sm_layout as lay  # noqa: E402


def sizes(node):
    return [round(c['size'], 3) for c in node['children']]


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='sml', dir='/tmp'))
        (self.temp / 'cfg').mkdir(); (self.temp / 'cfg/hostname').write_text('test')
        (self.temp / 'proj').mkdir()
        self.env = patch.dict(os.environ, SM_STATE=str(self.temp / 'sm'), PEER_STATE=str(self.temp / 'peer'),
                              PEER_CONFIG=str(self.temp / 'cfg'))
        self.env.start()
        self.ws = lay.ws_new('flat', self.temp / 'proj')
        self.first = self.ws['tabs'][0]['focus']
        lay.bind('flat', self.first, 'flat-main')

    def tearDown(self):
        self.env.stop(); shutil.rmtree(self.temp, ignore_errors=True)

    def tab(self, i=0):
        return lay.load('flat')['tabs'][i]

    def test_split_like_tmux(self):
        right = lay.split('flat', 'flat-main', 'right', .3, 'flat-qa')
        root = self.tab()['root']
        self.assertEqual(root['dir'], 'row')
        self.assertEqual(sizes(root), [.7, .3])
        lay.split('flat', 'flat-qa', 'down', .5, 'flat-rev')
        root = self.tab()['root']
        self.assertEqual(root['children'][1]['dir'], 'col')
        # same-direction split stays flat: three side by side, not nested
        lay.split('flat', 'flat-main', 'right', .5, 'frontend')
        root = self.tab()['root']
        self.assertEqual(len(root['children']), 3)
        self.assertEqual(sizes(root), [.35, .35, .3])
        self.assertEqual(self.tab()['focus'], self.first, 'a split must not take the focus the user is typing into')
        self.assertEqual(lay.load('flat')['panes'][right]['session'], 'flat-qa')

    def test_resize_width_and_height(self):
        lay.split('flat', 'flat-main', 'right', .5, 'flat-qa')
        lay.split('flat', 'flat-qa', 'down', .5, 'flat-rev')
        lay.resize('flat', 'flat-rev', width=.25)  # the column holding flat-rev gets 25% of the row
        root = self.tab()['root']
        self.assertEqual(sizes(root), [.75, .25])
        lay.resize('flat', 'flat-rev', height=.8)
        self.assertEqual(sizes(self.tab()['root']['children'][1]), [.2, .8])
        with self.assertRaisesRegex(ValueError, 'neighbour'):
            lay.resize('flat', 'flat-main', height=.5)

    def test_close_collapses_and_keeps_one(self):
        lay.split('flat', 'flat-main', 'right', .5, 'flat-qa')
        lay.close('flat', 'flat-qa')
        self.assertEqual(self.tab()['root'], {'pane': self.first})
        with self.assertRaisesRegex(ValueError, 'last pane'):
            lay.close('flat', 'flat-main')

    def test_swap_focus_zoom(self):
        lay.split('flat', 'flat-main', 'right', .5, 'flat-qa')
        lay.swap('flat', 'flat-main', 'flat-qa')
        order = [lay.load('flat')['panes'][p]['session'] for p in lay.leaves(self.tab()['root'])]
        self.assertEqual(order, ['flat-qa', 'flat-main'])
        lay.focus('flat', 'flat-main')
        self.assertEqual(self.tab()['focus'], self.first)
        lay.zoom('flat', 'flat-qa'); self.assertIsNotNone(self.tab()['zoom'])
        lay.zoom('flat', 'flat-qa'); self.assertIsNone(self.tab()['zoom'])

    def test_tabs_and_move(self):
        t = lay.tab_new('flat', 'workers')
        lay.bind('flat', 'workers.0', 'w1')
        lay.split('flat', 'flat-main', 'right', .5, 'w2')
        lay.move('flat', 'w2', 'workers')
        ws = lay.load('flat')
        self.assertEqual(len(lay.leaves(ws['tabs'][0]['root'])), 1)
        self.assertEqual([ws['panes'][p]['session'] for p in lay.leaves(ws['tabs'][1]['root'])], ['w1', 'w2'])
        lay.tab_move('flat', 'workers', 0)
        self.assertEqual(lay.load('flat')['tabs'][0]['name'], 'workers')
        lay.tab_close('flat', 'workers')
        self.assertEqual([x['name'] for x in lay.load('flat')['tabs']], ['main'])
        with self.assertRaisesRegex(ValueError, 'at least one'):
            lay.tab_close('flat', 'main')

    def test_one_pane_per_session_and_versions(self):
        v = lay.load('flat')['version']
        lay.split('flat', 'flat-main', 'down', .5)
        self.assertEqual(lay.load('flat')['version'], v + 1)
        with self.assertRaisesRegex(ValueError, 'already shown'):
            lay.bind('flat', 'main.1', 'flat-main')

    def test_cli_picks_workspace_from_cwd(self):
        sub = self.temp / 'proj/deep'; sub.mkdir()
        env = dict(os.environ); env.pop('SM_WORKSPACE', None)
        out = subprocess.run([sys.executable, str(BIN / 'sm-session'), 'split', 'flat-main', '--down', '--size', '0.3', '--json'],
                             cwd=sub, env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('pane', json.loads(out.stdout))
        lay_out = subprocess.run([sys.executable, str(BIN / 'sm-session'), 'layout', '--json'], cwd=sub, env=env,
                                 capture_output=True, text=True)
        self.assertEqual(json.loads(lay_out.stdout)['tabs'][0]['root']['dir'], 'col')

    def test_a_pane_id_or_name_at_host_finds_its_own_workspace(self):
        # From a pane of another workspace (SM_WORKSPACE=harness), a pane id or NAME@HOST still means that pane.
        lay.ws_new('harness', self.temp / 'proj')
        env = dict(os.environ, SM_WORKSPACE='harness')
        for ref in (self.first, 'flat-main@test'):
            out = subprocess.run([sys.executable, str(BIN / 'sm-session'), 'split', ref, '--right', '--json'],
                                 env=env, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(lay.leaves(lay.load('flat')['tabs'][0]['root'])), 3)
        self.assertEqual(len(lay.leaves(lay.load('harness')['tabs'][0]['root'])), 1)
        # a plain name stays relative to the picked workspace
        out = subprocess.run([sys.executable, str(BIN / 'sm-session'), 'split', 'flat-main', '--json'],
                             env=env, capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('in harness', out.stderr)

    def test_watch_yields_on_change(self):
        stream = lay.watch('flat', interval=.01, heartbeat=.05)
        first = next(stream)['version']
        lay.split('flat', 'flat-main', 'down', .5)
        self.assertEqual(next(stream)['version'], first + 1)
        beat = next(stream)
        self.assertTrue(beat['heartbeat'] and beat['version'] == first + 1)
        self.assertEqual(len(self.first), 2 + 16)  # p- + 8 bytes hex

    def test_border_drag_moves_only_two_panes(self):
        lay.split('flat', 'flat-main', 'right', .5, 'b')
        lay.split('flat', 'b', 'right', .5, 'c')  # [.5, .25, .25]
        lay.resize('flat', 'flat-main', width=.4, toward='next')
        self.assertEqual(sizes(self.tab()['root']), [.4, .35, .25])
        with self.assertRaisesRegex(ValueError, 'prev neighbour'):
            lay.resize('flat', 'flat-main', width=.3, toward='prev')

    def test_tab_new_with_a_session_in_one_step(self):
        t = lay.tab_new('flat', None, None, 'w9')
        ws = lay.load('flat')
        self.assertEqual(t['name'], 'w9')
        self.assertEqual(ws['panes'][t['focus']], {'session': 'w9', 'shell': False})
        with self.assertRaisesRegex(ValueError, 'already shown'):
            lay.tab_new('flat', None, None, 'flat-main')
        sub = self.temp / 'proj'
        self.session('bg1')
        out = subprocess.run([sys.executable, str(BIN / 'sm-session'), 'tab', 'new', '--attach', 'bg1', '--json'], cwd=sub,
                             capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != 'SM_WORKSPACE'})
        value = json.loads(out.stdout)
        self.assertTrue(value['tab'] and value['pane'] and value['version'])


    def test_layout_changes_leave_seols_view_unless_asked(self):
        lay.split('flat', 'flat-main', 'right', .5, 'flat-qa')
        main = lay.load('flat')['tabs'][0]['id']
        self.assertEqual(self.tab()['focus'], self.first)
        lay.tab_new('flat', 'workers', None, 'w1')
        self.assertEqual(lay.load('flat')['active_tab'], main, 'tab new pulled the user to the new tab')
        lay.move('flat', 'flat-qa', 'workers')
        self.assertEqual(lay.load('flat')['active_tab'], main, 'move pulled the user to the other tab')
        qa = lay.split('flat', 'w1', 'down', .5, 'w2', focus=True)
        ws = lay.load('flat')
        self.assertEqual(ws['active_tab'], ws['tabs'][1]['id'])
        self.assertEqual(ws['tabs'][1]['focus'], qa)

    def test_move_into_a_missing_tab_makes_it_and_a_lone_pane_takes_its_tab(self):
        lay.split('flat', 'flat-main', 'right', .5, 'w1')
        lay.move('flat', 'w1', 'workers')
        ws = lay.load('flat')
        self.assertEqual([t['name'] for t in ws['tabs']], ['main', 'workers'])
        self.assertEqual([ws['panes'][p]['session'] for p in lay.leaves(ws['tabs'][1]['root'])], ['w1'])
        lay.tab_new('flat', 'solo', None, 'w2')
        lay.move('flat', 'w2', 'workers')
        ws = lay.load('flat')
        self.assertEqual([t['name'] for t in ws['tabs']], ['main', 'workers'], 'the emptied tab should go away')
        self.assertEqual([ws['panes'][p]['session'] for p in lay.leaves(ws['tabs'][1]['root'])], ['w1', 'w2'])

    def test_arrange_like_select_layout(self):
        for name in ('a', 'b', 'c', 'd'):
            lay.split('flat', 'flat-main', 'down', .5, name)
        lay.arrange('flat', None, 'columns')
        root = self.tab()['root']
        self.assertEqual((root['dir'], len(root['children'])), ('row', 5))
        self.assertEqual(sizes(root), [.2] * 5)
        order = [lay.load('flat')['panes'][p]['session'] for p in lay.leaves(root)]
        lay.arrange('flat', 'main', 'tiled')  # 5 panes: 3 + 2
        root = self.tab()['root']
        self.assertEqual(root['dir'], 'col')
        self.assertEqual([len(r['children']) for r in root['children']], [3, 2])
        self.assertEqual([lay.load('flat')['panes'][p]['session'] for p in lay.leaves(root)], order, 'arrange must keep the order')
        lay.arrange('flat', None, 'main-left', main='c', size=.6)
        root = self.tab()['root']
        self.assertEqual(root['dir'], 'row'); self.assertEqual(sizes(root), [.6, .4])
        self.assertEqual(lay.load('flat')['panes'][root['children'][0]['pane']]['session'], 'c')
        self.assertEqual((root['children'][1]['dir'], len(root['children'][1]['children'])), ('col', 4))
        with self.assertRaisesRegex(ValueError, r'did you mean grid\?'):
            lay.arrange('flat', None, 'gird')

    def test_errors_name_what_exists(self):
        lay.split('flat', 'flat-main', 'right', .5, 'flat-qa')
        with self.assertRaisesRegex(ValueError, r'did you mean flat-qa\?.*panes: flat-main, flat-qa'):
            lay.focus('flat', 'flat-q')
        with self.assertRaisesRegex(ValueError, r'tabs: main'):
            lay.tab_select('flat', 'workers')

    def test_shape_in_one_line(self):
        lay.split('flat', 'flat-main', 'right', .5, 'flat-qa')
        lay.split('flat', 'flat-qa', 'down', .5, 'flat-rev')
        lay.tab_new('flat', 'workers', None, 'w1')
        self.assertEqual(lay.shape(lay.load('flat')),
                         'flat@test  main*: [flat-main* | (flat-qa / flat-rev)] · workers: [w1]')

    def test_tab_order_and_removal(self):
        for name in ('b', 'c'):
            lay.tab_new('flat', name)
        names = lambda: [t['name'] for t in lay.load('flat')['tabs']]
        lay.tab_move('flat', 'c', 'first'); self.assertEqual(names(), ['c', 'main', 'b'])
        lay.tab_move('flat', 'c', 'after:b'); self.assertEqual(names(), ['main', 'b', 'c'])
        lay.tab_move('flat', 'main', 'before:c'); self.assertEqual(names(), ['b', 'main', 'c'])
        lay.tab_move('flat', 'main', 'left'); self.assertEqual(names(), ['main', 'b', 'c'])
        lay.tab_move('flat', 'main', 'last'); self.assertEqual(names(), ['b', 'c', 'main'])
        gone = lay.ws_rm('flat')
        self.assertEqual(gone['sessions'], ['flat-main'])
        with self.assertRaisesRegex(ValueError, 'no workspace flat'):
            lay.load('flat')

    def session(self, name):
        """A session on this machine, which is what --attach may name."""
        path = lay.sm.entry_path(name); path.parent.mkdir(parents=True, exist_ok=True)
        lay.sm.write(path, dict(name=name, host='test'))

    def test_attach_refuses_a_session_of_another_machine(self):
        env = dict(os.environ, SM_WORKSPACE='flat')
        run = lambda *args: subprocess.run([sys.executable, str(BIN / 'sm-session'), *args], env=env, capture_output=True, text=True)
        for args in (['split', '--attach', 'nowhere'], ['split', '--attach', 'w1@mac'], ['tab', 'new', '--attach', 'nowhere']):
            out = run(*args)
            self.assertNotEqual(out.returncode, 0, args)
            self.assertIn('Start one here instead: sm split --spawn', out.stderr)
        self.assertEqual(len(lay.load('flat')['tabs']), 1, 'nothing half-made')
        self.assertEqual(lay.leaves(self.tab()['root']), [self.first])
        self.session('w1')
        self.assertEqual(run('split', '--attach', 'w1').returncode, 0)
        # a machine ssh reaches is fine; which ones those are comes from the hosts file
        (self.temp / 'cfg/hosts.json').write_text('{"box": {"ssh": "box"}}')
        self.assertEqual(run('split', '--attach', 'far@box').returncode, 0, 'NAME@HOST on a reachable machine')

    def test_cli_prints_the_new_shape(self):
        self.session('w1')
        env = dict(os.environ, SM_WORKSPACE='flat')
        run = lambda *args: subprocess.run([sys.executable, str(BIN / 'sm-session'), *args], env=env, capture_output=True, text=True)
        out = run('split', 'flat-main', '--right', '--tab', 'workers', '--attach', 'w1')
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('not both', out.stderr)
        out = run('split', '--tab', 'workers', '--attach', 'w1')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines()[0], 'flat@test  main*: [flat-main] · workers: [w1]')
        self.assertIn('sm show w1', out.stdout)
        out = run('arrange', 'workers', 'columns', '--json')
        self.assertEqual(json.loads(out.stdout)['tab'], 'workers')
        out = run('focus', 'w9')
        self.assertIn('panes: flat-main, w1', out.stderr)

    def test_cli_ws_new_and_rm_on_the_app_machine(self):
        env = dict(os.environ, SM_APP_HOST='test')  # this machine is the app's: no call to another machine
        env.pop('SM_WORKSPACE', None)
        run = lambda *args: subprocess.run([sys.executable, str(BIN / 'sm-session'), *args], env=env, capture_output=True, text=True,
                                           cwd=self.temp / 'proj')
        out = run('ws', 'new', 'side', '--cwd', str(self.temp / 'proj'), '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn('sidebar', json.loads(out.stdout))
        out = run('ws', 'rm', 'side')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), 'removed workspace side')
        out = run('ws', 'rm', 'sidee')
        self.assertIn('did you mean', out.stderr + ' ' + out.stdout) if 'side' in [w['workspace'] for w in lay.ws_list()] else \
            self.assertIn('workspaces: flat', out.stderr)
        out = run('ws', 'ls')
        self.assertTrue(out.stdout.startswith('flat@test  main*: [flat-main]'), out.stdout)

    def test_where_names_the_fine_tab_that_shows_it_now(self):
        # A session brought into Fine with `fine attach` has no FINE_TAB_ID of its own; Fine's saved windows know.
        home = self.temp / 'home'; (home / '.fine').mkdir(parents=True)
        (home / '.fine/window-states.json').write_text(json.dumps(
            [{'id': 'W', 'sessions': [{'id': '3EF6FD22-0000-0000-0000-000000000000', 'smSessionName': 'flat-main'}]}]))
        env = dict(os.environ, HOME=str(home))
        out = subprocess.run([sys.executable, str(BIN / 'sm-session'), 'where', 'flat-main', '--json'], env=env,
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)['fine_tab'], '3ef6fd22-0000-0000-0000-000000000000')
        (home / '.fine/window-states.json').write_text('[]')
        out = subprocess.run([sys.executable, str(BIN / 'sm-session'), 'where', 'flat-main', '--json'], env=env,
                             capture_output=True, text=True)
        self.assertIsNone(json.loads(out.stdout)['fine_tab'])

if __name__ == '__main__':
    unittest.main(verbosity=1)
