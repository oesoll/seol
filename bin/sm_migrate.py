"""Move agents that live in tmux into sm, without losing a conversation.

adopt: read a tmux session (windows, split layout, panes), write it as an sm workspace on this
machine, and register each agent pane as an sm session under its peer name with its engine
session id. Nothing is stopped.

migrate: for one adopted agent that is idle, replace the tmux pane's process with
`sm-session attach NAME`. The agent process ends, sm resumes the same engine session
(resuming its conversation) under its own peer-hold, and the tmux pane keeps showing it as a plain viewer.
The layout people see does not change; the agent no longer depends on tmux. When the sm app
draws the workspace itself, the tmux viewer can simply be closed.
"""
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import time

import peer_core
import sm_layout as lay
import sm_session as sm

AGENT = re.compile(r'(^|/)(claude|codex)( |$)|/claude/versions/|(^| )codex (--|resume|-s)')


def processes():
    out = subprocess.run(['ps', '-eo', 'pid=,ppid=,args='], capture_output=True, text=True).stdout
    kids, args = {}, {}
    for line in out.splitlines():
        m = re.match(r'\s*(\d+)\s+(\d+)\s+(.*)', line)
        if m:
            pid, ppid = int(m[1]), int(m[2])
            args[pid] = m[3]
            kids.setdefault(ppid, []).append(pid)
    return kids, args


def agent_under(pid, kids, args):
    """The first claude/codex TUI process under a pane's shell (not an app-server helper)."""
    a = args.get(pid, '')
    # Skip the launcher itself (python …/peer-session claude …), not a TUI whose arguments mention a
    # peer-session temp path (channel mode passes --mcp-config /tmp/peer-session-…/mcp.json).
    launcher = re.match(r'\S*python[\d.]*\s+\S*peer-session\s', a)
    if AGENT.search(a) and 'app-server' not in a and not launcher:
        return pid, a
    for kid in kids.get(pid, []):
        found = agent_under(kid, kids, args)
        if found:
            return found
    return None


def session_id(kind, pid, cmd):
    """Engine session id of a running TUI: from its own resume argument, Claude's per-process
    session file, or the Codex rollout file the process holds open."""
    m = re.search(r'(?:-ry|resume(?: -s \S+)?|--resume|-r)\s+([0-9a-f]{8}-[0-9a-f-]{27})', cmd)
    if kind == 'claude':
        info = sm.read(Path.home() / '.claude/sessions' / (str(pid) + '.json'))
        if info.get('sessionId'):
            return info['sessionId']
    if m:
        return m[1]
    if kind == 'codex':
        fds = Path('/proc') / str(pid) / 'fd'
        targets = []
        if fds.exists():
            for fd in fds.iterdir():
                try:
                    targets.append(os.readlink(fd))
                except OSError:
                    pass
        else:
            out = subprocess.run(['lsof', '-p', str(pid), '-Fn'], capture_output=True, text=True).stdout
            targets = [l[1:] for l in out.splitlines() if l.startswith('n')]
        for t in targets:
            m = re.search(r'rollout-[^/]*?([0-9a-f]{8}-[0-9a-f-]{27})\.jsonl', t)
            if m:
                return m[1]
    for row in peer_core.sessions():  # last resort: whatever peer saw in this pane
        if (row.get('process') or {}).get('pid') == pid and row.get('session'):
            return row['session']
    return None


def tmux(*args):
    return subprocess.run(['tmux'] + list(args), capture_output=True, text=True).stdout


def parse_layout(text, panes):
    """tmux #{window_layout} → sm tree. 'csum,WxH,X,Y,ID' is a pane, '{…}' side by side, '[…]' stacked."""
    text = text.split(',', 1)[1]  # drop checksum
    pos = 0

    def node():
        nonlocal pos
        m = re.match(r'(\d+)x(\d+),\d+,\d+', text[pos:])
        w, h = int(m[1]), int(m[2])
        pos += m.end()
        if pos < len(text) and text[pos] == ',':
            m2 = re.match(r',(\d+)', text[pos:])
            pos += m2.end()
            return dict(pane=panes['%' + m2[1]]), w, h
        opener = text[pos]
        closer = '}' if opener == '{' else ']'
        pos += 1
        children = []
        while text[pos] != closer:
            child, cw, ch = node()
            children.append((child, cw, ch))
            if text[pos] == ',':
                pos += 1
        pos += 1
        axis = 'row' if opener == '{' else 'col'
        total = sum(cw if axis == 'row' else ch for _, cw, ch in children) or 1
        return dict(dir=axis, children=[dict(c, size=(cw if axis == 'row' else ch) / total) for c, cw, ch in children]), w, h

    tree, _, _ = node()
    return lay.normalize(tree)


