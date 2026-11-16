#!/usr/bin/env python3
"""sm session layer: one agent per name, resume by name, restore after a dead holder.

Uses the real peer-hold; the agent is a shell that prints its arguments, so `-ry <session>`
on resume is visible in `sm tail`. Run: python3 -I tests/sm/test_sm_session.py
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

# The installed layout (session layer, peer and peer-hold in one folder): tests/run.sh assembles it in SEOL_BIN.
BIN = Path(os.environ.get('SEOL_BIN') or Path(__file__).resolve().parents[1] / 'bin')
sys.path.insert(0, str(BIN))
if not (BIN / 'peer-hold').exists():
    sys.exit('build peer-hold first: hold/install.sh')
import peer_core  # noqa: E402
import sm_session as sm  # noqa: E402


def secrets_hex():
    import secrets
    return secrets.token_hex(3)


def fake_launcher(kind, args):
    return ['/bin/sh', '-c', 'echo "agent ' + kind + ' $*"; sleep 60', 'x'] + list(args)


class SmSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='sm', dir='/tmp'))  # short: socket path limit
        (self.temp / 'cfg').mkdir()
        (self.temp / 'cfg/hostname').write_text('test')
        fake = self.temp / 'agent'
        fake.write_text('#!/bin/sh\nk=$1; shift; echo "agent $k $*"; exec sleep 60\n'); fake.chmod(0o755)
        # SM_TEST_LAUNCHER reaches viewers started as child processes too: no test may start a real agent.
        self.env = patch.dict(os.environ, SM_STATE=str(self.temp / 'sm'), PEER_STATE=str(self.temp / 'peer'),
                              PEER_CONFIG=str(self.temp / 'cfg'), SM_TEST_LAUNCHER=str(fake),
                              SM_READY_TIMEOUT='1')
        self.env.start()
        os.environ.pop('XDG_RUNTIME_DIR', None)  # no systemd scope in tests
        self.launch = patch.object(sm, 'launcher', fake_launcher); self.launch.start()
        self.health = patch.object(sm, 'CLAUDE_HEALTH', 0); self.health.start()  # fake agents: no fullscreen ledger
        self.work = self.temp / 'w'; self.work.mkdir()

    def tearDown(self):
        for entry in sm.entries():
            meta = sm.read(Path(entry['hold']).with_suffix('.json'))
            for pid in (meta.get('child'), meta.get('holder')):
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        self.launch.stop(); self.health.stop(); self.env.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def spawn(self, name='lane', **kw):
        return sm.spawn(name, 'claude', str(self.work), timeout=.3, **kw)

    def age(self, name, seconds=5 * 3600):
        """Pretend NAME started long ago (sleep counts idle from its last start too)."""
        e = sm.load(name); e['started'] = time.time() - seconds; sm.write(sm.entry_path(name), e)

    def tail(self, name='lane'):
        time.sleep(.3)
        return sm.tail(name).decode()

    def kill_holder(self, name='lane'):
        meta = sm.read(Path(sm.load(name)['hold']).with_suffix('.json'))
        os.kill(meta['holder'], signal.SIGKILL)  # like a reboot: no exit record
        os.kill(meta['child'], signal.SIGKILL)
        time.sleep(.3)

    def test_one_agent_per_name(self):
        first = self.spawn(args=['--model', 'm'])
        self.assertEqual(first['action'], 'spawned')
        self.assertEqual(first['status'], 'running')
        again = self.spawn()
        self.assertEqual(again['action'], 'already-running')
        self.assertEqual(again['hold'], first['hold'])
        self.assertIn('agent claude -y --model m', self.tail())

    def test_spawned_agents_skip_permission_prompts_unless_asked(self):
        # Nobody sits at a spawned agent's screen: Claude and Codex start with -y unless --ask.
        self.assertEqual(sm.unattended('claude', ['--model', 'm']), ['-y', '--model', 'm'])
        self.assertEqual(sm.unattended('codex', []), ['-y'])
        self.assertEqual(sm.unattended('codex', ['-y', '-m', 'x']), ['-y', '-m', 'x'])
        self.assertEqual(sm.unattended('omo', ['--model', 'm']), ['--permission-preset', 'full-access', '--model', 'm'])
        self.assertEqual(sm.unattended('omo', ['--permission-preset', 'workspace']), ['--permission-preset', 'workspace'])
        self.assertEqual(sm.unattended('omo', [], ask=True), [])
        self.assertEqual(sm.unattended('claude', ['--dangerously-skip-permissions']), ['--dangerously-skip-permissions'])
        self.assertEqual(sm.unattended('claude', ['--model', 'm'], ask=True), ['--model', 'm'])
        self.assertEqual(sm.unattended('shell', ['-c', 'x']), ['-c', 'x'])
        self.spawn('asked', ask=True)
        self.assertNotIn('agent claude -y', self.tail('asked'))

    def test_only_a_worker_asked_for_runs_without_the_advisor(self):
        # Orchestration is opt-in: a spawned agent keeps the user's settings unless its lead says --worker.
        self.assertNotIn(sm.ADVISOR_OFF, sm.load(self.spawn('plain', parent='test/abc')['name'])['env'])
        self.assertIn(sm.ADVISOR_OFF, sm.load(self.spawn('worker', parent='test/abc', worker=True)['name'])['env'])
        e = sm.load(sm.spawn('cx', 'codex', str(self.work), timeout=.3, parent='test/abc', worker=True)['name'])
        self.assertNotIn(sm.ADVISOR_OFF, e['env'])

    def test_concurrent_spawns_start_once(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.spawn()['action'])) for _ in range(4)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(sorted(results), ['already-running'] * 3 + ['spawned'])
        holders = list((self.temp / 'sm/hold').glob('*.json'))
        self.assertEqual(len(holders), 1)

    def test_dead_session_resumes_by_name(self):
        self.spawn()
        entry = sm.load('lane'); entry['session'] = 'sid-1'; sm.write(sm.entry_path('lane'), entry)
        self.kill_holder()
        self.assertEqual(sm.status(sm.load('lane')), 'dead')
        out = sm.restore()['restored']
        self.assertEqual([r['action'] for r in out], ['resumed'])
        self.assertIn('agent claude -ry sid-1', self.tail())
        self.assertEqual(sm.load('lane')['hold'], entry['hold'])  # same socket: panels keep pointing at it

    def test_stopped_stays_stopped_until_asked(self):
        self.spawn()
        entry = sm.load('lane'); entry['session'] = 'sid-2'; sm.write(sm.entry_path('lane'), entry)
        self.assertEqual(sm.stop('lane')['action'], 'stopped')
        self.assertEqual(sm.status(sm.load('lane')), 'ended')
        self.assertEqual(sm.restore()['restored'], [])
        back = self.spawn()  # the name is the session: spawning an ended name resumes it
        self.assertEqual(back['action'], 'resumed')
        self.assertIn('-ry sid-2', self.tail())

    def test_resume_keeps_the_conversation_and_takes_engine_options(self):
        # Changing a model on resume must not need --fresh, which starts a new conversation.
        self.spawn()
        entry = sm.load('lane'); entry['session'] = 'sid-m'; sm.write(sm.entry_path('lane'), entry)
        sm.stop('lane')
        sm.resume('lane', args=['-y', '--model', 'n'])
        self.assertIn('agent claude -ry sid-m --model n', self.tail())
        sm.stop('lane')
        again = self.spawn(args=['--model', 'o'])  # spawning the ended name resumes it with these options
        self.assertEqual(again['action'], 'resumed')
        self.assertIn('-ry sid-m --model o', self.tail())

    def test_fresh_starts_a_new_engine_session(self):
        self.spawn(args=['--model', 'm'])
        entry = sm.load('lane'); entry['session'] = 'sid-3'; sm.write(sm.entry_path('lane'), entry)
        sm.stop('lane')
        new = self.spawn(fresh=True, args=['--model', 'n'])
        self.assertEqual(new['action'], 'spawned')
        self.assertIsNone(sm.load('lane')['session'])
        text = self.tail()
        self.assertIn('--model n', text)
        self.assertNotIn('-ry', text)
        with self.assertRaisesRegex(ValueError, 'running'):
            self.spawn(fresh=True)

    def test_attach_brings_it_back(self):
        self.spawn()
        entry = sm.load('lane'); entry['session'] = 'sid-4'; sm.write(sm.entry_path('lane'), entry)
        self.kill_holder()
        calls, real = [], sm.subprocess.run
        def run(cmd, *a, **k):
            if 'attach' in cmd:
                calls.append(cmd)
                raise SystemExit(0)  # the viewer would stay attached; stop the loop here
            return real(cmd, *a, **k)
        with patch.object(sm.subprocess, 'run', run), self.assertRaises(SystemExit):
            sm.attach('lane')
        self.assertTrue(calls[0][0].endswith('peer-hold') and calls[0][1:3] == ['attach', '--sock'])
        self.assertIn('-ry sid-4', self.tail())

    def test_name_of_a_live_session_elsewhere_is_refused(self):
        root = Path(os.environ['PEER_STATE'])
        (root / 'sessions').mkdir(parents=True)
        proc = peer_core.process(os.getpid())
        peer_core.write(peer_core.path('sessions', 'claude:x'), dict(id='claude:x', session='x', name='fine-one', kind='claude',
                                                                     host='test', target='fine:abc', process=proc, state='idle'))
        (root / 'names.json').write_text(json.dumps({'fine-one': 'claude:x'}))
        with self.assertRaisesRegex(ValueError, 'live session'):
            self.spawn('fine-one')

    def test_env_carries_name_pane_and_parent(self):
        self.spawn(pane='P-1', parent='mac/claude:lead', env=['X=1'])
        entry = sm.load('lane')
        self.assertTrue(entry['visible'])
        for item in ('PEER_NAME=lane', 'SM_PANE_ID=P-1', 'PEER_VISIBLE=1', 'PEER_PARENT=mac/claude:lead', 'X=1'):
            self.assertIn(item, entry['env'])
        with self.assertRaisesRegex(ValueError, 'cannot set'):
            self.spawn('other', env=['PEER_NAME=evil'])

    def test_shell_pane_is_held_and_restarts(self):
        self.launch.stop()  # real launcher: kind=shell runs $SHELL -l under the holder
        try:
            with patch.dict(os.environ, SHELL='/bin/sh'):
                out = sm.spawn('dev', 'shell', str(self.work), timeout=.3)
                self.assertEqual((out['action'], out['status']), ('spawned', 'running'))
                self.kill_holder('dev')
                self.assertEqual(sm.restore()['restored'][0]['action'], 'resumed')
                self.assertEqual(sm.status(sm.load('dev')), 'running')
        finally:
            self.launch.start()

    def test_keys_type_into_the_session_and_return_its_screen(self):
        """sm keys: answer a screen that stops an agent you started (tmux send-keys), and see what happened."""
        self.assertEqual(sm.encode_keys(['Down', 'Enter', 'C-c', 'hi']), b'\x1b[B\r\x03hi')
        self.launch.stop()
        try:
            with patch.dict(os.environ, SHELL='/bin/sh'):
                sm.spawn('dev', 'shell', str(self.work), timeout=.3)
                time.sleep(.5)
                # the stand-in agent only sleeps; the terminal echoing the line shows the keys reached it
                screen = sm.keys('dev', ['echo', 'Space', 'typed-$((40+2))', 'Enter'], wait=1).decode()
                self.assertIn('echo typed-$((40+2))\n', screen)
                sm.stop('dev')
                with self.assertRaisesRegex(ValueError, 'not running'):
                    sm.keys('dev', ['Enter'])
        finally:
            self.launch.start()

    def test_upgrade_restarts_an_idle_session_running_an_older_engine(self):
        """A session restarts into the installed version once idle — same name, same conversation."""
        self.spawn('old')
        e = sm.load('old'); e['started'] = time.time() - 3600; e['session'] = 'conv-1'; sm.write(sm.entry_path('old'), e)
        engine = self.temp / 'claude-new'; engine.write_text('new'); os.utime(engine, (time.time(), time.time()))
        idle = dict(state='idle')
        with patch.object(sm, 'tools', lambda: {'claude': str(engine)}), \
                patch.object(sm, 'peer_row', lambda entry: idle), \
                patch.object(sm, 'quiet_since', lambda entry, row: time.time() - 600), \
                patch.object(sm, 'looked_at', lambda entry: False), \
                patch.object(sm, 'background_work', lambda entry: False), \
                patch.object(sm, 'resumable', lambda entry: True):
            planned = sm.upgrade(dry_run=True, engines=False)['upgrade']
            self.assertEqual([(d['name'], d['action']) for d in planned], [('old', 'would-upgrade')])
            self.assertIn('claude updated', planned[0]['why'])
            with patch.object(sm, 'looked_at', lambda entry: True):  # on screen: the user may be typing, wait longer
                self.assertEqual(sm.upgrade(engines=False)['upgrade'][0]['action'], 'pending')
            # being shown right now does not reset the quiet time (a visible pane is "seen" every moment)
            with patch.object(sm, 'looked_at', lambda entry: True), \
                    patch.object(sm, 'quiet_since', lambda entry, row: time.time() - 31 * 60):
                self.assertEqual(sm.upgrade(dry_run=True, engines=False)['upgrade'][0]['action'], 'would-upgrade')
            done = sm.upgrade(engines=False)['upgrade']
            self.assertEqual([(d['name'], d['action']) for d in done], [('old', 'upgraded')])
            after = sm.load('old')
            self.assertEqual(sm.status(after), 'running')
            self.assertGreater(after['started'], time.time() - 60)
            self.assertNotIn('slept', after)
            self.assertEqual(sm.upgrade(engines=False)['upgrade'], [], 'now current: nothing more to do')
            with patch.object(sm, 'peer_row', lambda entry: dict(state='working')):
                os.utime(engine, (time.time() + 5, time.time() + 5))
                self.assertEqual(sm.upgrade(engines=False)['upgrade'][0]['hold'], 'it is working')

    def test_an_upgrade_that_cannot_restart_leaves_the_session_asleep(self):
        """Ended by the upgrade, not by anyone: a message or a look must still bring it back. A start that dies after
        the old peer row made it look registered counts as failed too."""
        self.spawn('old')
        e = sm.load('old'); e['started'] = time.time() - 3600; e['session'] = 'conv-1'; sm.write(sm.entry_path('old'), e)
        engine = self.temp / 'claude-new'; engine.write_text('new'); os.utime(engine, (time.time(), time.time()))

        def dies_after_registering(name, timeout=30, args=()):
            entry = sm.load(name); entry.pop('slept', None); sm.write(sm.entry_path(name), entry)
            return dict(name=name, action='resumed')  # the holder is gone: _end stopped it and nothing started

        with patch.object(sm, 'tools', lambda: {'claude': str(engine)}), \
                patch.object(sm, 'peer_row', lambda entry: dict(state='idle')), \
                patch.object(sm, 'quiet_since', lambda entry, row: time.time() - 600), \
                patch.object(sm, 'looked_at', lambda entry: False), \
                patch.object(sm, 'background_work', lambda entry: False), \
                patch.object(sm, 'resumable', lambda entry: True), \
                patch.object(sm, 'UPGRADE_SETTLE', 0), \
                patch.object(sm, 'resume', dies_after_registering):
            done = sm.upgrade(engines=False)['upgrade']
        self.assertEqual([(d['name'], d['action']) for d in done], [('old', 'failed')])
        self.assertTrue(sm.load('old').get('slept'), 'asleep, so a message wakes it')

    def test_a_claude_never_spoken_to_starts_fresh_instead_of_failing_to_resume(self):
        """Claude keeps no transcript before its first message: resuming that id ends in "No conversation found"."""
        self.spawn('new')
        entry = sm.load('new'); entry['session'] = 'no-transcript'; sm.write(sm.entry_path('new'), entry)
        sm.stop('new')
        with patch.object(sm, 'resumable', lambda entry: False):
            done = sm.resume('new', args=['-m', 'x'])
        self.assertIn('fresh', done['action'])
        self.assertEqual(sm.status(sm.load('new')), 'running')
        self.assertNotEqual(sm.load('new').get('session'), 'no-transcript', 'the new start gets its own id')
        sm.stop('new')
        entry = sm.load('new'); entry['args'] = ['-ry', 'old-thread']; sm.write(sm.entry_path('new'), entry)
        with patch.object(sm, 'resumable', lambda entry: False):
            self.assertEqual(sm.resume('new')['action'], 'resumed', 'a Fine tab reopened on an old conversation')
        home = self.temp / 'claude-home'; (home / 'projects/p').mkdir(parents=True)
        env = {k: v for k, v in os.environ.items() if k != 'SM_TEST_LAUNCHER'}
        env['CLAUDE_CONFIG_DIR'] = str(home)
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(sm.resumable(dict(kind='claude', session='s1')))
            (home / 'projects/p/s1.jsonl').write_text('{}')
            self.assertTrue(sm.resumable(dict(kind='claude', session='s1')))

    def test_spawn_with_a_conversation_named_resumes_exactly_that(self):
        self.assertEqual(sm.unattended('codex', ['-ry', 'thread-1'], False), ['-ry', 'thread-1'], 'no -y beside -ry')
        self.assertEqual(sm.unattended('codex', [], False), ['-y'])
        self.spawn('moved')
        entry = sm.load('moved'); entry['session'] = 'older'; sm.write(sm.entry_path('moved'), entry)
        sm.stop('moved')
        sm.spawn('moved', 'claude', str(self.work), args=['-ry', 'thread-1'])  # an ended name, retried
        out = sm.tail('moved').decode()
        self.assertIn('-ry thread-1', out)
        self.assertNotIn('older', out)

    def test_an_older_row_on_the_same_holder_does_not_hide_the_live_one(self):
        entry = dict(session='new', hold='/h.sock', started=100)
        rows = [dict(id='claude:old', name='x-2', session='old', hold='/h.sock', state='gone', observed=50),
                dict(id='claude:new', name='x', session='new', hold='/h.sock', state='idle', observed=200)]
        with patch.object(sm.peer_core, 'sessions', lambda: rows), patch.object(sm.peer_core, 'public', lambda r: r):
            self.assertEqual(sm.peer_row(entry)['id'], 'claude:new')
            self.assertEqual(sm.peer_row(dict(entry, session=None))['id'], 'claude:new', 'no id yet: the newest')

    def test_a_startup_failure_says_why(self):
        self.spawn('bad')
        sm.stop('bad')
        with patch.object(sm, 'last_screen', lambda name: b'\x1b[?2026h\x1b[H\x1b[2JNo conversation found with session ID: s1\r\n>\r\n\x1b[<u'):
            message = sm.startup_failure('bad')
        self.assertIn('exited during startup: No conversation found with session ID: s1', message)
        self.assertIn('sm screen bad', message)

    def test_launchers_are_found_through_the_interactive_shell(self):
        """launchd's login shell does not read .zshrc: ccv is only on PATH in an interactive shell (Mac, 11/07)."""
        shell = self.temp / 'fake-shell'
        shell.write_text('#!/bin/sh\necho noise from a shell rc\nprintf "\\nSM_PATH=%s\\n" "/from/interactive:/usr/bin"\n')
        shell.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k != 'SM_TEST_LAUNCHER'}
        env.update(SHELL=str(shell), PATH='/usr/bin:/bin')
        code = 'import sys; sys.path.insert(0, %r); import os, sm_session as sm; sm.find_launchers(); print(os.environ["PATH"])' % str(BIN)
        out = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), '/from/interactive:/usr/bin', out.stderr)

    def test_adopted_but_still_in_tmux_is_never_resumed(self):
        name = 'smt' + secrets_hex()
        subprocess.run(['tmux', 'new-session', '-d', '-s', name, 'sleep 30'], check=True)
        try:
            pane = subprocess.run(['tmux', 'display', '-p', '-t', '=' + name + ':', '#{pane_id}'], capture_output=True, text=True).stdout.strip()
            sm.write(sm.entry_path('lane'), dict(name='lane', kind='claude', host='test', cwd=str(self.work), session='sid-9',
                                                 hold=str(self.temp / 'sm/hold/x.sock'), env=[], args=[], tmux_pane=pane, adopted=True))
            self.assertEqual(sm.status(sm.load('lane')), 'tmux')
            self.assertEqual(sm.restore()['restored'], [])
            self.assertEqual(self.spawn()['action'], 'already-running')
            entry = sm.load('lane'); entry['migrated'] = 1; sm.write(sm.entry_path('lane'), entry)
            self.assertEqual(sm.status(sm.load('lane')), 'dead')  # migrated: its holder is what counts
        finally:
            subprocess.run(['tmux', 'kill-session', '-t', '=' + name])

    def test_spawn_from_inside_an_agent_gets_a_clean_environment(self):
        dirty = {'CLAUDECODE': '1', 'CLAUDE_CODE_CHILD_SESSION': '1', 'CLAUDE_CODE_MESSAGING_TOKEN': 'secret',
                 'CLAUDE_PID': '1', 'PEER_HOLD_SOCK': '/x.sock', 'PEER_LAUNCHED': '1', 'SM_PANE_ID': 'p-other',
                 'CLAUDE_CODE_EXECUTABLE': '/keep/me'}
        names = ['CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CODE_MESSAGING_TOKEN', 'PEER_LAUNCHED', 'SM_PANE_ID',
                 'PEER_HOLD_SOCK', 'CLAUDE_CODE_EXECUTABLE', 'PEER_NAME', 'FINE_INITIAL_PROMPT', 'GHOSTTY_BIN_DIR']
        # print only these names' values, never the whole environment (it holds keys)
        dump = lambda kind, args: ['/bin/sh', '-c', ' '.join('echo %s=${%s};' % (n, n) for n in names) + ' sleep 60']
        dirty.update(FINE_INITIAL_PROMPT='first question', GHOSTTY_BIN_DIR='/g')
        with patch.dict(os.environ, dirty), patch.object(sm, 'launcher', dump):
            self.spawn(args=[])
        env = self.tail()
        for gone in ('FINE_INITIAL_PROMPT=first', 'GHOSTTY_BIN_DIR=/g'):
            self.assertNotIn(gone, env)
        self.assertEqual(sm.clean_env()['TERM_PROGRAM'], 'ghostty')
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('LANG', None)
            self.assertTrue(sm.clean_env()['LANG'].endswith('UTF-8'))
        for gone in ('CLAUDECODE=1', 'CLAUDE_CODE_CHILD_SESSION=1', 'secret', 'PEER_LAUNCHED=1', 'p-other', 'PEER_HOLD_SOCK=/x.sock'):
            self.assertNotIn(gone, env)
        self.assertIn('CLAUDE_CODE_EXECUTABLE=/keep/me', env)
        self.assertIn('PEER_NAME=lane', env)

    def test_idle_past_the_cache_sleeps_and_wakes_on_use(self):
        old = time.time() - 70 * 60
        rows = {'lane': dict(state='idle', since=old), 'busy': dict(state='working', since=old),
                'fresh': dict(state='idle', since=time.time() - 10 * 60), 'keep-x': dict(state='idle', since=old)}
        for name in rows:
            self.spawn(name)
        entry = sm.load('lane'); entry['session'] = 'sid-s'; sm.write(sm.entry_path('lane'), entry)
        for name in rows:
            e = sm.load(name); e['session'] = e.get('session') or 'sid-' + name; sm.write(sm.entry_path(name), e)
            self.age(name)
        with patch.object(sm, 'peer_row', lambda e: rows.get(e['name'])), \
                patch.object(sm, 'awake_prefixes', lambda: ['keep-']):
            plan = sm.sleep(dry_run=True)['sleep']
            self.assertIn('keep-x matches awake_prefixes', [r.get('reason') for r in plan])
            self.assertEqual([r['name'] for r in plan if r['action'] == 'would-sleep'], ['lane'])
            done = sm.sleep()['sleep']
        self.assertEqual([(r['name'], r['action']) for r in done if r['action'] == 'slept'], [('lane', 'slept')])
        self.assertEqual(sm.status(sm.load('lane')), 'ended')
        self.assertTrue(sm.load('lane')['slept'])
        self.assertEqual(sm.restore()['restored'], [])  # boot leaves sleepers asleep; use wakes them
        woke = sm.wake('lane')
        self.assertEqual(woke['action'], 'resumed')
        self.assertNotIn('slept', sm.load('lane'))
        self.assertIn('-ry sid-s', self.tail())
        sm.stop('lane')
        self.assertEqual(sm.wake('lane')['action'], 'awake')  # stopped on purpose: a message does not wake it

    def test_a_viewer_survives_sleep_and_comes_back_on_wake(self):
        import pty, select as sel
        self.spawn()
        entry = sm.load('lane'); entry['session'] = 'sid-v'; sm.write(sm.entry_path('lane'), entry)
        self.age('lane')
        pid, fd = pty.fork()
        if pid == 0:
            os.execv(sys.executable, [sys.executable, str(BIN / 'sm-session'), 'attach', 'lane', '--passive'])
        def read_until(text, timeout=6):
            buf, end = b'', time.time() + timeout
            while text.encode() not in buf and time.time() < end:
                if sel.select([fd], [], [], .1)[0]:
                    try:
                        buf += os.read(fd, 65536)
                    except OSError:
                        break
            return text.encode() in buf
        try:
            self.assertTrue(read_until('agent claude'))
            with patch.object(sm, 'peer_row', lambda e: dict(state='idle', since=time.time() - 4000)):
                done = sm.sleep()['sleep']
            self.assertEqual([d.get('action') for d in done], ['slept'], done)  # a passive viewer is not a screen
            time.sleep(1.5)
            self.assertEqual(sm.status(sm.load('lane')), 'ended', 'the leftover viewer must not wake it')
            self.assertEqual(os.waitpid(pid, os.WNOHANG), (0, 0), 'the viewer must stay while its session sleeps')
            sm.wake('lane')  # e.g. a message arrived
            self.assertTrue(read_until('-ry sid-v'), 'the viewer re-attaches by itself after a wake')
        finally:
            os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0); os.close(fd)

    def test_opening_a_pane_wakes_it_and_a_stop_while_shown_waits_for_a_key(self):
        import pty, select as sel
        self.spawn()
        entry = sm.load('lane'); entry['session'] = 'sid-k'; sm.write(sm.entry_path('lane'), entry)
        time.sleep(.3)
        sm.stop('lane')  # e.g. stopped to save quota
        pid, fd = pty.fork()
        if pid == 0:
            os.execv(sys.executable, [sys.executable, str(BIN / 'sm-session'), 'attach', 'lane'])
        def read_until(text, timeout=6):
            buf, end = b'', time.time() + timeout
            while text.encode() not in buf and time.time() < end:
                if sel.select([fd], [], [], .1)[0]:
                    try:
                        buf += os.read(fd, 65536)
                    except OSError:
                        break
            return text.encode() in buf
        try:
            self.assertTrue(read_until('-ry sid-k'), 'opening the pane brings it back')
            self.assertEqual(sm.status(sm.load('lane')), 'running')
            sm.stop('lane')  # stopped while shown (to save quota): respected, the pane waits
            time.sleep(1.5)
            self.assertEqual(sm.status(sm.load('lane')), 'ended')
            os.write(fd, b'x')
            for _ in range(40):
                if sm.status(sm.load('lane')) == 'running':
                    break
                time.sleep(.1)
            self.assertEqual(sm.status(sm.load('lane')), 'running', 'a key in the pane starts it again')
        finally:
            os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0); os.close(fd)

    def test_each_start_gets_its_own_scope(self):
        # A dead holder's leftover programs can keep its scope alive; the next start must not reuse that name.
        with patch.object(sm.sys, 'platform', 'linux'), patch.object(sm.shutil, 'which', lambda _: '/usr/bin/systemd-run'), \
                patch.dict(os.environ, {'XDG_RUNTIME_DIR': '/run/user/1000'}):
            first = sm.scope_prefix('/x/hold/a25ec181.sock')
            second = sm.scope_prefix('/x/hold/a25ec181.sock')
        unit = lambda cmd: cmd[cmd.index('--unit') + 1]
        self.assertTrue(unit(first).startswith('peer-hold-a25ec181-'), first)  # the memory cap drop-in matches peer-hold-*
        self.assertNotEqual(unit(first), unit(second))

    def test_a_workspace_sleeps_as_a_whole(self):
        import sm_layout as lay
        lay.ws_new('proj', self.work)
        old, recent = time.time() - 70 * 60, time.time() - 5 * 60
        rows = {'a': dict(state='idle', since=old), 'b': dict(state='idle', since=recent)}
        for name in rows:
            self.spawn(name, env=['SM_WORKSPACE=proj'])
            e = sm.load(name); e['session'] = 'sid-' + name; sm.write(sm.entry_path(name), e)
            self.age(name)
        with patch.object(sm, 'peer_row', lambda e: rows.get(e['name'])):
            done = sm.sleep()['sleep']
            self.assertEqual([d['action'] for d in done], ['awake'], done)  # b was active 5 min ago: proj stays awake
            rows['b']['since'] = old
            done = sm.sleep()['sleep']
        self.assertEqual(sorted(d['name'] for d in done if d['action'] == 'slept'), ['a', 'b'])

    def test_a_fine_tab_in_the_same_folder_is_not_part_of_the_workspace(self):
        import sm_layout as lay
        lay.ws_new('proj', self.work)
        self.spawn('fine-1234', env=['FINE_TAB_ID=aaaa'])
        self.spawn('lead')
        self.assertIsNone(sm.workspace_of(sm.load('fine-1234')))
        self.assertEqual(sm.workspace_of(sm.load('lead')), 'proj')

    def test_nothing_to_resume_means_no_sleep(self):
        self.spawn('empty')  # never registered: no engine session id
        self.age('empty')
        with patch.object(sm, 'peer_row', lambda e: dict(state='idle', since=time.time() - 4000)):
            done = sm.sleep()['sleep']
        self.assertEqual(done[0]['action'], 'awake')
        self.assertIn('no conversation', done[0]['reason'])

    def test_view_state_follows_the_session_and_keeps_its_last_screen(self):
        self.spawn()
        entry = sm.load('lane'); entry['session'] = 'sid-st'; sm.write(sm.entry_path('lane'), entry)
        self.assertEqual(sm.view_state('lane'), 'waking')  # up, but its screen has not stood up (not registered)
        with patch.object(sm, 'peer_row', lambda e: dict(state='idle', observed=time.time() + 1, since=time.time())):
            self.assertEqual(sm.view_state('lane'), 'ready')
        time.sleep(.3)
        sm.stop('lane')
        self.assertEqual(sm.view_state('lane'), 'stopped')
        self.assertIn(b'agent claude', sm.last_screen('lane'))
        entry = sm.load('lane'); entry['slept'] = 1; sm.write(sm.entry_path('lane'), entry)
        self.assertEqual(sm.view_state('lane'), 'asleep')
        self.assertEqual(sm.view_state('nope'), 'gone')
        out = subprocess.run([sys.executable, str(BIN / 'sm-session'), 'state', 'lane', '--json'], capture_output=True, text=True)
        self.assertEqual(json.loads(out.stdout)['state'], 'asleep')
        raw = subprocess.run([sys.executable, str(BIN / 'sm-session'), 'screen', 'lane'], capture_output=True).stdout
        self.assertIn(b'agent claude', raw)

    def test_state_watch_looks_fast_only_while_waking_or_just_changed(self):
        # A steady pane is looked at every 2 s; waking and the few seconds after a change stay at .3 s.
        states = iter(['waking', 'waking', 'ready', 'ready', 'ready', 'stopped'])
        clock = [0.0]
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds); clock[0] += seconds
        with patch.object(sm, 'view_state', lambda name: next(states)), \
             patch.object(sm.time, 'monotonic', lambda: clock[0]), patch.object(sm.time, 'sleep', sleep):
            stream = sm.watch_state('lane', interval=.3, heartbeat=99, steady=2.0, settle=.5)
            lines = [next(stream)['state'] for _ in range(3)]
        self.assertEqual(lines, ['waking', 'ready', 'stopped'])
        self.assertEqual(sleeps[:2], [.3, .3])          # waking
        self.assertEqual(sleeps[2:4], [.3, .3])         # ready, just changed
        self.assertEqual(sleeps[4], 2.0)                # ready and steady

    def test_a_known_conversation_reads_its_own_peer_row_without_scanning(self):
        self.spawn()
        entry = sm.load('lane'); entry['session'] = 'sid-direct'; sm.write(sm.entry_path('lane'), entry)
        sm.peer_core.write(sm.peer_core.path('sessions', 'claude:sid-direct'),
                           dict(id='claude:sid-direct', session='sid-direct', kind='claude', name='lane', state='idle',
                                observed=time.time() + 1, hold=entry['hold']))
        with patch.object(sm.peer_core, 'sessions', side_effect=AssertionError('scanned every row')):
            self.assertEqual(sm.view_state('lane'), 'ready')

    def test_one_state_watcher_for_many_names(self):
        # The app writes +NAME / -NAME; each name added is answered at once; closing the pipe ends the watcher.
        read, write = os.pipe()
        os.write(write, b'+a\n+b\n')
        looks = {'a': iter(['ready', 'ready', 'ready']), 'b': iter(['waking', 'ready', 'ready'])}
        with patch.object(sm, 'view_state', lambda name: next(looks[name])):
            stream = sm.watch_states(read, interval=.01, heartbeat=99, steady=.01, settle=0)
            first = [next(stream) for _ in range(2)]
            self.assertEqual(sorted((r['name'], r['state']) for r in first), [('a', 'ready'), ('b', 'waking')])
            self.assertEqual((lambda r: (r['name'], r['state']))(next(stream)), ('b', 'ready'))
            os.write(write, b'-a\n-b\n'); os.close(write)
            self.assertEqual(list(stream), [])
        os.close(read)

    def test_old_saved_screen_is_safe_for_both_screen_and_attach(self):
        entry = dict(name='old', hold=str(self.temp / 'old.sock'))
        sm.write(sm.entry_path('old'), entry)
        legacy = self.temp / 'old.screen'
        legacy.write_bytes(b'old frame\r\n' * 80000 + b'\x1b[H\x1b[2JLAST VIEW\x1b[c\x1b]11;?\x07')
        rendered = sm.last_screen('old')
        self.assertLess(len(rendered), 1024)
        self.assertEqual(rendered.count(b'LAST VIEW'), 1)
        self.assertNotIn(b'old frame', rendered)
        self.assertNotIn(b'\x1b[c', rendered)
        self.assertNotIn(b'\x1b]', rendered)
        import io
        stdout = type('Output', (), {'buffer': io.BytesIO(), 'flush': lambda _: None})()
        with patch.object(sm.sys, 'stdout', stdout):
            sm.show_last_screen(entry)
        self.assertEqual(stdout.buffer.getvalue(), rendered)  # no clear outside the snapshot
        self.assertTrue(legacy.read_bytes().startswith(b'old frame'))  # keep the original log

    def test_idle_counts_from_the_last_start_or_look_not_an_old_turn(self):
        self.spawn()
        entry = sm.load('lane'); entry['session'] = 'sid-i'; sm.write(sm.entry_path('lane'), entry)
        old = time.time() - 5 * 3600  # its last turn ended hours ago, then it was woken just now
        with patch.object(sm, 'peer_row', lambda e: dict(state='idle', since=old)):
            done = sm.sleep()['sleep']
        self.assertEqual([d['action'] for d in done], [], done)  # judged alone, active < 65 min: nothing to report
        self.assertEqual(sm.status(sm.load('lane')), 'running')


