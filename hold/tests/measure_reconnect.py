#!/usr/bin/env python3
"""Isolated SM_TEST_LAUNCHER reconnect measurement; never starts an engine.

python3 -I hold/tests/measure_reconnect.py /path/to/peer-hold [output-directory]
Transport timings exclude the 150ms silence observation window and renderer time.
Replies are a deterministic terminal stand-in, not a libghostty measurement.
"""
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import struct
import sys
import tempfile
import time

BIN = Path(sys.argv[1]).resolve()
SOURCE = Path(__file__).resolve().parents[2] / 'bin'
sys.path.insert(0, str(SOURCE))
import sm_session as sm

QUERY_REPLIES = {
    b'\x1b[c': b'\x1b[?62;22;52c',
    b'\x1b[6n': b'\x1b[2;1R',
    b'\x1b]11;?\x07': b'\x1b]11;rgb:1e1e/1e1e/1e1e\x07',
}


def frame(kind, data):
    return struct.pack('>BI', kind, len(data)) + data


def main():
    root = Path(tempfile.mkdtemp(prefix='recon-', dir='/tmp'))
    info = None
    try:
        for key in list(os.environ):
            if key.startswith(('PEER_', 'SM_', 'FINE_', 'CLAUDE', 'CODEX', 'TMUX')):
                os.environ.pop(key)
        os.environ.update(SM_STATE=str(root / 'sm'), PEER_STATE=str(root / 'peer'),
                          TMUX_BRIDGE_CONFIG=str(root / 'config'),
                          SM_TEST_LAUNCHER=str(root / 'agent'))
        (root / 'peer-hold').symlink_to(BIN)
        sm.BIN = root
        fake = root / 'agent'
        fake.write_text(f'''#!{sys.executable}
import os, signal, tty
from pathlib import Path
tty.setraw(0)
def winch(*_):
    with open('winches', 'ab') as f: f.write(b'W')
    os.write(1, b'\\x1b[Hresize redraw\\r\\n')
signal.signal(signal.SIGWINCH, winch)
for i in range(600):
    os.write(1, b'\\x1b[H\\x1b[2J' + (('frame %04d ' % i) + 'x' * 109 + '\\r\\n').encode() * 20)
os.write(1, b'\\x1b[H\\x1b[2JCURRENT SCREEN\\r\\n\\x1b[c\\x1b[6n\\x1b]11;?\\x07')
Path('ready').touch()
while True:
    data = os.read(0, 4096)
    with open('input', 'ab') as f: f.write(data)
''')
        fake.chmod(0o755)
        entry = dict(name='reconnect-fixture', kind='shell', hold=str(root / 'h.sock'), cwd=str(root), env=[])
        info = sm.start_holder(entry, sm.launcher('shell', []))
        deadline = time.monotonic() + 15
        while not (root / 'ready').exists():
            assert time.monotonic() < deadline, 'fixture did not finish output'
            time.sleep(.02)
        time.sleep(.1)
        samples = []
        for iteration in range(5):
            with socket.socket(socket.AF_UNIX) as s:
                s.connect(entry['hold'])
                start = last = time.monotonic()
                s.sendall(frame(1, struct.pack('>HH', 120, 40)))
                pending = b''
                output = b''
                while select.select([s], [], [], .15)[0]:
                    data = s.recv(65536)
                    assert data, 'holder disconnected'
                    pending += data
                    while len(pending) >= 5:
                        kind, length = struct.unpack('>BI', pending[:5])
                        if len(pending) < length + 5:
                            break
                        payload, pending = pending[5:5 + length], pending[5 + length:]
                        if kind == 10:
                            output += payload
                            last = time.monotonic()
                    assert time.monotonic() - start < 5, 'attach never settled'
                replies = b''.join(reply * output.count(query) for query, reply in QUERY_REPLIES.items())
                if replies:
                    s.sendall(frame(2, replies))
                    time.sleep(.05)
                samples.append(dict(bytes=len(output), last_output_ms=round((last-start)*1000, 3),
                                    query_count=sum(output.count(q) for q in QUERY_REPLIES)))
                if iteration == 0 and len(sys.argv) > 2:
                    destination = Path(sys.argv[2]); destination.mkdir(parents=True, exist_ok=True)
                    (destination / 'attach.bin').write_bytes(output)
        received = (root / 'input').read_bytes() if (root / 'input').exists() else b''
        result = dict(binary=str(BIN), samples=samples, agent_input_bytes=len(received),
                      agent_input_hex=received.hex(),
                      winches=len((root / 'winches').read_bytes()) if (root / 'winches').exists() else 0)
        print(json.dumps(result, indent=2))
        if len(sys.argv) > 2:
            (Path(sys.argv[2]) / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    finally:
        if info:
            for pid in (info['child'], info['holder']):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        shutil.rmtree(root)


if __name__ == '__main__':
    main()
