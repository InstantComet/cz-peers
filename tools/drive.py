#!/usr/bin/python3
"""Test helper: run app.py in a 45x10 pty (the launcher terminal's size), type
keys, and record the output with a checkpoint after each key.

usage: drive.py OUT.json KEY...   KEY is UP DOWN LEFT RIGHT ENTER ESC TAB BS,
plain text, or WAIT:<seconds>. Render the result with tools/show.py.
"""
import fcntl
import json
import os
import pty
import select
import struct
import sys
import termios
import time

out, keys = sys.argv[1], sys.argv[2:]
SEQ = {'UP': b'\x1b[A', 'DOWN': b'\x1b[B', 'LEFT': b'\x1b[D', 'RIGHT': b'\x1b[C',
       'ENTER': b'\r', 'ESC': b'\x1b', 'TAB': b'\t', 'BS': b'\x7f'}
COLS, ROWS = int(os.environ.get('COLS', 45)), int(os.environ.get('ROWS', 10))
pid, fd = pty.fork()
if pid == 0:
    os.environ['TERM'] = 'st-256color'
    os.execvp('python3', ['python3', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app', 'app.py')])
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', ROWS, COLS, 0, 0))
buf, marks = b'', []


def pump(seconds):
    global buf
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:
                buf += os.read(fd, 65536)
            except OSError:
                return


pump(2.5)
marks.append(['start', len(buf)])
for k in keys:
    if k.startswith('WAIT:'):
        pump(float(k[5:]))
    else:
        os.write(fd, SEQ.get(k, k.encode()))
        pump(1.2)
    marks.append([k, len(buf)])
os.write(fd, b'\x03')
pump(0.5)
with open(out, 'w') as f:
    json.dump({'cols': COLS, 'rows': ROWS, 'data': buf.decode('latin1'), 'marks': marks}, f)
