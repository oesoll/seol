#!/usr/bin/env python3
"""Claude/Codex hook adapter. Emits status only; never approves, blocks or slows a tool.

The hook does not guess "finished". A turn that ends may only be waiting for
another agent's result and wakes by itself. Whether a session has
really stopped and needs the user is the agent's call, made with `seol-notch` (agent
harness). The hook reports only states the agent cannot leave by itself: a
permission or question shown to the user, and a turn that failed. Any later event of
that session clears it again.

Delivery never runs in the hook: it records the desired state and returns. A
detached sender delivers the latest state per session (older ones are skipped),
so an unreachable or slow Mac cannot hit the harness hook timeout (10/30:
"UserPromptSubmit hook ... timed out after 3s").
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

CLEARABLE = ('waiting', 'blocked', 'error', 'working', 'unknown')  # working/unknown: left by the old hook
SEND_TIMEOUT = 12
NOTCH_KEY = 'seol-notch'  # item posted by the harness command seol-notch from the same session


def event_state(event):
    kind = event.get('hook_event_name')
    tool = event.get('tool_name', '')
    if kind == 'PermissionRequest':
        return 'waiting', '권한 확인이 필요합니다: ' + tool
    if kind == 'PreToolUse' and tool.split('.')[-1] in ('AskUserQuestion', 'request_user_input'):
        questions = event.get('tool_input', {}).get('questions', [])
        message = '\n'.join(q.get('question', q.get('title', '')) for q in questions if isinstance(q, dict))
        return 'waiting', message or '질문에 답변이 필요합니다.'
    if kind == 'Notification' and event.get('notification_type') in ('permission_prompt', 'elicitation_dialog'):
        return 'waiting', event.get('message') or '사용자 확인이 필요합니다.'
    if kind == 'StopFailure':
        return 'error', '응답을 마치지 못했습니다. 세션을 확인해 주세요.'
    return None


def state_root():
    root = Path(os.environ.get('TMUX_BRIDGE_STATE', str(Path.home() / '.smux/state/bridge'))) / 'notifications'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def pane_source():
    """This session's mark; seol-notch computes the same one. sm (peer-hold) sessions have no tmux pane
    or Fine tab, only PEER_NAME: without it every sm session shared one empty mark, so one session's
    next input cleared another's notice (10/31)."""
    env = os.environ
    if env.get('TMUX_PANE'):
        return env['TMUX_PANE']
    if env.get('FINE_TAB_ID'):
        return 'fine:' + env['FINE_TAB_ID']
    return 'peer:' + env['PEER_NAME'] if env.get('PEER_NAME') else ''


def from_agent(prompt):
    """A peer message arrives as a user turn too. It is not the user answering, so it must not clear the notice
    the user hasn't seen yet (busy teams like flat message each other all the time)."""
    head = prompt.lstrip()[:200]
    return head.startswith(('<cross-session-message', '[from ', '[smux ')) or '[peer-id:' in prompt[-200:]


def sm_source(env=os.environ):
    """An sm session's return address for the notch (opened with `sm reveal`); tmux/Fine sessions have one already."""
    if env.get('TMUX_PANE') or env.get('FINE_TAB_ID') or not (env.get('PEER_HOLD_SOCK') and env.get('PEER_NAME')):
        return []
    return ['--source', 'sm:' + env['PEER_NAME']]


def notch_marker(root, pane):
    """Written by seol-notch; read here so the next input in that pane clears its notice."""
    return root / ('seol-notch-' + hashlib.sha256(pane.encode()).hexdigest()[:32] + '.json')


@contextmanager
def locked(path):
    with path.open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)  # held only around small file reads/writes, never a send
        stream.seek(0)
        try:
            data = json.load(stream)
        except (ValueError, OSError):
            data = {}
        holder = {'data': data, 'dirty': False}
        yield holder
        if holder['dirty']:
            stream.seek(0); stream.truncate()
            json.dump(holder['data'], stream)
            stream.flush()


