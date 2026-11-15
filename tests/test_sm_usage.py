#!/usr/bin/env python3
"""sm usage / sm feedback: what agents ran and what got in their way, without keeping private values."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

# The installed layout (session layer, peer and peer-hold in one folder): tests/run.sh assembles it in SEOL_BIN.
BIN = Path(os.environ.get('SEOL_BIN') or Path(__file__).resolve().parents[1] / 'bin')
sys.path.insert(0, str(BIN))
import sm_usage  # noqa: E402


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='smu', dir='/tmp'))
        (self.temp / 'cfg').mkdir(); (self.temp / 'cfg/hostname').write_text('test')
        self.env = dict(os.environ, SM_STATE=str(self.temp / 'sm'), PEER_STATE=str(self.temp / 'peer'),
                        PEER_CONFIG=str(self.temp / 'cfg'), SM_APP_HOST='test', SM_FEEDBACK_TO='nobody-here',
                        PEER_NAME='w1')

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def sm(self, *args, env=None):
        return subprocess.run([sys.executable, str(BIN / 'sm-session'), *args], env=env or self.env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)

    def rows(self):
        path = self.temp / 'sm/usage.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_summary_keeps_names_not_values(self):
        s = sm_usage.summarize(['spawn', 'w1', '--kind', 'codex', '--cwd', '/secret/place', '--', 'a private brief'])
        self.assertEqual(s, dict(verb='spawn', sub=None, target='w1', flags=['--cwd', '--kind']))
        self.assertNotIn('secret', json.dumps(s))
        self.assertEqual(sm_usage.summarize(['run', 'arch', '--', 'cat', '/etc/x'])['target'], 'arch')
        self.assertEqual(sm_usage.summarize(['ws', 'new', 'flat', '--cwd', '/x'])['sub'], 'new')

    def test_records_agents_failures_but_not_the_apps_calls(self):
        self.sm('arange', 'grid')
        self.sm('tail', 'nosuch')
        app = {k: v for k, v in self.env.items() if k != 'PEER_NAME'}
        self.sm('tail', 'nosuch', env=app)  # the app: no PEER_NAME, no terminal
        rows = self.rows()
        self.assertEqual([(r['verb'], r['ok'], r['who']) for r in rows], [('arange', False, 'w1'), ('tail', False, 'w1')])
        self.assertIn('did you mean sm arrange', rows[0]['error'])
        report = self.sm('usage').stdout
        self.assertIn('recent failures', report)
        self.assertIn('sm arange', report)

    def test_feedback_help_is_not_sent_as_feedback(self):
        out = self.sm('feedback', '--help')
        self.assertIn('usage: sm feedback', out.stdout)
        self.assertFalse((self.temp / 'sm/feedback.jsonl').exists())

    def test_feedback_carries_the_commands_just_before(self):
        self.sm('arange', 'grid')
        out = self.sm('feedback', 'arrange 철자를', '자꾸 틀린다')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('kept for nobody-here', out.stdout)
        kept = json.loads((self.temp / 'sm/feedback.jsonl').read_text().splitlines()[-1])
        self.assertEqual((kept['from'], kept['text']), ('w1', 'arrange 철자를 자꾸 틀린다'))
        self.assertEqual(kept['recent'][-1]['verb'], 'arange')
        self.assertIn('자꾸 틀린다', self.sm('feedback').stdout)


if __name__ == '__main__':
    unittest.main(verbosity=1)
