"""sm session layer — what tmux did for agents, without tmux.

sm owns where a session lives and whether it is alive: spawn, attach, resume, stop, list.
peer owns who it is and talking to it. A session is its name: the name maps to one engine
session (Claude/Codex session id) on one host, held by one peer-hold process. Every start goes
through claude/codex, or the user's ccv/ccx wrappers when installed (via peer-session), and a session that died is brought back by resuming that
engine session, so its history continues.

Runs on every machine (Mac, Arch, NUC). The Mac app's `sm` CLI forwards session verbs here.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import peer_core  # noqa: E402  (names and engine session rows live in peer's registry)


def state_dir():
    return Path(os.environ.get('SM_STATE', str(Path.home() / '.smux/state/sm')))


def entry_path(name):
    return state_dir() / 'sessions' / (name + '.json')


def read(p):
    try:
        return json.loads(Path(p).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write(p, value):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp' + secrets.token_hex(3))
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1))
    os.replace(tmp, p)


@contextlib.contextmanager
def name_lock(name):
    """One spawn/resume per name at a time: the guard against starting the same agent twice."""
    p = state_dir() / 'locks' / (name + '.lock')
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'w') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def launcher(kind, args):
    test = os.environ.get('SM_TEST_LAUNCHER')  # tests: a stand-in agent, also for viewers in child processes
    if test:
        return [test, kind] + list(args)
    if kind == 'shell':
        # A shell pane (dev server, logs): held like an agent so it survives the app, no engine session.
        return [os.environ.get('SHELL') or '/bin/sh', '-l'] + list(args)
    return [peer_core.python(), str(BIN / 'peer-session'), kind] + list(args)


def holder_alive(entry):
    sock = entry.get('hold')
    if not sock:
        return False
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(1)
        try:
            s.connect(sock)
            return True
        except OSError:
            return False


def in_tmux(entry):
    """Adopted from tmux but not migrated yet: the agent still runs in that tmux pane."""
    pane = entry.get('tmux_pane')
    if not pane or not entry.get('adopted') or entry.get('migrated'):
        return False
    out = subprocess.run(['tmux', 'display', '-p', '-t', pane, '#{pane_id}'], capture_output=True, text=True)
    return out.returncode == 0 and out.stdout.strip() == pane


def status(entry):
    """running · tmux (adopted, still running in its tmux pane) · ended (stopped or exited by itself) ·
    dead (holder gone without an exit: reboot, kill). Only dead is resumed by restore."""
    if holder_alive(entry):
        return 'running'
    if in_tmux(entry):
        return 'tmux'  # never resume these: the same engine session is alive in tmux
    if Path(entry['hold']).with_suffix('.exit').exists():
        return 'ended'
    return 'dead'


def learn_session(entry):
    """Fill in the engine session id once the agent's hook has registered it with peer."""
    if entry.get('session') or entry.get('kind') == 'shell':
        return entry
    for row in peer_core.sessions():
        if row.get('hold') == entry['hold'] and row.get('session'):
            entry['session'] = row['session']
            write(entry_path(entry['name']), entry)
            break
    return entry


def peer_row(entry):
    """This session's peer row. A resume reuses the holder socket, so an older conversation's row can share it: prefer
    the row of this conversation, then the most recently observed. Taking the first match read a days-old row, and the
    live harness-claude showed as gone and its pane stayed "waking" (11/08 sm feedback)."""
    sid = entry.get('session')
    rows = [row for row in peer_core.sessions()
            if (sid and row.get('session') == sid) or row.get('hold') == entry['hold']]
    if not rows:
        return None
    return peer_core.public(max(rows, key=lambda row: (bool(sid) and row.get('session') == sid, row.get('observed') or 0)))


