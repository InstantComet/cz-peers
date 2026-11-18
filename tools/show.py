"""Test helper: replay drive.py output and print the screen at each checkpoint.

Understands only what app.py emits (cursor position, erase display, SGR, mode
switches), plus enough to skip anything else. Selected/coloured cells are not
shown; the right margin shows '#' for the selection colour, '=' for bars.
"""
import json
import re
import sys

d = json.load(open(sys.argv[1], encoding='utf-8'))
data = d['data'].encode('latin1')  # marks are byte offsets
W, H = d['cols'], d['rows']
grid = [[' '] * W for _ in range(H)]
hl = [''] * H
x = y = 0
bg_on = False
TOKEN = re.compile(r'\x1b\[([?0-9;]*)([A-Za-z])|\x1b[()][0-9A-Za-z]|\x1b.|[\r\n]|[^\x1b\r\n]')


def feed(text):
    global x, y, bg_on
    for m in TOKEN.finditer(text):
        tok = m.group(0)
        if m.group(2):
            params, cmd = m.group(1), m.group(2)
            if cmd == 'H':
                parts = (params or '1;1').split(';')
                y = int(parts[0] or 1) - 1
                x = int(parts[1] or 1) - 1 if len(parts) > 1 else 0
            elif cmd == 'J' and params == '2':
                for row in grid:
                    row[:] = [' '] * W
            elif cmd == 'm':
                codes = params.split(';')
                bg_on = ''
                if '48' in codes:
                    i = codes.index('48')
                    bg_on = '#' if codes[i + 2:i + 5] == ['32', '58', '88'] else '='
        elif tok == '\r':
            x = 0
        elif tok == '\n':
            y = min(H - 1, y + 1)
        elif tok.startswith('\x1b'):
            continue
        else:
            if 0 <= y < H and 0 <= x < W:
                grid[y][x] = tok
                if x == 0:
                    hl[y] = bg_on
            x += 1


pos = 0
for name, end in d['marks']:
    feed(data[pos:end].decode('utf-8', errors='replace'))
    pos = end
    print('=== after %r' % name)
    print('+' + '-' * W + '+')
    for i, row in enumerate(grid):
        print('|' + ''.join(row) + '|' + hl[i])
    print('+' + '-' * W + '+')