def adopt(tmux_session, ws_name=None, dry_run=False, names=None):
    ws_name = ws_name or re.sub(r'[^\w.-]+', '-', tmux_session)
    kids, args = processes()
    rows = {r.get('session'): r for r in peer_core.sessions() if r.get('session')}
    windows = tmux('list-windows', '-t', '=' + tmux_session + ':', '-F', '#{window_index}\t#{window_name}\t#{window_layout}\t#{window_active}')
    if not windows.strip():
        raise ValueError('no tmux session ' + tmux_session)
    panes_out = tmux('list-panes', '-s', '-t', '=' + tmux_session + ':', '-F',
                     '#{pane_id}\t#{window_index}\t#{pane_pid}\t#{pane_current_path}\t#{@name}\t#{pane_active}')
    ids, info, agents = {}, {}, []
    for line in panes_out.splitlines():
        pane, win, pid, cwd, label, active = line.split('\t')
        pid_ = 'p-' + secrets.token_hex(8)
        ids[pane] = pid_
        found = agent_under(int(pid), kids, args)
        if not found:
            info[pid_] = dict(session=None, shell=True, tmux_pane=pane)
            continue
        apid, cmd = found
        kind = 'codex' if re.search(r'(^|/)codex', cmd) else 'claude'
        sid = session_id(kind, apid, cmd)
        row = rows.get(sid) or {}
        agents.append(dict(name=row.get('name'), label=label, win=win, pane_id=pid_, kind=kind, session=sid, cwd=cwd,
                           tmux_pane=pane, pid=apid, cmd=cmd, peer_state=row.get('state')))
    names = names or {}
    for a in agents:
        # An explicit name wins (e.g. lane-<key> for a supervised lane); then peer's name; then the pane
        # label; else the tmux session itself when it holds one agent.
        if a['tmux_pane'] in names:
            a['name'] = sm.peer_core.valid_name(names[a['tmux_pane']])
        if not a['name']:
            tail = a['label'] or ('' if len(agents) == 1 else 'w' + a['win'] + '.' + a['tmux_pane'][1:])
            a['name'] = re.sub(r'[^\w.-]+', '-', tmux_session + ('-' + tail if tail else ''))[:64]
        info[a.pop('pane_id')] = dict(session=a['name'], shell=False, tmux_pane=a['tmux_pane'])
        a.pop('label'); a.pop('win')
    tabs, active_tab = [], None
    for line in windows.splitlines():
        win, wname, layout, wactive = line.split('\t')
        tab_panes = [p for p in panes_out.splitlines() if p.split('\t')[1] == win]
        focus = next((ids[p.split('\t')[0]] for p in tab_panes if p.split('\t')[5] == '1'), ids[tab_panes[0].split('\t')[0]])
        tab = dict(id='t-' + secrets.token_hex(8), name=wname, focus=focus, zoom=None, root=parse_layout(layout, ids))
        tabs.append(tab)
        if wactive == '1':
            active_tab = tab['id']
    root_cwd = os.path.commonpath([a['cwd'] for a in agents]) if agents else tmux('display', '-p', '-t', '=' + tmux_session + ':', '#{pane_current_path}').strip()
    ws = dict(schema=lay.SCHEMA, workspace=ws_name, host=peer_core.host(), cwd=root_cwd, version=1, updated=time.time(),
              active_tab=active_tab or tabs[0]['id'], tabs=tabs, panes=info, adopted_from='tmux:' + tmux_session)
    if dry_run:
        return dict(workspace=ws_name, tabs=[t['name'] for t in tabs], agents=[dict(a, ready=bool(a['session'])) for a in agents],
                    dry_run=True)
    existing = sm.read(lay.ws_path(ws_name))
    if existing:
        ws['version'] = existing.get('version', 0) + 1
    sm.write(lay.ws_path(ws_name), ws)
    registered = []
    for a in agents:
        entry = sm.read(sm.entry_path(a['name']))
        if entry and entry.get('session') not in (None, a['session']):
            raise ValueError(a['name'] + ' is already an sm session for a different engine session')
        hold = entry.get('hold') if entry else str(sm.state_dir() / 'hold' / (secrets.token_hex(4) + '.sock'))
        env = ['PEER_NAME=' + a['name'], 'PEER_STATE=' + str(peer_core.root()), 'TMUX_BRIDGE_CONFIG=' + str(peer_core.config()),
               'SM_WORKSPACE=' + ws_name, 'PEER_VISIBLE=1']
        sm.write(sm.entry_path(a['name']), dict(name=a['name'], kind=a['kind'], host=peer_core.host(), cwd=a['cwd'], hold=hold,
                                                session=a['session'], args=[], env=env, pane=None, visible=True, parent=None,
                                                created=time.time(), tmux_pane=a['tmux_pane'], agent_pid=a['pid'], adopted=True))
        registered.append(dict(a, ready=bool(a['session'])))
    return dict(workspace=ws_name, tabs=[t['name'] for t in tabs], agents=registered)