def scope_prefix(sock):
    # Arch: own systemd scope per session so the per-tab memory cap (peer-hold-.scope.d) applies.
    # A new unit name per start: when a holder dies, programs its agent started (a dev server, a fake API)
    # can keep the old scope alive, and reusing its name then fails ("already loaded"), so the session could
    # never come back (11/01: flat-main, flat-qa, flat-rev). Those programs are left alone; they may be in use.
    if sys.platform.startswith('linux') and shutil.which('systemd-run') and os.environ.get('XDG_RUNTIME_DIR'):
        unit = 'peer-hold-%s-%d' % (Path(sock).stem, time.time_ns() // 1000)
        return ['systemd-run', '--user', '--scope', '--quiet', '--collect', '--unit', unit]
    return []


# What a session must not inherit from whoever ran `sm spawn`: when an agent spawns an agent, Claude's
# child markers (CLAUDE_CODE_CHILD_SESSION turns transcript saving off), its messaging socket/token and
# session id, Codex's sandbox markers, and peer/sm identity (given back explicitly via --env).
# Only the markers an engine puts on its own children; settings people export (CLAUDE_CODE_EXECUTABLE,
# CLAUDE_CODE_TMPDIR, CODEX_HOME) pass through.
INHERITED = re.compile(r'^(CLAUDECODE|CLAUDE_CODE_(CHILD_SESSION|ENTRYPOINT|EXECPATH|MESSAGING_SOCKET|MESSAGING_TOKEN|'
                       r'SESSION_ATTENDED|SESSION_ID|SSE_PORT)|CLAUDE_PID|CLAUDE_EFFORT|'
                       r'CODEX_(THREAD_ID|SANDBOX|SANDBOX_NETWORK_DISABLED|MANAGED_BY_NPM|CI)|'
                       r'PEER_.*|SM_PANE_ID|SM_WORKSPACE|TMUX|TMUX_PANE|'
                       # a front end's own launch settings: Fine's first prompt/model for the tab that started this,
                       # the terminal's identity (Ghostty) and the app bundle macOS would attribute permissions to
                       r'FINE_(?!HOME$).*|GHOSTTY_.*|__CFBundleIdentifier|TERM_PROGRAM.*)$')


def clean_env():
    env = {k: v for k, v in os.environ.items() if not INHERITED.match(k)}
    # Every screen that shows a session is a Ghostty surface (sm and Fine both embed libghostty), whatever
    # terminal ran `sm spawn`; TERM stays xterm-256color, which every machine has terminfo for.
    env['TERM_PROGRAM'] = 'ghostty'
    env['TERM'] = 'xterm-256color'  # not the caller's (xterm-ghostty has no terminfo on Arch)
    if not env.get('LANG'):
        env['LANG'] = system_lang()  # an ssh command (spawn --host arch) arrives without one: Korean breaks
    return env


def system_lang():
    try:
        for line in open('/etc/locale.conf'):
            if line.startswith('LANG='):
                return line.split('=', 1)[1].strip().strip('"')
    except OSError:
        pass
    return 'en_US.UTF-8'


def trust_folder(cwd, config=None):
    """Record Claude Code's folder trust for cwd (and its git root) before an unattended start. Nobody sits at a
    spawned agent's screen, so the "Is this a project you trust?" screen stopped it for good, with "No, exit" under
    the cursor (11/02 inche-opus: the folder had just got `git init`, and Claude looks for a trusted parent only up to
    the git root). -y already skips every permission prompt; trusting the folder is the same decision.
    Takes Claude's own lock (~/.claude.json.lock, a directory) so it never loses a write of a running Claude; if the
    lock stays taken, it gives up and the agent may show the screen as before."""
    config = Path(config or Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home()) / '.claude.json')
    roots = {os.path.realpath(cwd)}
    try:
        top = subprocess.run(['git', '-C', cwd, 'rev-parse', '--show-toplevel'], capture_output=True, text=True, timeout=5)
        if top.returncode == 0 and top.stdout.strip():
            roots.add(os.path.realpath(top.stdout.strip()))
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        data = json.loads(config.read_text())
    except (OSError, ValueError):
        return False
    projects = data.get('projects') or {}
    if all((projects.get(r) or {}).get('hasTrustDialogAccepted') for r in roots):
        return True
    lock = Path(str(config) + '.lock')
    deadline = time.time() + 3
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.time() > deadline:
                return False
            time.sleep(.05)
        except OSError:
            return False
    try:
        data = json.loads(config.read_text())  # again, under the lock
        projects = data.setdefault('projects', {})
        for root in roots:
            projects[root] = dict(projects.get(root) or {}, hasTrustDialogAccepted=True)
        temp = config.with_name(config.name + '.sm-' + secrets.token_hex(4))
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.chmod(temp, config.stat().st_mode & 0o777)
        os.replace(temp, config)
        return True
    except (OSError, ValueError):
        return False
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def start_holder(entry, command):
    find_launchers()
    env_vars = clean_env()
    env_vars['PATH'] = merged_path(env_vars.get('PATH', ''))
    sock = Path(entry['hold'])
    sock.parent.mkdir(parents=True, exist_ok=True)
    if entry.get('kind') == 'claude' and not os.environ.get('SM_TEST_LAUNCHER') \
            and any(a in ('-y', '-ry', '-yr', '--dangerously-skip-permissions') for a in command):
        trust_folder(entry['cwd'])
    for stale in (sock.with_suffix('.exit'),):
        stale.unlink(missing_ok=True)
    env = list(entry.get('env', []))
    result = subprocess.run(scope_prefix(sock) + [str(BIN / 'peer-hold'), 'start', '--sock', str(sock), '--cwd', entry['cwd']]
                            + [a for e in env for a in ('--env', e)] + ['--'] + command,
                            capture_output=True, text=True, timeout=15, env=env_vars)
    if result.returncode:
        raise ValueError(result.stderr.strip() or 'peer-hold did not start')
    entry['started'] = time.time()
    write(entry_path(entry['name']), entry)
    return json.loads(result.stdout)


def startup_failure(name):
    """Say why a session died starting. `sm tail` cannot: it reads the holder, which is gone by then."""
    text = re.sub(r'\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\\\)|[()][A-Z0-9]|.)', '',
                  last_screen(name).decode(errors='replace'))
    lines = [line.strip() for line in text.splitlines() if line.strip() and line.strip() != '>']
    return name + ' exited during startup' + (': ' + lines[-1][:300] if lines else '') + '; last screen: sm screen ' + name


