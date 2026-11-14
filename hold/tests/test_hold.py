#!/usr/bin/env python3
"""peer-hold behaviour tests: run with `python3 -I tests/test_hold.py [path/to/peer-hold]`.

Each test starts a real holder, attaches through a real PTY (as sm/Fine would), and checks
what a viewer sees. Sockets live in a short temp dir (macOS caps socket paths at 104 bytes).
"""
import fcntl, json, os, pty, select, shutil, signal, struct, subprocess, sys, tempfile, termios, time

BIN = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else
                      os.path.join(os.path.dirname(__file__), '..', 'target', 'release', 'peer-hold'))
TMP = tempfile.mkdtemp(prefix='ph', dir='/tmp')
holders = []


def start(name, *cmd, extra=()):
    sock = os.path.join(TMP, name + '.sock')
    out = subprocess.run([BIN, 'start', '--sock', sock, *extra, '--', *cmd],
                         capture_output=True, text=True, timeout=5)
    assert out.returncode == 0, out.stderr
    info = json.loads(out.stdout)
    holders.append(info)
    return sock, info


class Viewer:
    """`peer-hold attach` running on its own PTY, like a terminal pane."""

    def __init__(self, sock, rows=24, cols=80, passive=False):
        pid, fd = pty.fork()
        if pid == 0:
            os.execv(BIN, [BIN, 'attach', '--sock', sock] + (['--passive'] if passive else []))
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
        self.pid, self.fd, self.buf = pid, fd, b''

    def read_until(self, needle, timeout=4.0):
        needle = needle.encode() if isinstance(needle, str) else needle
        end = time.time() + timeout
        while needle not in self.buf and time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                self.buf += data
        return needle in self.buf

    def write(self, text):
        os.write(self.fd, text.encode())

    def resize(self, rows, cols):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
        os.kill(self.pid, signal.SIGWINCH)

    def wait(self, timeout=4.0):
        end = time.time() + timeout
        while time.time() < end:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                return os.waitstatus_to_exitcode(status)
            self.read_until(b'\x00never', 0.05)
        return None

    def close(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        os.close(self.fd)


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_roundtrip():
    sock, _ = start('rt', '/bin/cat')
    v = Viewer(sock)
    v.write('hello-hold\r')
    assert v.read_until('hello-hold'), v.buf
    v.close()


def test_parentless_and_survives_viewer():
    sock, info = start('pl', '/bin/sh', '-c', 'while :; do sleep 1; done')
    ppid = int(subprocess.check_output(['ps', '-o', 'ppid=', '-p', str(info['holder'])]).strip())
    assert ppid != os.getpid(), 'holder still has the starter as parent'
    v = Viewer(sock)
    v.close()  # viewer killed hard, like a closed sm window or a dropped ssh
    time.sleep(0.3)
    assert alive(info['holder']) and alive(info['child'])


def test_reattach_restores_visible_screen():
    sock, _ = start('re', '/bin/sh', '-c', 'i=0; while :; do echo tick$i; i=$((i+1)); sleep 0.1; done')
    time.sleep(0.6)
    v = Viewer(sock)
    assert v.read_until('tick0'), 'visible output written with no viewer must be restored'
    v.close()
    time.sleep(0.4)
    v = Viewer(sock)
    assert v.read_until('tick0') and v.read_until('tick8'), v.buf[-200:]
    v.close()


def test_resize_reaches_child():
    # Size is polled (bash drops a WINCH that lands while its previous trap runs), and the
    # signal is checked once the trap is known to be installed ("ready"), so neither races.
    script = ('trap "echo winch" WINCH; echo ready; last=; '
              'while :; do s=$(stty size); [ "$s" != "$last" ] && echo "size $s" && last=$s; sleep 0.05; done')
    sock, _ = start('rs', '/bin/sh', '-c', script)
    v = Viewer(sock, rows=30, cols=100)
    assert v.read_until('ready') and v.read_until('size 30 100'), v.buf
    time.sleep(0.2)
    v.resize(33, 111)
    assert v.read_until('size 33 111') and v.read_until('winch'), v.buf
    v.close()


def test_no_viewer_no_stall():
    # 5MB of output with nobody attached: a holder that stops reading would block the child here.
    sock, _ = start('ns', '/bin/sh', '-c', 'yes | head -c 5000000 >/dev/tty; echo ALLDONE; sleep 30',
                    extra=('--ring', '65536'))
    # A PTY moves ~1MB/s with onlcr, so allow time; the point is that it finishes with nobody watching.
    time.sleep(2.0)
    v = Viewer(sock)
    assert v.read_until('ALLDONE', timeout=20), v.buf[-300:]
    v.close()


def test_exit_code_and_cleanup():
    sock, info = start('ex', '/bin/sh', '-c', 'sleep 0.6; exit 7')
    v = Viewer(sock)
    code = v.wait()
    assert code == 7, code
    time.sleep(0.2)
    assert not os.path.exists(sock)
    assert open(sock[:-5] + '.exit').read().strip() == '7'
    assert os.path.getsize(sock[:-5] + '.screen') >= 0  # last screen kept for viewers opened later
    assert not alive(info['holder'])
    err = subprocess.run([BIN, 'attach', '--sock', sock], capture_output=True, text=True)
    assert err.returncode == 1 and 'has ended (exit 7)' in err.stderr, err.stderr


def test_status_and_meta():
    sock, info = start('st', '/bin/sh', '-c', 'while :; do sleep 1; done', extra=('--cwd', '/tmp'))
    s = json.loads(subprocess.check_output([BIN, 'status', '--sock', sock]))
    assert s['child'] == info['child'] and s['viewers'] == 0, s
    meta = json.load(open(sock[:-5] + '.json'))
    assert meta['cmd'][0] == '/bin/sh' and meta['cwd'] == '/tmp' and meta['env'] == [], meta


def test_modes_restored_after_ring_rolls():
    # Alt screen + bracketed paste set long before the replay window must still be in effect.
    script = r'printf "\033[?1049h\033[?2004h"; i=0; while [ $i -lt 400 ]; do echo line$i; i=$((i+1)); done; sleep 30'
    sock, _ = start('md', '/bin/sh', '-c', script, extra=('--ring', '2000'))
    time.sleep(0.8)
    v = Viewer(sock)
    assert v.read_until('line399')
    assert v.read_until(b'\x1b[?1049h') and v.read_until(b'\x1b[?2004h'), v.buf[-120:]
    v.close()


def test_reconnect_snapshot_never_replays_queries_or_requests_redraw():
    ready = os.path.join(TMP, 'snapshot-ready')
    script = r'''
import os, signal, tty
from pathlib import Path
tty.setraw(0)
signal.signal(signal.SIGWINCH, lambda *_: os.write(1, b'UNWANTED-REDRAW'))
os.write(1, (b'\x1b[H\x1b[2Jpast output\r\n' * 60000))
os.write(1, b'\x1b[H\x1b[2JCURRENT\r\n\x1b[c\x1b[>c\x1b[6n\x1b[5n\x1b]4;0;?\x07\x1b]10;?\x07\x1b]11;?\x07\x1b]12;?\x07\x1bP$qm\x1b\\')
Path(READY).touch()
while True:
    data = os.read(0, 4096)
    if data == b'q':
        os.write(1, b'LIVE-QUERY\x1b[6n')
    else:
        os.write(1, b'INPUT=' + data.hex().encode())
'''.replace('READY', repr(ready))
    sock, _ = start('snapshot', sys.executable, '-c', script)
    deadline = time.monotonic() + 10
    while not os.path.exists(ready):
        assert time.monotonic() < deadline, 'fixture stalled'
        time.sleep(.02)
    time.sleep(.1)  # holder drains the final PTY write
    for _ in range(2):
        v = Viewer(sock, cols=120, rows=40)
        try:
            assert v.read_until('CURRENT')
            v.read_until(b'never-present', .3)
            assert len(v.buf) < 1024, len(v.buf)
            assert b'past output' not in v.buf and b'UNWANTED-REDRAW' not in v.buf
            for query in (b'\x1b[c', b'\x1b[>c', b'\x1b[6n', b'\x1b[5n', b'\x1b]', b'\x1bP'):
                assert query not in v.buf, (query, v.buf)
            # Snapshot filtering must not eat a new, live query or ordinary input.
            v.buf = b''
            v.write('q')
            assert v.read_until(b'LIVE-QUERY\x1b[6n')
            v.write('\x1b[2;1R')
            assert v.read_until(b'INPUT=1b5b323b3152')
        finally:
            v.close()


def test_exit_file_is_current_screen_including_final_drain():
    sock, _ = start('last', '/bin/sh', '-c',
                    r'printf "old\033[c\033[H\033[2JFINAL\033[6n"')
    path = sock[:-5] + '.screen'
    deadline = time.monotonic() + 5
    while not os.path.exists(path):
        assert time.monotonic() < deadline
        time.sleep(.02)
    screen = open(path, 'rb').read()
    assert screen.startswith(b'PHSC\x01')
    assert b'FINAL' in screen and b'old' not in screen
    assert b'\x1b[c' not in screen and b'\x1b[6n' not in screen
    assert len(screen) < 1024
    rendered = subprocess.check_output([BIN, 'screen', '--file', path])
    assert rendered == screen[9:]


def test_legacy_saved_screen_converts_without_queries():
    path = os.path.join(TMP, 'legacy.screen')
    with open(path, 'wb') as f:
        f.write(b'past output\r\n' * 90000)
        f.write(b'\x1b[H\x1b[2JLEGACY CURRENT\x1b[c\x1b]11;?\x07\x1b[6')
    rendered = subprocess.check_output([BIN, 'screen', '--file', path])
    assert len(rendered) < 1024
    assert rendered.count(b'LEGACY CURRENT') == 1
    assert b'past output' not in rendered
    assert b'\x1b[c' not in rendered and b'\x1b]' not in rendered
    assert rendered.endswith(b'\x1b[?2026l'), 'a frozen screen must not leave a partial query open'


def test_refuses_double_start():
    sock, _ = start('dd', '/bin/sh', '-c', 'while :; do sleep 1; done')
    out = subprocess.run([BIN, 'start', '--sock', sock, '--', '/bin/true'], capture_output=True, text=True)
    assert out.returncode == 1 and 'already held' in out.stderr


def test_front_end_ids_not_inherited():
    env = dict(os.environ, FINE_TAB_ID='leak', SM_PANE_ID='leak', TMUX_PANE='%99')
    sock = os.path.join(TMP, 'en.sock')
    out = subprocess.run([BIN, 'start', '--sock', sock, '--env', 'SM_PANE_ID=mine', '--', '/bin/sh', '-c',
                          'echo "F=[$FINE_TAB_ID] S=[$SM_PANE_ID] T=[$TMUX_PANE] H=[$PEER_HOLD_SOCK]"; sleep 30'],
                         capture_output=True, text=True, env=env, timeout=5)
    holders.append(json.loads(out.stdout))
    v = Viewer(sock)
    assert v.read_until(f'F=[] S=[mine] T=[] H=[{sock}]'), v.buf
    v.close()


def test_tail_text_without_attaching():
    script = r'printf "\033[1;31mERROR\033[0m: boom\r\n\033]0;title\007plain line\r\nspin 1\rspin 2\r\n[REPORT] done\r\n"; sleep 30'
    sock, _ = start('tl', '/bin/sh', '-c', script)
    time.sleep(0.5)
    out = subprocess.check_output([BIN, 'tail', '--sock', sock, '--lines', '3'], text=True)
    assert out.splitlines() == ['plain line', 'spin 2', '[REPORT] done'], out
    full = subprocess.check_output([BIN, 'tail', '--sock', sock], text=True)
    assert full.splitlines()[0] == 'ERROR: boom', full
    raw = subprocess.check_output([BIN, 'tail', '--sock', sock, '--raw'])
    # Claude Code (Ink) steps down rows with CR + CSI B instead of newlines.
    sock2, _ = start('tl2', '/bin/sh', '-c', r'printf "row one\r\033[1Bxx\r\033[2C\033[1Brow two"; sleep 30')
    time.sleep(0.5)
    ink = subprocess.check_output([BIN, 'tail', '--sock', sock2], text=True).splitlines()
    assert ink == ['row one', 'xx', 'row two'], ink  # column offsets are not kept (no screen model)
    assert b'\x1b[1;31m' in raw


def test_size_follows_the_typing_viewer_and_passive_never_sets_it():
    sock, _ = start('sz', '/bin/cat')
    def size():
        return tuple(json.loads(subprocess.check_output([BIN, 'status', '--sock', sock]))[k] for k in ('cols', 'rows'))
    a = Viewer(sock, rows=30, cols=100); time.sleep(.4)
    assert size() == (100, 30), size()
    p = Viewer(sock, rows=50, cols=60, passive=True); time.sleep(.4)
    assert size() == (100, 30), 'passive viewer changed the size: %r' % (size(),)
    st = json.loads(subprocess.check_output([BIN, 'status', '--sock', sock]))
    assert (st['viewers'], st['active']) == (2, 1), st  # the passive one is not someone's screen
    p.resize(55, 70); time.sleep(.3)
    assert size() == (100, 30), size()
    b = Viewer(sock, rows=20, cols=90); time.sleep(.4)
    assert size() == (90, 20), size()  # a new active viewer takes it
    a.write('x'); time.sleep(.3)
    assert size() == (100, 30), size()  # typing in a takes it back
    a.close(); time.sleep(.4)
    assert size() == (90, 20), size()  # owner left: the remaining active viewer
    p.write('y'); time.sleep(.3)
    assert size() == (90, 20), size()  # passive typing still never resizes
    b.close(); p.close()


def main():
    tests = [v for k, v in globals().items() if k.startswith('test_')]
    failed = 0
    try:
        for t in tests:
            try:
                t()
                print('PASS', t.__name__)
            except AssertionError as e:
                failed += 1
                print('FAIL', t.__name__, repr(e)[:300])
    finally:
        for h in holders:
            for pid in (h['child'], h['holder']):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        shutil.rmtree(TMP, ignore_errors=True)
    print(f'{len(tests) - failed}/{len(tests)} passed')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
