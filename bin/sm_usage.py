"""What agents run with sm, and what they find awkward about it.

Agents work around a clumsy tool and say nothing; the next agent trips on the same thing (the tool's friction is the agent's productivity). So sm keeps two records, the way flat kit does:

- usage.jsonl on each machine: one line per command an agent or a person ran — the verb, the session it named,
  which flags (never their values: a brief or a command line can be private), the exit code, how long it took and
  the error text. Failures nobody mentioned show up in `sm usage`.
- feedback.jsonl on the Mac: `sm feedback "..."`, one line from whoever got stuck, with the commands they ran just
  before, so the owner (sm-front by default) sees what happened without asking. The owner gets a peer message.
The app's own calls (no PEER_NAME, no terminal) are not recorded: they are the bulk and say nothing about usability.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

BIN = Path(__file__).resolve().parent
STREAMING = {'watch', 'attach'}  # long-lived viewers the app runs; nothing to learn from their duration
LIMIT = 2 * 1024 * 1024


def state_dir():
    return Path(os.environ.get('SM_STATE') or Path.home() / '.smux/state/sm')


def who():
    return os.environ.get('PEER_NAME') or ('tty' if sys.stdin.isatty() else None)


def host():
    try:
        sys.path.insert(0, str(BIN))
        import peer_core
        return peer_core.host()
    except Exception:
        return socket.gethostname().split('.')[0]


def summarize(argv):
    """verb, sub (ws/tab), target and flag names — no values, nothing after `--`."""
    args = argv[:argv.index('--')] if '--' in argv else list(argv)
    args = [a for a in args if a != '--json']
    verb = args[0] if args else ''
    rest = args[1:]
    sub = rest[0] if verb in ('ws', 'tab') and rest and not rest[0].startswith('-') else None
    if sub:
        rest = rest[1:]
    flags = sorted({a.split('=', 1)[0] for a in rest if a.startswith('-')})
    positional = [a for a in rest if not a.startswith('-')]
    target = positional[0] if positional and len(positional[0]) <= 64 and ' ' not in positional[0] else None
    if verb in ('run', 'cp', 'keys', 'feedback'):
        target = positional[0] if verb in ('run', 'keys') and positional else None  # host / session only
    return dict(verb=verb, sub=sub, target=target, flags=flags)


def append(path, entry):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LIMIT:
        path.replace(path.with_suffix('.1.jsonl'))
    with open(path, 'a') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def record(argv, code, error, seconds):
    caller = who()
    if not caller or not argv or argv[0] in STREAMING or (argv[0] == 'state' and '--watch' in argv):
        return
    try:
        entry = dict(at=round(time.time(), 3), host=host(), who=caller, ok=code == 0, code=code,
                     ms=int(seconds * 1000), error=(error or '')[-300:] or None, **summarize(argv))
        append(state_dir() / 'usage.jsonl', entry)
    except OSError:
        pass  # a record is never worth failing the command


def read(path, since=0):
    rows = []
    for p in (path.with_suffix('.1.jsonl'), path):
        if p.exists():
            for line in p.read_text(errors='replace').splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get('at', 0) >= since:
                    rows.append(row)
    return rows


def recent(caller, n=5):
    rows = [r for r in read(state_dir() / 'usage.jsonl', time.time() - 3600) if r.get('who') == caller]
    return [dict((k, r.get(k)) for k in ('verb', 'sub', 'target', 'flags', 'ok', 'ms', 'error')) for r in rows[-n:]]


def owner():
    if os.environ.get('SM_FEEDBACK_TO'):
        return os.environ['SM_FEEDBACK_TO']
    try:
        return (Path.home() / '.smux/feedback-owner').read_text().strip()
    except OSError:
        return ''  # no owner set: feedback is kept locally (`sm feedback` lists it)


def keep_feedback(entry):
    """On the Mac (where the owner reads): store it and tell the owner. Returns what to print."""
    append(state_dir() / 'feedback.jsonl', entry)
    to = owner()
    line = '[sm feedback] %s@%s: %s' % (entry['from'], entry['host'], entry['text'][:600])
    if entry.get('recent'):
        line += ' | commands before: ' + '; '.join(
            ' '.join(x for x in ('sm', r.get('verb'), r.get('sub'), r.get('target')) if x)
            + ('' if r.get('ok') else ' ✗ ' + (r.get('error') or '')[:120]) for r in entry['recent'])
    if not to:
        return 'thanks — kept (sm feedback lists them; set ~/.smux/feedback-owner to an agent name to send them there)'
    try:
        subprocess.run([str(BIN / 'peer'), 'say', to, line], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass  # stored either way; the owner reads `sm feedback` too
    return 'thanks — kept for ' + to + ' (sm feedback lists them)'


def feedback(text, app_is_here, to_app):
    caller = who() or 'unknown'
    entry = dict(at=round(time.time(), 3), host=host(), **{'from': caller}, cwd=os.getcwd(), text=text.strip()[:2000],
                 recent=recent(caller))
    if app_is_here:
        return keep_feedback(entry)
    return to_app(['feedback', '--keep', json.dumps(entry, ensure_ascii=False)])


def list_feedback(days=7):
    rows = read(state_dir() / 'feedback.jsonl', time.time() - days * 86400)
    if not rows:
        return 'no feedback in the last %d days' % days
    out = []
    for r in rows[-30:]:
        out.append(time.strftime('%m/%d %H:%M', time.localtime(r['at'])) + '  ' + r['from'] + '@' + r['host'] + '  ' + r['text'])
        for c in r.get('recent') or []:
            if not c.get('ok'):
                out.append('      ✗ sm ' + ' '.join(x for x in (c.get('verb'), c.get('sub'), c.get('target')) if x)
                           + ': ' + (c.get('error') or '')[:160])
    return '\n'.join(out)


def report(hours=24, as_json=False):
    rows = read(state_dir() / 'usage.jsonl', time.time() - hours * 3600)
    groups = {}
    for r in rows:
        key = ' '.join(x for x in (r.get('verb'), r.get('sub')) if x)
        groups.setdefault(key, []).append(r)
    summary = []
    for key, rs in groups.items():
        ms = sorted(r.get('ms', 0) for r in rs)
        summary.append(dict(command=key, runs=len(rs), failed=sum(not r.get('ok') for r in rs),
                            p50_ms=ms[len(ms) // 2], max_ms=ms[-1]))
    summary.sort(key=lambda s: (-s['failed'], -s['runs']))
    failures = [r for r in rows if not r.get('ok')][-15:]
    if as_json:
        return json.dumps(dict(host=host(), hours=hours, commands=summary, failures=failures), ensure_ascii=False)
    if not rows:
        return '%s: no sm commands recorded in the last %dh' % (host(), hours)
    out = ['%s, last %dh: %d commands by %d callers' % (host(), hours, len(rows), len({r.get('who') for r in rows})),
           '%-16s %5s %6s %8s %8s' % ('command', 'runs', 'failed', 'p50 ms', 'max ms')]
    out += ['%-16s %5d %6d %8d %8d' % (s['command'][:16], s['runs'], s['failed'], s['p50_ms'], s['max_ms']) for s in summary]
    if failures:
        out.append('recent failures:')
        out += ['  ' + time.strftime('%m/%d %H:%M', time.localtime(r['at'])) + '  ' + str(r.get('who')) + '  sm '
                + ' '.join(x for x in (r.get('verb'), r.get('sub'), r.get('target')) if x)
                + ' → ' + ((r.get('error') or 'exit %s' % r.get('code'))[:160]) for r in failures]
    return '\n'.join(out)