def wait_registered(entry, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        learn_session(entry)
        row = peer_row(entry)
        if row and row['state'] in ('idle', 'working', 'blocked', 'error'):
            return row
        if status(entry) == 'ended':
            raise ValueError(startup_failure(entry['name']))
        time.sleep(.2)
    return None


def result(entry, action, row=None):
    return dict(action=action, name=entry['name'], host=entry['host'], kind=entry['kind'], cwd=entry['cwd'],
                session=entry.get('session'), hold=entry['hold'], status=status(entry),
                visible=entry.get('visible', False), pane=entry.get('pane'), parent=entry.get('parent'),
                peer=row)


def release_peer_name(name):
    """For --fresh: let a new engine session take a name whose old session has ended."""
    names_file = peer_core.root() / 'names.json'
    with peer_core.lock(names_file, .02):
        names = peer_core.read(names_file)
        owner = names.get(name)
        if owner:
            row = peer_core.session(owner)
            if row and peer_core.public(row)['state'] != 'gone':
                raise ValueError(name + ' belongs to a live session; stop it first')
            del names[name]
            peer_core.write(names_file, names)


def unattended(kind, args, ask=False):
    """An agent started by sm runs with nobody at its screen, so a permission or approval prompt would stop it for
    good. -y skips them (a wrapper's shorthand; peer-session spells it --dangerously-skip-permissions for the stock
    Claude and --dangerously-bypass-approvals-and-sandbox for Codex), the same mode a resume (-ry) already uses.
    --ask keeps the prompts."""
    args = list(args)
    if kind == 'omo':
        # OmO asks through its permission preset; full-access is its -y. A caller's own preset or rule stays.
        if ask or any(a.startswith('--permission') for a in args):
            return args
        return ['--permission-preset', 'full-access'] + args
    # -ry already skips the prompts, and Codex refuses -y with it on a remote resume ("Permission overrides are not
    # supported"): `sm split --spawn be -- -ry ID` ended at once (11/07 sm feedback).
    if ask or kind not in ('claude', 'codex') or any(a in ('-y', '-ry', '-yr', '--dangerously-skip-permissions') for a in args):
        return args
    return ['-y'] + args


# --worker: a lead orchestrating a team spawns its workers with the lead-only features of their harness turned off
# (Claude: the advisor from advisorModel); judgment stays with the lead, and a worker's result is checked by the lead
# and by a reviewer from another engine. Opt-in: without it a spawned agent runs as the user's settings say. A harness
# with a lead-only feature of its own adds its line here.
ADVISOR_OFF = 'CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1'
WORKER_ENV = {'claude': (ADVISOR_OFF,)}


def spawn(name, kind, cwd, pane=None, visible=False, env=(), args=(), parent=None, fresh=False, timeout=30, ask=False,
          worker=False):
    peer_core.valid_name(name)
    args = unattended(kind, args, ask)
    if kind not in ('claude', 'codex', 'omo', 'shell'):
        raise ValueError('kind is claude, codex, omo or shell')
    with name_lock(name):
        entry = read(entry_path(name))
        if entry and not fresh:
            if entry['host'] != peer_core.host():
                raise ValueError(name + ' lives on ' + entry['host'])
            if status(entry) in ('running', 'tmux'):
                return result(entry, 'already-running', peer_row(entry))
            return _resume(entry, timeout, args)
        if entry and fresh:
            if status(entry) == 'running':
                raise ValueError(name + ' is running; stop it before --fresh')
            release_peer_name(name)
        owner = peer_core.read(peer_core.root() / 'names.json').get(name)
        if owner and not fresh:
            row = peer_core.session(owner)
            if row and peer_core.public(row)['state'] != 'gone':
                raise ValueError(name + ' is already the name of a live session (' + str(row.get('target')) + ')')
        cwd = str(Path(cwd).expanduser().resolve())
        if not Path(cwd).is_dir():
            raise ValueError('working directory does not exist: ' + cwd)
        for item in env:
            if '=' not in item or item.split('=', 1)[0] in ('PEER_HOLD_SOCK', 'PEER_NAME'):
                raise ValueError('--env takes K=V and cannot set ' + item.split('=', 1)[0])
        hold = str(state_dir() / 'hold' / (secrets.token_hex(4) + '.sock'))  # short: macOS socket paths ≤ 104 bytes
        full_env = ['PEER_NAME=' + name, 'PEER_STATE=' + str(peer_core.root()), 'PEER_CONFIG=' + str(peer_core.config())]
        if parent:
            full_env.append('PEER_PARENT=' + parent)
        if worker:
            full_env += WORKER_ENV.get(kind, ())
        if pane:
            full_env.append('SM_PANE_ID=' + pane)
        if pane or visible:
            full_env.append('PEER_VISIBLE=1')
        full_env += list(env)
        entry = dict(name=name, kind=kind, host=peer_core.host(), cwd=cwd, hold=hold, session=None, args=list(args),
                     env=full_env, pane=pane, visible=bool(pane or visible), parent=parent, created=time.time())
        write(entry_path(name), entry)
        start_holder(entry, launcher(kind, args))
        return result(entry, 'spawned', None if kind == 'shell' else wait_registered(entry, timeout))


RESUME_FLAGS = ('-r', '-ry', '-yr', '--resume')


def resume_args(args):
    """What a resume passes after `-ry ID`: the caller's engine options (a model, an effort). -y is dropped: -ry
    already skips the prompts. Silently dropping these sent an agent to --fresh to change a model, and the
    conversation it meant to keep was cut off (11/01)."""
    return [a for a in args if a not in ('-y', '--dangerously-skip-permissions')]


def _resume(entry, timeout=30, args=()):
    entry.pop('slept', None)  # awake again (start_holder writes the entry)
    learn_session(entry)
    if status(entry) == 'running':
        return result(entry, 'already-running', peer_row(entry))
    if status(entry) == 'tmux':
        return result(entry, 'in-tmux', peer_row(entry))
    if entry['kind'] == 'shell':
        start_holder(entry, launcher('shell', entry.get('args', [])))  # a shell has no history to resume: start it again
        return result(entry, 'resumed')
    if any(a in RESUME_FLAGS for a in args):  # the caller named the conversation: `sm spawn NAME -- -ry ID` on an ended name
        command, action = launcher(entry['kind'], resume_args(args)), 'resumed'
    elif resumable(entry) and entry['kind'] == 'omo':
        # --session-id continues that session (and would create it, never fail, if its file were gone).
        command, action = launcher('omo', unattended('omo', ['--session-id', entry['session']] + resume_args(args))), 'resumed'
    elif resumable(entry):
        command, action = launcher(entry['kind'], ['-ry', entry['session']] + resume_args(args)), 'resumed'
    else:
        # Nothing to resume (never registered, or never spoken to): start as before. Resuming an id with no
        # conversation only ends in "No conversation found", and the session could not come back at all.
        original = entry.get('args', [])
        command = launcher(entry['kind'], original + resume_args(args))
        # A Fine tab reopened on an old conversation was spawned with `-ry ID`: that start does resume (11/07 sm feedback).
        action = 'resumed' if any(a in RESUME_FLAGS for a in original) else 'started fresh, no conversation to resume'
        entry.pop('session', None)  # that id led nowhere; learn the new one, or the next resume starts over again
    start_holder(entry, command)
    return result(entry, action, wait_registered(entry, timeout))


def load(name):
    entry = read(entry_path(name))
    if not entry:
        raise ValueError('no session named ' + name + ' on ' + peer_core.host() + '; sm ls')
    return entry


def resume(name, timeout=30, args=()):
    with name_lock(name):
        return _resume(load(name), timeout, args)


def stop(name, timeout=10):
    entry = load(name)
    if status(entry) != 'running':
        return result(entry, 'not-running')
    _end(name, timeout)
    entry = load(name)
    entry.pop('slept', None)  # stopped on purpose: stays ended, a message does not wake it
    write(entry_path(name), entry)
    return result(entry, 'stopped')


CLAUDE_HEALTH = 12  # seconds


def _end(name, timeout=10):
    entry = load(name)
    meta = read(Path(entry['hold']).with_suffix('.json'))
    if status(entry) != 'running' or not meta:
        return
    # Claude Code counts a fullscreen session that dies within ~10 s of starting as a strike, and two strikes turn
    # fullscreen off for the whole machine (~/.claude.json fullscreenAutoDisabled; 11/01 a spawn→stop test did it).
    young = CLAUDE_HEALTH - (time.time() - (entry.get('started') or 0))
    if entry.get('kind') == 'claude' and young > 0:
        time.sleep(young)
    def send(pid, sig):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass  # already gone: the child often exits before its holder finishes winding down (11/05)

    def wait(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end and status(entry) == 'running':
            time.sleep(.1)

    send(meta['child'], signal.SIGTERM)
    wait(timeout)
    if status(entry) == 'running':
        send(meta['child'], signal.SIGKILL)
        wait(5)
    if status(entry) == 'running' and meta.get('holder'):
        send(meta['holder'], signal.SIGTERM)  # the child is gone but its holder lingers
        wait(5)


# Sleep: past the prompt cache's life (Claude Code keeps it 1 hour) an idle session costs the same to
# resume as to keep, so it gives its memory back. +5 min lets Claude's idle compaction run first.
# Waking is attach (opening the pane), a peer message, or the notch (sm reveal → attach).
SLEEP_AFTER = 65 * 60
SHELLS = {'bash', 'zsh', 'sh', 'fish', 'dash'}


def background_work(entry):
    """True when something still runs under the agent besides its helpers: a background shell job, a dev
    server. Ending the agent would end that too."""
    meta = read(Path(entry['hold']).with_suffix('.json'))
    if not meta:
        return False
    out = subprocess.run(['ps', '-eo', 'pid=,ppid=,comm='], capture_output=True, text=True).stdout
    kids = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            kids.setdefault(int(parts[1]), []).append((int(parts[0]), os.path.basename(parts[2].strip())))
    agent_seen = False
    stack = [meta['child']]
    while stack:
        pid = stack.pop()
        for kid, name in kids.get(pid, []):
            if name in ('claude', 'codex') or re.fullmatch(r'\d+\.\d+\.\d+', name):
                agent_seen = True
            elif agent_seen and name in SHELLS:
                return True
            stack.append(kid)
    return False


def workspace_of(entry):
    """The workspace a session belongs to: named at spawn (SM_WORKSPACE), shown in its layout, or holding its
    folder (deepest). None for a background helper outside every workspace."""
    import sm_layout
    named = next((e.split('=', 1)[1] for e in entry.get('env', []) if e.startswith('SM_WORKSPACE=')), None)
    spaces = sm_layout.ws_list()
    if named and any(w['workspace'] == named for w in spaces):
        return named
    wanted = entry['name'].lower()
    for ws in spaces:
        if any((p.get('session') or '').lower() == wanted for p in ws.get('panes', {}).values()):
            return ws['workspace']
    if any(e.startswith('FINE_TAB_ID=') for e in entry.get('env', [])):
        return None  # a Fine tab is its own place; sharing a folder with a workspace doesn't make it part of it
    cwd = entry.get('cwd', '')
    best = None
    for ws in spaces:
        root = ws.get('cwd', '')
        if root and (cwd == root or cwd.startswith(root.rstrip('/') + '/')) and (not best or len(root) > len(best['cwd'])):
            best = ws
    return best and best['workspace']


def resumable(entry):
    """Sleeping must not cost the conversation: there has to be a session id, and a conversation behind it. Claude keeps
    no transcript until its first message ("No conversation found", 11/07); Codex refuses to resume a thread without
    turns (10/31)."""
    sid = entry.get('session')
    if not sid:
        return False
    if entry.get('kind') == 'claude':
        if os.environ.get('SM_TEST_LAUNCHER'):
            return True  # a stand-in agent writes no transcript
        home = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))
        return any((home / 'projects').glob('*/' + sid + '.jsonl'))
    if entry.get('kind') != 'codex':
        return True
    home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    for f in (home / 'sessions').glob('*/*/*/rollout-*' + sid + '.jsonl'):
        try:
            with open(f, errors='replace') as stream:
                if any('"response_item"' in line for line in stream):
                    return True
        except OSError:
            pass
    return False