def input_line(pane):
    """Text typed but not sent in a Claude (❯) or Codex (›) input line, else ''. Placeholder hints are not drafts."""
    screen = tmux('capture-pane', '-p', '-t', pane).splitlines()
    for line in reversed(screen):
        m = re.match(r'^\s*[│ ]*\s*(❯|›)\s?(.*?)\s*[│ ]*$', line)
        if m:
            text = m[2].strip()
            if text.startswith(('Ask Codex', 'Try "', 'Implement {feature}', 'Find and fix', 'Explain this', 'Write tests')):
                return ''
            return text
    return ''


def migrate(name, force=False):
    """Idle agent in a tmux pane → same engine session under peer-hold; the pane becomes its viewer."""
    entry = sm.load(name)
    pane = entry.get('tmux_pane')
    if not pane:
        raise ValueError(name + ' was not adopted from tmux')
    if sm.status(entry) == 'running':
        return dict(name=name, action='already-in-sm')
    if not entry.get('session'):
        raise ValueError(name + ': engine session id unknown; cannot resume it, so it is left in tmux')
    row = next((peer_core.public(r) for r in peer_core.sessions() if r.get('session') == entry['session']), None)
    state = (row or {}).get('state')
    if not row and entry.get('kind') == 'claude' and entry.get('agent_pid'):
        # Not registered with peer (started before the hook): Claude's own per-process file says busy/idle.
        state = sm.read(Path.home() / '.claude/sessions' / (str(entry['agent_pid']) + '.json')).get('status')
    if state is None:
        # Neither peer nor the engine says: read the screen. Both TUIs show "esc to interrupt" while a turn runs.
        screen = tmux('capture-pane', '-p', '-t', pane)
        if screen.strip() and 'esc to interrupt' not in screen and 'Working' not in screen.splitlines()[-6:].__str__():
            state = 'idle'
    if state != 'idle' and not force:
        raise ValueError(name + ' is ' + str(state) + '; migrate only idle agents (a turn in progress would be cut), or --force')
    draft = input_line(pane)
    if draft and not force:
        raise ValueError(name + ' has unsent text in its input line (' + draft[:40] + '…); moving it would drop that draft')
    entry['migrated'] = time.time()  # from here the tmux pane is only a viewer; attach must resume, not show tmux
    sm.write(sm.entry_path(name), entry)
    viewer = shlex.join([str(sm.BIN / 'sm-session'), 'attach', name, '--passive'])  # sm panels own the size
    # respawn-pane -k ends the agent in that pane and runs the viewer in the same place; the viewer's
    # attach resumes the engine session under sm before showing it.
    result = subprocess.run(['tmux', 'respawn-pane', '-k', '-t', pane, viewer], capture_output=True, text=True)
    if result.returncode:
        raise ValueError('tmux respawn-pane failed: ' + result.stderr.strip())
    end = time.monotonic() + 30
    while time.monotonic() < end and sm.status(sm.load(name)) != 'running':
        time.sleep(.3)
    entry = sm.load(name)
    entry['migrated'] = time.time()
    sm.write(sm.entry_path(name), entry)
    return dict(name=name, action='migrated' if sm.status(entry) == 'running' else 'pending', status=sm.status(entry),
                session=entry['session'], hold=entry['hold'], viewer=pane)
