#!/usr/bin/env python3
"""Add/remove only Fine hooks; preserve every existing setting and back up edits."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile
import time

MARKER = 'fine-attention-hook.py'
COMMON = ['UserPromptSubmit', 'PermissionRequest', 'PreToolUse', 'PostToolUse', 'Stop', 'SessionEnd']


def update(path, command, events, remove=False):
    config = json.loads(path.read_text()) if path.exists() else {}
    hooks = config.setdefault('hooks', {})
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            group = dict(group)
            group['hooks'] = [hook for hook in group.get('hooks', []) if MARKER not in hook.get('command', '')]
            if group['hooks']:
                groups.append(group)
        if groups: hooks[event] = groups
        else: hooks.pop(event)
    if not remove:
        for event in events:
            group = {'hooks': [{'type': 'command', 'command': command, 'timeout': 3}]}
            if event == 'PreToolUse': group['matcher'] = 'AskUserQuestion|request_user_input'
            if event == 'Notification': group['matcher'] = 'permission_prompt|elicitation_dialog'
            hooks.setdefault(event, []).append(group)
    text = json.dumps(config, ensure_ascii=False, indent=2) + '\n'
    if path.exists() and text == path.read_text(): return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists(): shutil.copy2(path, str(path) + '.fine-backup-' + str(time.time_ns()))
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.fine-hooks-')
    try:
        with os.fdopen(fd, 'w') as stream: stream.write(text)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--host', default='local')
    parser.add_argument('--remove', action='store_true')
    args = parser.parse_args()
    script = Path(__file__).with_name(MARKER).resolve()
    for harness, path, events in [
        ('claude', args.home / '.claude/settings.json', COMMON + ['Notification', 'StopFailure', 'SessionStart']),
        ('codex', args.home / '.codex/hooks.json', COMMON + ['Interrupt'])
    ]:
        command = shlex.join(['python3', str(script), '--host', args.host, '--harness', harness])
        update(path, command, events, args.remove)
        print(path)
    if not args.remove:
        print('Codex: review and trust the new hooks using /hooks. Existing permissions are unchanged.')


if __name__ == '__main__': main()