def holder_status(entry):
    out = subprocess.run([str(BIN / 'peer-hold'), 'status', '--sock', entry['hold']], capture_output=True, text=True, timeout=5)
    return json.loads(out.stdout or '{}') if out.returncode == 0 else {}


def looked_at(entry):
    """Someone's screen shows it: an sm or Fine pane (passive leftovers such as tmux viewers don't count)."""
    st = holder_status(entry)
    return st.get('active', st.get('viewers', 0)) > 0  # holders older than 11/01 only count all viewers


def idle_since(entry, row):
    """When it last did or showed anything: the end of its last turn (the engine's own idle state), but no earlier
    than its last start (a resumed session keeps its old idle time) or the last moment a screen showed it."""
    return max(row.get('since') or 0, entry.get('started') or 0, holder_status(entry).get('seen') or 0)


def awake_prefixes():
    """Names never put to sleep, from ~/.smux/sm.json {"awake_prefixes": ["lane-"]}. For sessions reached outside
    peer (a supervisor that types into them by terminal), which a sleep would cut off; `sm awake NAME` does one."""
    try:
        return [p for p in json.loads((Path.home() / '.smux' / 'sm.json').read_text()).get('awake_prefixes', []) if p]
    except (OSError, ValueError, AttributeError):
        return []


