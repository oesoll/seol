"""sm layout — workspaces, tabs and panes as data (what tmux kept in its server).

A workspace is one project. It has tabs; a tab is a tree of splits whose leaves are panes; a pane
shows one session (an agent by its sm name, or a shell). The layout lives on the machine where the
workspace runs (~/.smux/state/sm/workspaces/<ws>.json), so it exists whether or not any app shows
it, and agents there change it with the same commands a person would: sm split / resize / focus /
swap / zoom / close / tab. The sm app only renders this file and edits it through these commands.

Tree: leaf {"pane": id}; branch {"dir": "row"|"col", "children": [{"size": f, ...node}, ...]}.
row = side by side (tmux split -h), col = stacked (split -v). Sizes in a branch sum to 1.
"""
import contextlib
import difflib
import math
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import time

import sm_session as sm

SCHEMA = 1


def ws_dir():
    return sm.state_dir() / 'workspaces'


def ws_path(name):
    return ws_dir() / (name + '.json')


@contextlib.contextmanager
def editing(name):
    """Lock, load, yield, bump version, save. Every change goes through here: one writer at a time."""
    p = ws_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(str(p) + '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ws = sm.read(p)
        if not ws:
            raise ValueError('no workspace ' + name + '; sm ws new ' + name)
        yield ws
        ws['version'] = ws.get('version', 0) + 1
        ws['updated'] = time.time()
        sm.write(p, ws)


def load(name):
    ws = sm.read(ws_path(name))
    if not ws:
        raise ValueError('no workspace ' + name + ' on this machine; sm ws ls')
    return ws


def new_id(prefix):
    # 8 bytes: pane ids become SM_PANE_ID and peer targets (sm:<id>), so they must not collide across
    # workspaces and machines.
    return prefix + '-' + secrets.token_hex(8)


# ---------- workspaces and tabs ----------

def ws_new(name, cwd):
    sm.peer_core.valid_name(name)
    p = ws_path(name)
    if p.exists():
        raise ValueError('workspace exists: ' + name)
    cwd = str(Path(cwd).expanduser().resolve())
    tab, pane = new_id('t'), new_id('p')
    ws = dict(schema=SCHEMA, workspace=name, host=sm.peer_core.host(), cwd=cwd, version=1, updated=time.time(),
              active_tab=tab, tabs=[dict(id=tab, name='main', focus=pane, zoom=None, root=dict(pane=pane))],
              panes={pane: dict(session=None, shell=True)})
    sm.write(p, ws)
    return ws


def ws_rm(name):
    """Remove a workspace's layout. Its sessions keep running in the background (closing a view never ends
    an agent; sm stop does). Returns the sessions it showed."""
    p = ws_path(name)
    with open(str(p) + '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ws = sm.read(p)
        if not ws:
            names = [w['workspace'] for w in ws_list()]
            raise ValueError('no workspace ' + name + hint(name, names) + '; workspaces: ' + ', '.join(names))
        p.unlink()
    try:
        Path(str(p) + '.lock').unlink()
    except OSError:
        pass
    return dict(workspace=name, sessions=sorted({i['session'] for i in ws['panes'].values() if i.get('session')}))


def ws_list():
    folder = ws_dir()
    return [sm.read(p) for p in sorted(folder.glob('*.json'))] if folder.exists() else []


def tab_new(ws_name, name=None, after=None, session=None, focus=False):
    """A tab with one pane; with session, that pane shows it from the start (no half-made tab).
    focus: also make it the tab the user sees. Off by default (tmux's -d): an agent arranging panes must not pull
    the user's screen and keys away from what the user is typing into; `sm show NAME`, `sm tab select` do that on purpose."""
    with editing(ws_name) as ws:
        if session:
            for other in ws['panes'].values():
                if other.get('session') == session:
                    raise ValueError(session + ' is already shown in this workspace')
        tab, pane = new_id('t'), new_id('p')
        entry = dict(id=tab, name=name or session or 'tab' + str(len(ws['tabs']) + 1), focus=pane, zoom=None, root=dict(pane=pane))
        index = len(ws['tabs']) if after is None else find_tab(ws, after)[0] + 1
        ws['tabs'].insert(index, entry)
        ws['panes'][pane] = dict(session=session, shell=not session)
        if focus:
            ws['active_tab'] = tab
    return entry


def find_tab(ws, ref):
    for i, tab in enumerate(ws['tabs']):
        if ref in (tab['id'], tab['name']) or ref == str(i):
            return i, tab
    names = [t['name'] for t in ws['tabs']]
    raise ValueError('no tab ' + str(ref) + ' in ' + ws['workspace'] + hint(str(ref), names) + '; tabs: ' + ', '.join(names))


def hint(ref, candidates):
    close = difflib.get_close_matches(ref, candidates, n=1, cutoff=.6)
    return ' (did you mean ' + close[0] + '?)' if close else ''


def tab_select(ws_name, ref):
    with editing(ws_name) as ws:
        ws['active_tab'] = find_tab(ws, ref)[1]['id']


def tab_rename(ws_name, ref, name):
    with editing(ws_name) as ws:
        find_tab(ws, ref)[1]['name'] = name


def tab_move(ws_name, ref, where):
    """where: an index (0 = first), first, last, left, right, or before:TAB / after:TAB."""
    with editing(ws_name) as ws:
        i, tab = find_tab(ws, ref)
        where = str(where)
        if where.startswith(('before:', 'after:')):
            side, other = where.split(':', 1)
            j = find_tab(ws, other)[0]
            ws['tabs'].pop(i)
            j = j - 1 if j > i else j
            ws['tabs'].insert(j + (side == 'after'), tab)
            return
        ws['tabs'].pop(i)
        named = dict(first=0, last=len(ws['tabs']), left=i - 1, right=i + 1)
        if where in named:
            index = named[where]
        elif re.fullmatch(r'-?\d+', where):
            index = int(where)
        else:
            raise ValueError('sm tab move TAB INDEX|first|last|left|right|--before TAB|--after TAB (got ' + where + ')')
        ws['tabs'].insert(max(0, min(index, len(ws['tabs']))), tab)


def tab_close(ws_name, ref):
    """Closing a tab detaches its sessions (they keep running in the background)."""
    with editing(ws_name) as ws:
        if len(ws['tabs']) == 1:
            raise ValueError('a workspace keeps at least one tab')
        i, tab = find_tab(ws, ref)
        for pane in leaves(tab['root']):
            ws['panes'].pop(pane, None)
        ws['tabs'].pop(i)
        if ws['active_tab'] == tab['id']:
            ws['active_tab'] = ws['tabs'][min(i, len(ws['tabs']) - 1)]['id']


# ---------- tree helpers ----------

def leaves(node):
    if 'pane' in node:
        return [node['pane']]
    return [p for child in node['children'] for p in leaves(child)]


def locate(node, pane, parent=None, index=None):
    """(node, parent, index in parent) for the leaf holding pane."""
    if node.get('pane') == pane:
        return node, parent, index
    for i, child in enumerate(node.get('children', [])):
        found = locate(child, pane, node, i)
        if found:
            return found
    return None


def path_to(node, pane, trail=()):
    if node.get('pane') == pane:
        return list(trail)
    for i, child in enumerate(node.get('children', [])):
        found = path_to(child, pane, trail + ((node, i),))
        if found is not None:
            return found
    return None


def normalize(node):
    """Collapse single-child branches, merge same-direction nesting, keep sizes summing to 1."""
    if 'pane' in node:
        return node
    kids = []
    for child in node['children']:
        size = child.get('size', 1 / len(node['children']))
        child = normalize(child)
        if 'dir' in child and child['dir'] == node['dir']:
            for grand in child['children']:
                kids.append(dict(grand, size=grand['size'] * size))
        else:
            kids.append(dict(child, size=size))
    if len(kids) == 1:
        only = dict(kids[0]); only.pop('size', None)
        return only
    total = sum(k['size'] for k in kids) or 1
    for k in kids:
        k['size'] = round(k['size'] / total, 6)
    return dict(dir=node['dir'], children=kids)


def pane_order(ws, tab):
    return leaves(tab['root'])


def resolve(ws, ref):
    """A pane by session name (flat-qa), pane id (p-1a2b3c), or TAB.N (main.0)."""
    for tab in ws['tabs']:
        for pane in leaves(tab['root']):
            info = ws['panes'].get(pane, {})
            if ref == pane or (info.get('session') and ref in (info['session'], info['session'].split('@')[0])):
                return tab, pane
    m = re.fullmatch(r'(.+)\.(\d+)', ref)
    if m:
        _, tab = find_tab(ws, m.group(1))
        order = pane_order(ws, tab)
        if int(m.group(2)) < len(order):
            return tab, order[int(m.group(2))]
    names = [describe_pane(ws, tab, p) for tab in ws['tabs'] for p in leaves(tab['root'])]
    raise ValueError('no pane ' + ref + ' in ' + ws['workspace'] + hint(ref, names) + '; panes: ' + ', '.join(names)
                     + ' (a session name, a pane id or TAB.N)')


# ---------- pane operations ----------

DIRS = {'right': ('row', 1), 'left': ('row', 0), 'down': ('col', 1), 'up': ('col', 0)}


def split(ws_name, target, where='right', size=0.5, session=None, shell=False, focus=False):
    """New pane beside target, taking `size` of target's space. Returns the new pane id.
    focus: the new pane takes the tab's focus and its tab comes to front (off by default, see tab_new)."""
    if not 0.05 <= size <= 0.95:
        raise ValueError('size is a fraction between 0.05 and 0.95 (e.g. 0.3 = 30%)')
    axis, after = DIRS[where]
    with editing(ws_name) as ws:
        tab, pane = resolve(ws, target) if target else active_pane(ws)
        new = new_id('p')
        leaf, parent, index = locate(tab['root'], pane)
        if parent is not None and parent['dir'] == axis:
            share = parent['children'][index]['size']
            parent['children'][index]['size'] = share * (1 - size)
            parent['children'].insert(index + after, dict(pane=new, size=share * size))
        else:
            old = dict(pane=pane, size=1 - size)
            fresh = dict(pane=new, size=size)
            branch = dict(dir=axis, children=[old, fresh] if after else [fresh, old])
            if parent is None:
                tab['root'] = branch
            else:
                branch['size'] = parent['children'][index]['size']
                parent['children'][index] = branch
        tab['root'] = normalize(tab['root'])
        ws['panes'][new] = dict(session=session, shell=bool(shell or not session))
        tab['zoom'] = None  # a new pane under a zoomed one would be invisible (tmux unzooms too)
        if focus:
            tab['focus'] = new
            ws['active_tab'] = tab['id']
    return new


def active_pane(ws):
    tab = find_tab(ws, ws['active_tab'])[1]
    return tab, tab['focus']


def resize(ws_name, target, width=None, height=None, toward=None):
    """Set target's share of its row (width) or column (height), like tmux resize-pane -x/-y in %.
    toward=next|prev: take the change only from that neighbour (dragging one border moves two panes)."""
    with editing(ws_name) as ws:
        tab, pane = resolve(ws, target)
        for value, axis in ((width, 'row'), (height, 'col')):
            if value is None:
                continue
            if not 0.05 <= value <= 0.95:
                raise ValueError('width/height is a fraction between 0.05 and 0.95')
            trail = path_to(tab['root'], pane)
            hit = next(((node, i) for node, i in reversed(trail) if node['dir'] == axis), None)
            if not hit:
                raise ValueError(target + ' has no ' + ('side-by-side' if axis == 'row' else 'stacked') + ' neighbour to resize against')
            node, i = hit
            if toward:
                j = i + 1 if toward == 'next' else i - 1
                if not 0 <= j < len(node['children']):
                    raise ValueError(target + ' has no ' + toward + ' neighbour in that direction')
                pair = node['children'][i]['size'] + node['children'][j]['size']
                mine = max(.05, min(value, pair - .05))
                node['children'][i]['size'], node['children'][j]['size'] = mine, pair - mine
                continue
            others = sum(c['size'] for j, c in enumerate(node['children']) if j != i) or 1
            for j, c in enumerate(node['children']):
                c['size'] = value if j == i else c['size'] / others * (1 - value)
        tab['root'] = normalize(tab['root'])


def focus(ws_name, target):
    with editing(ws_name) as ws:
        tab, pane = resolve(ws, target)
        tab['focus'] = pane
        ws['active_tab'] = tab['id']


def zoom(ws_name, target):
    with editing(ws_name) as ws:
        tab, pane = resolve(ws, target)
        tab['zoom'] = None if tab.get('zoom') == pane else pane
        tab['focus'] = pane


def swap(ws_name, a, b):
    with editing(ws_name) as ws:
        tab_a, pa = resolve(ws, a)
        tab_b, pb = resolve(ws, b)
        la = locate(tab_a['root'], pa)[0]
        lb = locate(tab_b['root'], pb)[0]
        la['pane'], lb['pane'] = pb, pa


def close(ws_name, target):
    """Remove the pane; its session keeps running in the background (sm stop ends it)."""
    with editing(ws_name) as ws:
        tab, pane = resolve(ws, target)
        if leaves(tab['root']) == [pane]:
            raise ValueError('last pane of tab ' + tab['name'] + '; close the tab instead')
        _, parent, index = locate(tab['root'], pane)
        parent['children'].pop(index)
        tab['root'] = normalize(tab['root'])
        ws['panes'].pop(pane, None)
        if tab['focus'] == pane:
            tab['focus'] = leaves(tab['root'])[0]
        if tab.get('zoom') == pane:
            tab['zoom'] = None
        return ws['panes']


def move(ws_name, target, to_tab, where='right', size=0.5, focus=False):
    """Move a pane (with its session) into another tab beside that tab's focused pane. A tab that does not
    exist yet is made (\"put w1 in workers\" should just work); moving the only pane of a tab takes the tab
    away with it, like tmux join-pane."""
    with editing(ws_name) as ws:
        tab, pane = resolve(ws, target)
        info = ws['panes'][pane]
        try:
            dest = find_tab(ws, to_tab)[1]
        except ValueError:
            dest = None
        if dest is tab:
            raise ValueError(target + ' is already in tab ' + tab['name'])
        if leaves(tab['root']) == [pane]:
            if dest is None:  # the only pane into a tab of its own: that is a rename
                tab['name'] = to_tab
                if focus:
                    ws['active_tab'] = tab['id']
                return pane
            i = ws['tabs'].index(tab)
            ws['tabs'].pop(i)
            if ws['active_tab'] == tab['id']:
                ws['active_tab'] = dest['id']
        else:
            _, parent, index = locate(tab['root'], pane)
            parent['children'].pop(index)
            tab['root'] = normalize(tab['root'])
            if tab['focus'] == pane:
                tab['focus'] = leaves(tab['root'])[0]
            if tab.get('zoom') == pane:
                tab['zoom'] = None
        ws['panes'].pop(pane)
    if dest is None:
        return tab_new(ws_name, to_tab, None, info.get('session'), focus=focus)['focus'] if info.get('session') else \
            _shell_tab(ws_name, to_tab, focus)
    return split(ws_name, dest['focus'], where, size, info.get('session'), info.get('shell'), focus=focus)


def _shell_tab(ws_name, name, focus):
    return tab_new(ws_name, name, focus=focus)['focus']


# ---------- arrange (tmux select-layout) ----------

PRESETS = {
    'columns': 'columns', 'cols': 'columns', 'side-by-side': 'columns', 'horizontal': 'columns', 'even-horizontal': 'columns',
    'rows': 'rows', 'stack': 'rows', 'stacked': 'rows', 'vertical': 'rows', 'even-vertical': 'rows',
    'grid': 'grid', 'tiled': 'grid', 'tile': 'grid',
    'main-left': 'main-left', 'main-vertical': 'main-left',
    'main-top': 'main-top', 'main-horizontal': 'main-top',
}


def arrange(ws_name, tab_ref, preset, main=None, size=0.6):
    """Rebuild a tab's panes into one of tmux's layouts in one step; the panes, their sessions, the focus
    and the order stay. columns/rows: side by side / stacked, equal; grid: rows of equal tiles; main-left /
    main-top: one main pane (`main`, else the first) with `size` of the tab, the rest beside / below it."""
    kind = PRESETS.get(preset)
    if not kind:
        names = sorted(set(PRESETS.values()))
        raise ValueError('no layout ' + preset + hint(preset, list(PRESETS)) + '; layouts: ' + ', '.join(names))
    if not 0.05 <= size <= 0.95:
        raise ValueError('size is a fraction between 0.05 and 0.95 (e.g. 0.6 = 60%)')
    with editing(ws_name) as ws:
        tab = find_tab(ws, tab_ref)[1] if tab_ref else find_tab(ws, ws['active_tab'])[1]
        order = leaves(tab['root'])
        if main:
            main_tab, main_pane = resolve(ws, main)
            if main_tab is not tab:
                raise ValueError(main + ' is in tab ' + main_tab['name'] + ', not ' + tab['name'])
            order.remove(main_pane)
            order.insert(0, main_pane)

        def even(axis, panes):
            if len(panes) == 1:
                return dict(pane=panes[0])
            return dict(dir=axis, children=[dict(pane=p, size=1 / len(panes)) for p in panes])

        if len(order) == 1:
            root = dict(pane=order[0])
        elif kind == 'columns':
            root = even('row', order)
        elif kind == 'rows':
            root = even('col', order)
        elif kind == 'grid':
            cols = math.ceil(math.sqrt(len(order)))
            lines = [order[i:i + cols] for i in range(0, len(order), cols)]
            root = dict(dir='col', children=[dict(even('row', line), size=1 / len(lines)) for line in lines]) \
                if len(lines) > 1 else even('row', lines[0])
        else:
            axis, rest_axis = ('row', 'col') if kind == 'main-left' else ('col', 'row')
            rest = even(rest_axis, order[1:])
            root = dict(dir=axis, children=[dict(pane=order[0], size=size), dict(rest, size=1 - size)])
        tab['root'] = normalize(root)
        tab['zoom'] = None
        return tab['name']


# ---------- describing a workspace in one line ----------

def describe_pane(ws, tab, pane):
    info = ws['panes'].get(pane, {})
    if info.get('session'):
        return info['session']
    return tab['name'] + '.' + str(leaves(tab['root']).index(pane)) + '(shell)'


def shape(ws):
    """`main*: [flat-main* | (flat-qa / flat-rev)] · workers: [w1 | w2]` — * marks the tab the user's screen shows
    and each tab's focused pane; | is side by side, / is stacked."""
    def node_text(tab, node, top=False):
        if 'pane' in node:
            text = describe_pane(ws, tab, node['pane'])
            return text + ('*' if node['pane'] == tab.get('focus') and len(leaves(tab['root'])) > 1 else '')
        inner = (' | ' if node['dir'] == 'row' else ' / ').join(node_text(tab, c) for c in node['children'])
        return inner if top else '(' + inner + ')'
    parts = []
    for tab in ws['tabs']:
        text = tab['name'] + ('*' if tab['id'] == ws.get('active_tab') else '') + ': [' + node_text(tab, tab['root'], True) + ']'
        if tab.get('zoom'):
            text += ' zoomed ' + describe_pane(ws, tab, tab['zoom'])
        parts.append(text)
    return ws['workspace'] + '@' + ws.get('host', '?') + '  ' + ' · '.join(parts)


def bind(ws_name, target, session):
    """Show an existing session in this pane (the pane's previous content is let go)."""
    with editing(ws_name) as ws:
        tab, pane = resolve(ws, target)
        for other_tab in ws['tabs']:
            for other in leaves(other_tab['root']):
                if other != pane and ws['panes'].get(other, {}).get('session') == session:
                    raise ValueError(session + ' is already shown in pane ' + other)
        ws['panes'][pane] = dict(session=session, shell=False)
        return pane


def watch(name, interval=.2, heartbeat=20):
    """Yield the layout each time its version changes (first one immediately), and every `heartbeat`
    seconds a {"heartbeat": t, "version": v} line, so a reader can tell a quiet stream from a stalled
    ssh. For remote apps: one ssh running `sm watch --ws WS --json` instead of polling."""
    seen, beat = None, time.monotonic()
    p = ws_path(name)
    while True:
        ws = sm.read(p)
        if ws and ws.get('version') != seen:
            seen = ws.get('version')
            beat = time.monotonic()
            yield ws
        elif heartbeat and time.monotonic() - beat >= heartbeat:
            beat = time.monotonic()
            yield dict(heartbeat=time.time(), version=seen, workspace=name)
        time.sleep(interval)