def enqueue(cache, host, key, state, message, title):
    with locked(cache) as h:
        data = h['data']
        data.update(host=host, key=key, state=state, at=time.time(), seq=data.get('seq', 0) + 1,
                    pending={'state': state, 'message': message[:1800], 'title': title[:100]})
        h['dirty'] = True
    subprocess.Popen([sys.executable, os.path.abspath(__file__), '--send', str(cache)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def send(cache):
    """Detached: deliver the newest pending state; one sender per item at a time."""
    bridge = os.environ.get('PEER_BIN', os.environ.get('FINE_BRIDGE_BIN', str(Path.home() / '.smux/bin/peer')))
    lock = cache.with_suffix('.send')
    while True:
        with lock.open('a') as guard:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return  # the running sender reads the newest state before it exits
            while True:
                with locked(cache) as h:
                    data = h['data']
                    if data.get('seq', 0) <= data.get('sent', 0):
                        break
                    job, seq = data['pending'], data['seq']
                try:
                    ok = subprocess.run([bridge, 'notify', data['host'], job['message'], '--id', data['key'],
                                         '--state', job['state'], '--title', job['title']] + sm_source(),
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=SEND_TIMEOUT).returncode == 0
                except (OSError, subprocess.SubprocessError):
                    ok = False
                with locked(cache) as h:
                    # An undelivered state is dropped, not retried: the next event brings a fresher one.
                    h['data']['sent'] = max(h['data'].get('sent', 0), seq)
                    h['data']['delivered'] = ok
                    h['dirty'] = True
        with locked(cache) as h:  # a state queued while this sender was releasing its lock
            if h['data'].get('seq', 0) <= h['data'].get('sent', 0):
                return


PEER_BIN = Path(__file__).resolve().parents[1] / 'bin'  # ~/.smux/bin beside ~/.smux/hooks (the Mac calls this through a link)


def peer_step(event, harness):
    """Register this session for `peer` and hand a running Claude its waiting peer mail.

    Lives in this hook instead of a hook of its own: Codex trusts hooks by command hash, so a
    new hook entry would sit untrusted until someone approves it in /hooks. Local files only."""
    if harness not in ('claude', 'codex'):
        return
    try:
        sys.path.insert(0, str(PEER_BIN))
        import peer_core
        row = peer_core.observe(event, harness)
        if row:
            peer_core.prompt_receipts(row, event)
            output = peer_core.hook_deliver(row, event)
            if output:
                print(json.dumps(output, ensure_ascii=False))
    except Exception as error:  # noqa: BLE001 — peer must never break the turn or the notice below
        try:
            with (state_root() / 'hook-errors.log').open('a') as log:
                log.write('%s peer %s\n' % (time.strftime('%Y-%m-%dT%H:%M:%S'), type(error).__name__))
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='local')
    parser.add_argument('--harness', default='agent')
    parser.add_argument('--send', type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.send:
        return send(options.send)
    event = json.load(sys.stdin)
    peer_step(event, options.harness)
    session = event.get('session_id')
    # Subagents share their parent's terminal address; mixing their events
    # would hide a still-running parent. They can send a separate explicit task ID.
    if not session or event.get('agent_id'):
        return
    root = state_root()
    if event.get('hook_event_name') == 'UserPromptSubmit' and not from_agent(event.get('prompt') or ''):
        marker = notch_marker(root, pane_source())
        if marker.exists():
            try:
                notch = json.loads(marker.read_text())
                marker.unlink()
                enqueue(root / (marker.stem + '.cache.json'), notch.get('host', options.host), notch.get('key', NOTCH_KEY),
                        'done', '다시 진행하고 있어요.', notch.get('title', '에이전트'))
            except (ValueError, OSError):
                pass
    key = options.harness + ':' + session
    cache = root / (hashlib.sha256(key.encode()).hexdigest() + '.json')
    state = event_state(event)
    with locked(cache) as h:
        previous = h['data']
    if state:
        if (previous.get('state'), (previous.get('pending') or {}).get('message')) == state:
            return
    elif previous.get('state') in CLEARABLE:
        state = 'done', '이어서 진행하고 있어요.'
    else:
        return
    title = Path(event.get('cwd', '.')).name + ' · ' + options.harness
    enqueue(cache, options.host, key, state[0], state[1], title)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:  # noqa: BLE001 — a notification problem must never reach the agent
        try:
            with (state_root() / 'hook-errors.log').open('a') as log:
                log.write('%s %s\n' % (time.strftime('%Y-%m-%dT%H:%M:%S'), type(error).__name__))
        except OSError:
            pass