def sleep(after=SLEEP_AFTER, dry_run=False):
    """Put whole workspaces to sleep once every agent in them has been idle past the prompt cache and nobody
    is looking at any of them; a busy or watched member keeps its workspace awake: the unit
    that sleeps is the workspace. Background helpers outside any workspace are judged alone."""
    done = []
    now = time.time()
    groups = {}
    for entry in entries():
        if entry.get('host') != peer_core.host() or entry.get('kind') == 'shell' or status(entry) != 'running':
            continue
        groups.setdefault(workspace_of(entry) or 'solo:' + entry['name'], []).append(entry)
    for group, members in sorted(groups.items()):
        reason = None
        for entry in members:
            name = entry['name']
            row = peer_row(entry)
            if entry.get('awake'):
                reason = name + ' is kept awake'
            elif any(name.startswith(p) for p in awake_prefixes()):
                reason = name + ' matches awake_prefixes'  # e.g. lanes another program addresses by terminal, not by peer
            elif not row or row['state'] != 'idle':
                reason = name + ' is ' + str((row or {}).get('state'))
            elif now - idle_since(entry, row) < after:
                reason = name + ' was active %d min ago' % ((now - idle_since(entry, row)) / 60)
            elif not resumable(entry):
                reason = name + ' has no conversation to resume'
            elif looked_at(entry):
                reason = name + ' is on screen'
            elif background_work(entry):
                reason = name + ' has background work'
            if reason:
                break
        if reason:
            if not group.startswith('solo:') or not reason.endswith('min ago'):
                done.append(dict(workspace=group, action='awake', reason=reason))
            continue
        for entry in members:
            name = entry['name']
            idle = int((now - idle_since(entry, peer_row(entry) or {})) / 60)
            if dry_run:
                done.append(dict(workspace=group, name=name, action='would-sleep', idle_minutes=idle))
                continue
            entry['slept'] = time.time()  # mark first: a viewer that sees the holder end must already know why
            write(entry_path(name), entry)
            _end(name)
            done.append(dict(workspace=group, name=name, action='slept', idle_minutes=idle))
    return dict(sleep=done)


# ---------- new versions: engines and harness ----------
#
# A new engine or harness version should reach every session without anyone restarting it, once it is idle. A session
# keeps running the claude/codex binary and the harness code (peer-hold, peer-session, …) it started with; an update
# reaches it only when it restarts. So the 10-minute job (`sm sleep`) also (1) keeps Codex at the latest release (Claude
# updates itself) and (2) restarts each session whose engine or harness files changed after it started, once it is
# idle — same name, same conversation (-ry), the way a sleep and a wake do it.

UPGRADE_IDLE = 5 * 60              # idle this long and nobody looking: restart now
UPGRADE_IDLE_ON_SCREEN = 30 * 60   # on the user's screen they may be typing a reply; wait longer (a restart loses the draft)
ENGINE_CHECK_EVERY = 3600
HARNESS = ('peer-hold', 'peer-session', 'peer_codex.py', 'peer_core.py', 'peer_transport.py')
_tools = None


_path_checked = False


