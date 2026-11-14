#!/usr/bin/env python3
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'hooks'
spec = importlib.util.spec_from_file_location('attention_hook', ROOT / 'fine-attention-hook.py')
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


class AttentionHookTests(unittest.TestCase):
    def test_only_states_the_agent_cannot_leave_by_itself(self):
        self.assertIsNone(hook.event_state({'hook_event_name': 'PostToolUseFailure'}))
        self.assertIsNone(hook.event_state({'hook_event_name': 'Notification', 'notification_type': 'idle_prompt'}))
        self.assertEqual(hook.event_state({'hook_event_name': 'PermissionRequest', 'tool_name': 'Bash'})[0], 'waiting')
        self.assertEqual(hook.event_state({'hook_event_name': 'PreToolUse', 'tool_name': 'AskUserQuestion',
                                          'tool_input': {'questions': [{'question': 'A 또는 B?'}]}}), ('waiting', 'A 또는 B?'))
        self.assertEqual(hook.event_state({'hook_event_name': 'StopFailure'})[0], 'error')
        # A turn that ends may be waiting for another agent and wake by itself: never "done" from the hook.
        for kind in ('UserPromptSubmit', 'PostToolUse', 'Stop', 'SessionEnd', 'Interrupt'):
            self.assertIsNone(hook.event_state({'hook_event_name': kind, 'last_assistant_message': '끝났어요.'}), kind)

    def test_install_is_idempotent_and_remove_preserves_other_hooks_and_settings(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            paths = [root / '.claude/settings.json', root / '.codex/hooks.json']
            original = {'permissions': {'allow': ['Read']}, 'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'keep-this'}]}]}}
            for path in paths:
                path.parent.mkdir()
                path.write_text(json.dumps(original))
            args = ['python3', str(ROOT / 'install-attention-hooks.py'), '--home', str(root), '--host', 'mac']
            subprocess.run(args, check=True, capture_output=True)
            first = [path.read_text() for path in paths]
            subprocess.run(args, check=True, capture_output=True)
            self.assertEqual(first, [path.read_text() for path in paths])
            for path in paths:
                current = json.loads(path.read_text())
                self.assertEqual(current['permissions'], original['permissions'])
                self.assertEqual(len(current['hooks']['Stop']), 2)
                self.assertTrue(list(path.parent.glob(path.name + '.fine-backup-*')))
            subprocess.run(args + ['--remove'], check=True, capture_output=True)
            for path in paths: self.assertEqual(json.loads(path.read_text()), original)


class DeliveryTests(unittest.TestCase):
    """The real hook process against a fake bridge: it must return at once whatever the bridge does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.log = self.root / 'bridge.log'
        self.bridge = self.root / 'tmux-bridge'
        self.bridge.write_text('#!/bin/sh\nsleep "${FAKE_SLEEP:-0}"\nprintf "%s\\n" "$*" >> "$FAKE_LOG"\nexit "${FAKE_EXIT:-0}"\n')
        self.bridge.chmod(0o755)
        self.env = dict(os.environ, FINE_BRIDGE_BIN=str(self.bridge), FAKE_LOG=str(self.log),
                        TMUX_BRIDGE_STATE=str(self.root / 'state'), PEER_STATE=str(self.root / 'peer'), TMUX_PANE='%77')
        for k in ('FINE_TAB_ID', 'PEER_HOLD_SOCK', 'PEER_NAME', 'SM_PANE_ID', 'SM_WORKSPACE'):  # the suite may run inside an sm session
            self.env.pop(k, None)

    def tearDown(self):
        time.sleep(0.05); self.tmp.cleanup()

    def run_hook(self, event, session='s1', **env):
        started = time.monotonic()
        result = subprocess.run(['python3', str(ROOT / 'fine-attention-hook.py'), '--host', 'mac', '--harness', 'claude'],
                                input=json.dumps({'session_id': session, 'cwd': '/w/proj', **event}),
                                env=dict(self.env, **env), capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        return time.monotonic() - started

    def sent(self, settle=0.6):
        time.sleep(settle)
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_turn_end_alone_sends_nothing(self):
        for kind in ('UserPromptSubmit', 'PostToolUse', 'Stop', 'SessionEnd'):
            self.run_hook({'hook_event_name': kind, 'last_assistant_message': '다 끝났어요.'})
        self.assertEqual(self.sent(), [])

    def test_permission_is_announced_and_cleared_by_the_next_event(self):
        self.run_hook({'hook_event_name': 'PermissionRequest', 'tool_name': 'Bash'})
        self.assertEqual(self.sent(), ['notify mac 권한 확인이 필요합니다: Bash --id claude:s1 --state waiting --title proj · claude'])
        self.run_hook({'hook_event_name': 'PermissionRequest', 'tool_name': 'Bash'})  # same prompt again: no resend
        self.run_hook({'hook_event_name': 'PostToolUse'})
        self.run_hook({'hook_event_name': 'Stop'})
        lines = self.sent()
        self.assertEqual(len(lines), 2); self.assertIn('--state done', lines[1])

    def test_unreachable_or_hanging_mac_never_delays_the_hook(self):
        for env in ({'FAKE_SLEEP': '20'}, {'FAKE_EXIT': '1'}):
            for kind in ('UserPromptSubmit', 'PermissionRequest', 'PostToolUse', 'StopFailure', 'Stop'):
                self.assertLess(self.run_hook({'hook_event_name': kind, 'tool_name': 'Bash'}, session='slow' + str(len(env)), **env), 0.5)

    def test_latest_state_wins_while_a_send_is_slow(self):
        self.run_hook({'hook_event_name': 'PermissionRequest', 'tool_name': 'A'}, FAKE_SLEEP='1')
        self.run_hook({'hook_event_name': 'PostToolUse'}, FAKE_SLEEP='1')
        self.run_hook({'hook_event_name': 'StopFailure'}, FAKE_SLEEP='1')
        lines = self.sent(settle=3.5)
        self.assertEqual([l.split('--state ')[1].split()[0] for l in lines], ['waiting', 'error'])  # done was superseded

    def test_old_hook_working_state_is_cleared_once(self):
        key = 'claude:s1'
        cache = Path(self.env['TMUX_BRIDGE_STATE']) / 'notifications' / (hashlib.sha256(key.encode()).hexdigest() + '.json')
        cache.parent.mkdir(parents=True); cache.write_text(json.dumps({'state': 'working', 'at': 1}))
        self.run_hook({'hook_event_name': 'PostToolUse'}); self.run_hook({'hook_event_name': 'PostToolUse'})
        lines = self.sent(); self.assertEqual(len(lines), 1); self.assertIn('--state done', lines[0])

    def test_next_input_in_the_pane_clears_a_seol_notch_notice(self):
        root = Path(self.env['TMUX_BRIDGE_STATE']) / 'notifications'; root.mkdir(parents=True)
        marker = root / ('seol-notch-' + hashlib.sha256(b'%77').hexdigest()[:32] + '.json')
        marker.write_text(json.dumps({'host': 'mac', 'title': 'proj'}))
        self.run_hook({'hook_event_name': 'UserPromptSubmit'}, TMUX_PANE='%78')  # another pane: untouched
        self.assertTrue(marker.exists())
        self.run_hook({'hook_event_name': 'UserPromptSubmit'})
        self.assertFalse(marker.exists())
        self.assertEqual(self.sent(), ['notify mac 다시 진행하고 있어요. --id seol-notch --state done --title proj'])

    def test_sm_sessions_without_a_pane_clear_only_their_own_notice(self):
        root = Path(self.env['TMUX_BRIDGE_STATE']) / 'notifications'; root.mkdir(parents=True)
        mark = lambda name: root / ('seol-notch-' + hashlib.sha256(('peer:' + name).encode()).hexdigest()[:32] + '.json')
        for name in ('harness-c1', 'harness-w1'):
            mark(name).write_text(json.dumps({'host': 'mac', 'title': name, 'key': 'seol-notch:peer:' + name}))
        self.run_hook({'hook_event_name': 'UserPromptSubmit'}, TMUX_PANE='', PEER_NAME='harness-w1')
        self.assertTrue(mark('harness-c1').exists()); self.assertFalse(mark('harness-w1').exists())
        self.assertEqual(self.sent(), ['notify mac 다시 진행하고 있어요. --id seol-notch:peer:harness-w1 --state done --title harness-w1'])

    def test_sm_session_notices_carry_their_return_address(self):
        root = Path(self.env['TMUX_BRIDGE_STATE']) / 'notifications'; root.mkdir(parents=True)
        mark = root / ('seol-notch-' + hashlib.sha256(b'peer:flat-qa').hexdigest()[:32] + '.json')
        mark.write_text(json.dumps({'host': 'mac', 'title': 'flat-qa', 'key': 'seol-notch:peer:flat-qa'}))
        sock = self.root / 'h.sock'
        (self.root / 'h.json').write_text(json.dumps({'holder': os.getpid()}))  # its holder is an ancestor of the hook
        self.run_hook({'hook_event_name': 'UserPromptSubmit'}, TMUX_PANE='', PEER_NAME='flat-qa', PEER_HOLD_SOCK=str(sock))
        self.assertEqual(self.sent(), ['notify mac 다시 진행하고 있어요. --id seol-notch:peer:flat-qa --state done --title flat-qa --source sm:flat-qa'])

    def test_another_agents_message_does_not_clear_seols_notice(self):
        root = Path(self.env['TMUX_BRIDGE_STATE']) / 'notifications'; root.mkdir(parents=True)
        marker = root / ('seol-notch-' + hashlib.sha256(b'%77').hexdigest()[:32] + '.json')
        marker.write_text(json.dumps({'host': 'mac', 'title': 'proj'}))
        for prompt in ('<cross-session-message from="peer:flat-coord@arch">\nqa 끝났어?\n[peer-id:abc]\n</cross-session-message>',
                       '[from flat-coord@arch] qa 끝났어?', '[smux claude@%31] 보고'):
            self.run_hook({'hook_event_name': 'UserPromptSubmit', 'prompt': prompt})
            self.assertTrue(marker.exists(), prompt)
        self.run_hook({'hook_event_name': 'UserPromptSubmit', 'prompt': 'B안으로 가자'})  # the user answers
        self.assertFalse(marker.exists())


if __name__ == '__main__': unittest.main()