class TrustFolderTests(unittest.TestCase):
    """An unattended Claude must not stop on the folder-trust screen (11/02 inche-opus after `git init`)."""
    def test_trusts_the_folder_and_its_git_root_under_claudes_lock(self):
        temp = Path(tempfile.mkdtemp(prefix='smtrust', dir='/tmp'))
        try:
            repo = temp / 'proj'; (repo / 'sub').mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            config = temp / '.claude.json'
            config.write_text(json.dumps({'numStartups': 3, 'projects': {os.path.realpath(repo): {'hasTrustDialogAccepted': False, 'x': 1}}}, indent=2))
            self.assertTrue(sm.trust_folder(str(repo / 'sub'), config=config))
            projects = json.loads(config.read_text())['projects']
            self.assertTrue(projects[os.path.realpath(repo)]['hasTrustDialogAccepted'])
            self.assertEqual(projects[os.path.realpath(repo)]['x'], 1, 'other fields of the record must stay')
            self.assertTrue(projects[os.path.realpath(repo / 'sub')]['hasTrustDialogAccepted'])
            self.assertEqual(json.loads(config.read_text())['numStartups'], 3)
            self.assertFalse(Path(str(config) + '.lock').exists(), 'the lock must be released')
            # Claude holds its lock: give up instead of writing under it.
            other = temp / 'other'; other.mkdir()
            Path(str(config) + '.lock').mkdir()
            started = time.time()
            self.assertFalse(sm.trust_folder(str(other), config=config))
            self.assertLess(time.time() - started, 5)
            self.assertNotIn(os.path.realpath(other), json.loads(config.read_text())['projects'])
        finally:
            shutil.rmtree(temp, ignore_errors=True)


class FolderWorkspaceTest(unittest.TestCase):
    def test_innermost_folder_wins_when_workspaces_nest(self):
        spaces = [dict(workspace='harness', cwd='/u/cld'), dict(workspace='lopin', cwd='/u/cld/lopin')]
        self.assertEqual(sm.folder_workspace('/u/cld/lopin', spaces), 'lopin')
        self.assertEqual(sm.folder_workspace('/u/cld/lopin/web', spaces), 'lopin')
        self.assertEqual(sm.folder_workspace('/u/cld/other', spaces), 'harness')
        self.assertIsNone(sm.folder_workspace('/u/elsewhere', spaces))
        self.assertEqual(sm.folder_workspace('/u/cld/lopin', list(reversed(spaces))), 'lopin')


if __name__ == '__main__':
    unittest.main(verbosity=1)