def find_launchers():
    """launchd's `zsh -lc` (sm sleep, restore) skips .zshrc, which is where claude and codex (or wrappers named ccv
    and ccx) often get onto PATH. A session restarted from there died at once ("No such file or directory"): 28
    automatic upgrades had ended idle agents that way by 11/07. So borrow the interactive shell's PATH when this
    process cannot see the engines."""
    global _path_checked
    if _path_checked or os.environ.get('SM_TEST_LAUNCHER') or (shutil.which('ccv') and shutil.which('ccx')) or \
            (shutil.which('claude') and shutil.which('codex')):
        return
    _path_checked = True
    try:
        out = subprocess.run([os.environ.get('SHELL') or '/bin/sh', '-ic', 'printf "\\nSM_PATH=%s\\n" "$PATH"'],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return
    found = re.search(r'^SM_PATH=(.+)$', out, re.M)
    if found:
        os.environ['PATH'] = found.group(1)


_user_path = None


def user_path():
    """The PATH the user's own terminal has (an interactive shell reads .zshrc, where ~/bin and the like are added).
    Read once per process; empty if the shell does not answer."""
    global _user_path
    if _user_path is None:
        _user_path = ''
        if not os.environ.get('SM_TEST_LAUNCHER'):
            try:
                out = subprocess.run([os.environ.get('SHELL') or '/bin/sh', '-ic', 'printf "\\nSM_PATH=%s\\n" "$PATH"'],
                                     stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20).stdout
                found = re.search(r'^SM_PATH=(.+)$', out, re.M)
                _user_path = found.group(1) if found else ''
            except (OSError, subprocess.SubprocessError):
                pass
    return _user_path


def merged_path(current):
    """An agent sees the commands the user's terminal sees, wherever `sm spawn` ran: the user's interactive PATH
    first, then whatever of the caller's PATH it lacks. The caller's PATH alone was the spawner's — an agent started
    from an app or another agent's shell lacked ~/bin, so home and seol-notch were not found (11/16 sm feedback)."""
    seen, parts = set(), []
    for part in (user_path() + ':' + current).split(':'):
        if part and part not in seen:
            seen.add(part)
            parts.append(part)
    return ':'.join(parts)


def tools():
    """Where claude, codex, ccv, ccx resolve for this user (a login shell's PATH, as the launcher sees it)."""
    global _tools
    if _tools is None:
        _tools = {}
        find_launchers()
        script = 'for t in claude codex ccv ccx; do printf "%s=%s\\n" "$t" "$(command -v $t)"; done'
        try:
            out = subprocess.run([os.environ.get('SHELL') or '/bin/sh', '-lc', script], capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            out = ''
        for line in out.splitlines():
            name, _, path = line.partition('=')
            if path:
                if '/mise/shims/' in path:  # a shim never changes; ask mise where it points now
                    try:
                        real = subprocess.run(['mise', 'which', name], capture_output=True, text=True, timeout=20).stdout.strip()
                        path = real or path
                    except (OSError, subprocess.SubprocessError):
                        pass
                _tools[name] = os.path.realpath(path)
    return _tools


def mtime(path):
    try:
        return os.stat(path).st_mtime
    except (OSError, TypeError):
        return 0


def outdated(entry):
    """Why NAME runs an older engine or harness than is installed now, or None."""
    started = entry.get('started') or entry.get('created') or 0
    kind = entry.get('kind')
    engine = tools().get(kind)
    if engine and mtime(engine) > started:
        return kind + ' updated (' + os.path.basename(os.path.dirname(engine)) + '/' + os.path.basename(engine) + ')'
    wrapper = tools().get('ccv' if kind == 'claude' else 'ccx')
    if wrapper and mtime(wrapper) > started:
        return os.path.basename(wrapper) + ' changed'
    changed = [f for f in HARNESS if mtime(BIN / f) > started]
    return 'harness changed (' + ', '.join(changed) + ')' if changed else None


def quiet_since(entry, row):
    """When the agent last did anything: the end of its last turn, or its last start. Unlike idle_since it ignores
    being looked at — a pane on the user's screen is "seen" every moment, so it never looked idle and never moved to a
    new version (11/05); being on screen only makes the wait longer."""
    return max(row.get('since') or 0, entry.get('started') or 0)


def update_engines(force=False):
    """Codex at the latest release (Claude installs its own updates). At most once an hour; never fails the job."""
    mark = state_dir() / 'engines.json'
    seen = read(mark)
    if not force and time.time() - seen.get('checked', 0) < ENGINE_CHECK_EVERY:
        return seen.get('result')
    result = {}
    try:
        latest = subprocess.run(['npm', 'view', '@openai/codex', 'version'], capture_output=True, text=True, timeout=60).stdout.strip()
        have = subprocess.run([os.environ.get('SHELL') or '/bin/sh', '-lc', 'codex --version'], capture_output=True, text=True,
                              timeout=30).stdout.strip().split()[-1:]
        have = have[0] if have else ''
        result = dict(codex=have, latest=latest)
        if latest and have and latest != have and re.fullmatch(r'[0-9.]+', latest):
            path = tools().get('codex') or ''
            if '/mise/' in path:
                cmd = ['mise', 'use', '-g', 'codex@' + latest]
            else:
                cmd = ['npm', 'install', '-g', '@openai/codex@' + latest]
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            result.update(installed=latest if done.returncode == 0 else None,
                          error=None if done.returncode == 0 else (done.stderr or done.stdout)[-300:])
            global _tools
            _tools = None
    except (OSError, subprocess.SubprocessError) as error:
        result = dict(error=str(error)[-300:])
    write(mark, dict(checked=time.time(), result=result))
    return result


UPGRADE_SETTLE = 2  # seconds


def upgrade(dry_run=False, engines=True):
    """Restart idle sessions that run an older engine or harness than is installed (same conversation)."""
    done = []
    engine = update_engines() if engines and not dry_run else None
    now = time.time()
    for entry in entries():
        if entry.get('host') != peer_core.host() or entry.get('kind') not in ('claude', 'codex') or status(entry) != 'running':
            continue
        why = outdated(entry)
        if not why:
            continue
        name = entry['name']
        row = peer_row(entry)
        wait = UPGRADE_IDLE_ON_SCREEN if looked_at(entry) else UPGRADE_IDLE
        hold = None
        if not row:
            hold = 'peer does not list it (its state is unknown)'
        elif row['state'] != 'idle':
            hold = 'it is ' + str(row['state'])
        elif now - quiet_since(entry, row) < wait:
            hold = 'idle only %d min' % ((now - quiet_since(entry, row)) / 60)
        elif background_work(entry):
            hold = 'it has background work'
        elif not resumable(entry):
            hold = 'no conversation to resume yet'
        if hold or dry_run:
            done.append(dict(name=name, action='pending' if hold else 'would-upgrade', why=why, **({'hold': hold} if hold else {})))
            continue
        entry['slept'] = time.time()  # viewers see a sleep (they reattach on their own), not a deliberate stop
        write(entry_path(name), entry)
        try:
            _end(name)
            resume(name, timeout=30)
            time.sleep(UPGRADE_SETTLE)  # the old peer row can pass for registered while the new start is already dying
            if status(load(name)) != 'running':
                raise ValueError(startup_failure(name))
            done.append(dict(name=name, action='upgraded', why=why))
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            # Asleep, not stopped: resume cleared the mark, and without it nothing but a look would bring this one back.
            failed = load(name)
            failed['slept'] = time.time()
            write(entry_path(name), failed)
            done.append(dict(name=name, action='failed', why=why, error=str(error)))
    return dict(upgrade=done, engines=engine)


def wake(name):
    """Resume a sleeping session; a no-op for running ones. Used by peer when a message arrives."""
    entry = load(name)
    if not entry.get('slept') or status(entry) == 'running':
        return result(entry, 'awake')
    return resume(name, timeout=30)


def entries():
    folder = state_dir() / 'sessions'
    return [read(p) for p in sorted(folder.glob('*.json'))] if folder.exists() else []


def ls():
    rows = []
    for entry in entries():
        learn_session(entry)
        row = peer_row(entry)
        rows.append(dict(result(entry, 'list', row), state=(row or {}).get('state')))
    return dict(sessions=rows, host=peer_core.host())


def restore():
    """At login/boot: resume every session whose holder died without exiting. Ended ones stay ended."""
    done = []
    for entry in entries():
        if entry.get('host') == peer_core.host() and status(entry) == 'dead':
            try:
                done.append(resume(entry['name'], timeout=5))
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                done.append(dict(action='failed', name=entry['name'], error=str(error)))
    return dict(restored=done)


KEYS = {'Enter': b'\r', 'Tab': b'\t', 'Esc': b'\x1b', 'Space': b' ', 'BSpace': b'\x7f', 'Up': b'\x1b[A', 'Down': b'\x1b[B',
        'Right': b'\x1b[C', 'Left': b'\x1b[D', 'Home': b'\x1b[H', 'End': b'\x1b[F', 'PageUp': b'\x1b[5~', 'PageDown': b'\x1b[6~',
        'S-Tab': b'\x1b[Z'}


def encode_keys(keys):
    """tmux send-keys names: Enter, Down, Esc, C-c …; anything else is typed as text."""
    data = b''
    for key in keys:
        if key in KEYS:
            data += KEYS[key]
        elif re.fullmatch(r'C-[a-zA-Z]', key):
            data += bytes([ord(key[2].lower()) - 96])
        else:
            data += key.encode()
    return data


def keys(name, keys, wait=.8, lines=20):
    """Type into NAME's terminal, then return its screen. For an agent you started that is stuck on a screen peer
    cannot reach (a first-run question, before its first prompt). Read it first (sm tail): a choice that is
    the user's to make is not yours to answer."""
    entry = load(name)
    if status(entry) != 'running':
        raise ValueError(name + ' is not running (' + status(entry) + '); sm resume ' + name)
    data = encode_keys(keys)
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(3)
        sock.connect(entry['hold'])
        sock.sendall(bytes([2]) + len(data).to_bytes(4, 'big') + data)  # INPUT (hold/PROTOCOL.md); no HELLO: size stays
    time.sleep(wait)
    return tail(name, lines)


def tail(name, lines=40, raw=False):
    entry = load(name)
    out = subprocess.run([str(BIN / 'peer-hold'), 'tail', '--sock', entry['hold'], '--lines', str(lines)] + (['--raw'] if raw else []),
                         capture_output=True, timeout=5)
    if out.returncode:
        raise ValueError(out.stderr.decode(errors='replace').strip())
    return out.stdout


def attach(name, passive=False, screen=True):
    """Become a viewer of NAME, and stay one: the pane is the session's place, not one process's.

    Being looked at turns it on: opening a pane
    resumes a session that is asleep, stopped or dead, showing its last screen until it repaints. If it ends
    while shown, a sleep or a crash brings it straight back, but a deliberate `sm stop` (to save quota) is
    respected: it waits for a key here, or for something else to start it."""
    first = True
    while True:
        with name_lock(name):
            entry = load(name)
            state = status(entry)
            if state == 'tmux':
                # Still in tmux (sm migrate moves it): show that pane rather than starting a second copy.
                os.execvp('tmux', ['tmux', 'attach-session', '-t', entry['tmux_pane']])
            stopped = state == 'ended' and not entry.get('slept')
            # Opening always wakes it. Afterwards only a real screen does: a sleep while a passive leftover (a tmux
            # viewer) is attached must hold, and a deliberate stop is respected by everyone.
            if state != 'running' and (first or not (stopped or passive)):
                if first and screen:
                    show_last_screen(entry)
                _resume(entry, timeout=0)
                woke = True
                state = 'running'
            else:
                woke = False
        if woke:
            # Keys the user types while it wakes stay in this terminal until the agent's screen is up, then reach it
            # in order; typed into a half-started TUI they would be lost or echoed raw.
            wait_ready(load(name))
        if state != 'running':
            if not wait_for_use(name):
                sys.exit(0)
            with name_lock(name):
                if status(load(name)) != 'running':
                    _resume(load(name), timeout=0)
            wait_ready(load(name))
        if not first and screen:
            sys.stdout.buffer.write(b'\x1b[H\x1b[2J')  # the agent repaints its own screen
            sys.stdout.flush()
        first = False
        subprocess.run([str(BIN / 'peer-hold'), 'attach', '--sock', entry['hold']] + (['--passive'] if passive else []))


def view_state(name):
    """What a screen should show for NAME: asleep (dim it, opening wakes it), stopped (dim it, a key wakes it),
    waking (process up, its screen not yet stood up — keep the old frame), ready (usable), tmux, gone."""
    try:
        entry = load(name)
    except ValueError:
        return 'gone'
    state = status(entry)
    if state == 'tmux':
        return 'tmux'
    if state == 'dead':
        return 'asleep'  # a crash or reboot: opening it brings it back, same as a sleep
    if state == 'ended':
        return 'asleep' if entry.get('slept') else 'stopped'
    if entry.get('kind') == 'shell':
        return 'ready'
    row = peer_row(learn_session(entry))
    return 'ready' if row and (row.get('observed') or 0) >= (entry.get('started') or 0) else 'waking'


def watch_state(name, interval=.3, heartbeat=20, steady=2.0, settle=5.0):
    """Fast (`interval`) while it wakes and for `settle` s after any change — the app reveals the frame on
    ready, so that edge must be quick. Otherwise every `steady` s: each look reads the entry, the holder and
    the peer registry, and the apps keep one watcher per visible pane; at .3 s eleven of them used 27% CPU
    on the Mac (11/03: battery). A stop seen 2 s late only dims the frame 2 s late."""
    seen, beat, changed = None, time.monotonic(), time.monotonic()
    while True:
        state = view_state(name)
        now = time.monotonic()
        if state != seen:
            changed = now
        if state != seen or now - beat >= heartbeat:
            seen, beat = state, now
            yield dict(name=name, state=state, at=time.time())
        time.sleep(interval if state == 'waking' or now - changed < settle else steady)


def last_screen(name):
    """A frozen snapshot, including conversion of pre-snapshot holders' raw logs."""
    try:
        path = Path(load(name)['hold']).with_suffix('.screen')
        if not path.exists():
            return b''
        result = subprocess.run([str(BIN / 'peer-hold'), 'screen', '--file', str(path)],
                                capture_output=True, timeout=5)
        # Never fall back to replaying a raw log: terminal queries can wake or
        # type into the next agent. Keep the file for diagnosis if decoding fails.
        return result.stdout if result.returncode == 0 else b''
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return b''


def wait_ready(entry, timeout=None):
    """Until the resumed agent registers again (its start hook runs once the TUI is up), or output settles."""
    timeout = float(os.environ.get('SM_READY_TIMEOUT', 15)) if timeout is None else timeout
    end = time.monotonic() + timeout
    started = entry.get('started') or 0
    while time.monotonic() < end:
        row = peer_row(learn_session(load(entry['name'])))
        if row and (row.get('observed') or 0) >= started:
            return True
        if status(load(entry['name'])) == 'ended':
            return False  # it could not start; attach shows why
        time.sleep(.2)
    return False


def show_last_screen(entry):
    data = last_screen(entry['name'])
    # The snapshot includes its clear inside synchronized output; clearing before
    # it would briefly expose a blank frame on an ordinary attach terminal.
    sys.stdout.buffer.write(data)
    sys.stdout.flush()


def wait_for_use(name):
    """Block until a key arrives on this terminal (then wake) or the session runs again; False if stdin closed."""
    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd) if os.isatty(fd) else None
    try:
        if saved:
            tty.setraw(fd)
        while True:
            ready, _, _ = select.select([fd], [], [], 2)
            if ready:
                if not os.read(fd, 64):
                    return False
                return True  # attach resumes it; the key itself is dropped, the agent never saw it typed
            if status(load(name)) == 'running':
                return True
    finally:
        if saved:
            termios.tcsetattr(fd, termios.TCSANOW, saved)


# ---------- other machines ----------

def ssh_of(host):
    """The ssh destination peer's hosts.json gives HOST ({"ssh": "me@server"}), or None for a relay-only machine."""
    from peer_transport import hosts
    entry = hosts().get(host)
    value = entry.get('ssh') if isinstance(entry, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def alias(host):
    value = ssh_of(host)
    if not value:
        raise ValueError(host + ' needs {"ssh": "..."} in peer\'s hosts.json (relay-only hosts cannot run sm)')
    return value


REMOTE = '"$HOME/.smux/bin/sm-session"'


def direct(host):
    return ssh_of(host)


def remote(host, argv, tty=False):
    if not direct(host):
        # No ssh into that machine (the Mac): run the same sm command there through the relay tunnel.
        if tty:
            raise ValueError(host + ' is reached through the relay, which cannot carry an interactive screen; '
                                    'attach from ' + host + ' itself (its sm app or a terminal there)')
        import io, peer_remote
        out, err = io.BytesIO(), io.BytesIO()
        code = peer_remote.run(host, ['~/.smux/bin/sm-session'] + list(argv), out=out, err=err)
        if code:
            raise ValueError(host + ': ' + err.getvalue().decode(errors='replace').strip()[:500])
        return out.getvalue()
    command = REMOTE + ' ' + ' '.join(shlex.quote(a) for a in argv)
    if tty:
        # The app's panes (--no-screen) keep ssh's own lines off the screen: past the shared master's 10 sessions ssh
        # falls back to a new connection and says so in the pane ("Session open refused by peer"), and the stage took
        # that text for the agent's screen. The app learns of a real failure from the exit, a person at `sm attach`
        # still reads why. Past 10 panes a fresh connection is what costs (3-40 s on 11/07 through `home path`).
        quiet = ['-o', 'LogLevel=QUIET'] if '--no-screen' in argv else []
        os.execvp('ssh', ['ssh', '-t', '-o', 'ConnectTimeout=5'] + quiet + lane(host, argv) + [alias(host), command])
    # Short commands get their own ssh master. The shared one carries every attached pane and hits the server's
    # MaxSessions (10 by default), and the fresh connection ssh falls back to then failed on a shaky network:
    # 5 of 144 agent commands died on "Session open refused by peer" (11/07 sm usage). 255 means ssh itself failed
    # and the command never ran, so one retry cannot run it twice.
    argv_ssh = ['ssh', '-n', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5'] + SHORT + [alias(host), command]
    out = subprocess.run(argv_ssh, capture_output=True, timeout=60)
    if out.returncode == 255:
        time.sleep(1)
        out = subprocess.run(argv_ssh, capture_output=True, timeout=60)
    if out.returncode:
        raise ValueError(host + ': ' + out.stderr.decode(errors='replace').strip()[:500])
    return out.stdout


# Short commands (sm run, cp, layout verbs) get a master of their own, so they never queue behind panes.
SHORT = ['-o', 'ControlMaster=auto', '-o', 'ControlPath=~/.ssh/cm-sm-%C', '-o', 'ControlPersist=600']
LANES = 32


def lane(host, argv):
    """Long-lived streams (a pane's attach and its state watch) spread over a few masters of their own. One master
    carries at most the server's MaxSessions (10) and every shown remote pane holds two: with 29 panes ssh fell back
    to a fresh connection each time, and sm run sharing that master was refused (11/09 sm feedback). Both streams of
    one session land on the same lane, so a lane holds about 2 x panes / 32."""
    name = next((a for a in argv if not a.startswith('-') and a not in ('attach', 'state')), '')
    n = int(hashlib.sha1((host + '/' + name).encode()).hexdigest(), 16) % LANES
    return ['-o', 'ControlMaster=auto', '-o', 'ControlPath=~/.ssh/cm-sm-l%d-%%C' % n, '-o', 'ControlPersist=60']


def stream(host, argv):
    """Replace this process with ssh running argv on host, output unbuffered (sm watch)."""
    command = REMOTE + ' ' + ' '.join(shlex.quote(a) for a in argv)
    os.execvp('ssh', ['ssh', '-n', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=15',
                      '-o', 'LogLevel=QUIET'] + lane(host, argv) + [alias(host), command])
