#!/usr/bin/python3
"""Store screenshots without the device's real screen (or real devices).

Replays a tools/drive.py recording the way the launcher's terminal draws it:
the same 16-colour mapping (rgb_to_palette in the launcher's ui_app_st.cpp),
bold brightening, 7x15 px cells under a 20 px top bar, and the launcher's
fonts. Run it on the CardputerZero (needs Pillow and the launcher's fonts).
Pair it with tools/fake_tailscaled.py so only placeholder devices show.

usage: screenshot.py REC.json MARK OUT.png [--qr] [--scale N]
  MARK: index of the checkpoint to show (0 = 'start', 1 = after the first key...)
  --qr: also paint the login QR code the way the app's overlay does
  --scale N: draw at N x 320x170 with the fonts scaled up (sharp README/cover art)
"""
import json
import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

FONT_DIR = '/usr/share/APPLaunch/share/font/'
SCALE = int(sys.argv[sys.argv.index('--scale') + 1]) if '--scale' in sys.argv else 1
MONO = ImageFont.truetype(FONT_DIR + 'JetBrainsMono-Bold.ttf', 12 * SCALE)
SYMBOLS = ImageFont.truetype(FONT_DIR + 'DejaVuSansMono.ttf', 12 * SCALE)
BAR = ImageFont.truetype(FONT_DIR + 'Montserrat-Bold.ttf', 13 * SCALE)
PALETTE = [0x0D1117, 0xFF5F56, 0x27C93F, 0xFFBD2E, 0x2F81F7, 0xBC8CFF, 0x39C5CF, 0xF0F6FC,
           0x6E7681, 0xFFA198, 0x56D364, 0xE3B341, 0x79C0FF, 0xD2A8FF, 0x56D4DD, 0xFFFFFF]
DEFAULT_FG, DEFAULT_BG = 7, 0
CW, CH, TOP = 7, 15, 20


def rgb_to_palette(r, g, b):
    hi, lo = max(r, g, b), min(r, g, b)
    if hi < 80:
        return 0
    if hi - lo < 35:
        return 15 if hi > 180 else 8
    if r >= g and r >= b:
        return 9 if r > 180 else 1
    if g >= r and g >= b:
        return 10 if g > 180 else 2
    return 12 if b > 180 else 4


def rgb(c):
    return (c >> 16, (c >> 8) & 255, c & 255)


def replay(data, cols, rows):
    grid = [[(' ', DEFAULT_FG, DEFAULT_BG, False) for _ in range(cols)] for _ in range(rows)]
    x = y = 0
    fg, bg, bold = DEFAULT_FG, DEFAULT_BG, False
    for m in re.finditer(r'\x1b\[([?0-9;]*)([A-Za-z])|\x1b.|[\r\n]|[^\x1b\r\n]', data):
        tok = m.group(0)
        if m.group(2):
            params, cmd = m.group(1), m.group(2)
            if params.startswith('?'):
                continue
            if cmd == 'H':
                parts = (params or '1;1').split(';')
                y = int(parts[0] or 1) - 1
                x = int(parts[1] or 1) - 1 if len(parts) > 1 else 0
            elif cmd == 'J' and params == '2':
                grid = [[(' ', DEFAULT_FG, DEFAULT_BG, False) for _ in range(cols)] for _ in range(rows)]
            elif cmd == 'm':
                p = [int(v) if v else 0 for v in (params or '0').split(';')]
                i = 0
                while i < len(p):
                    v = p[i]
                    if v == 0:
                        fg, bg, bold = DEFAULT_FG, DEFAULT_BG, False
                    elif v == 1:
                        bold = True
                    elif v in (38, 48) and i + 4 < len(p) and p[i + 1] == 2:
                        c = rgb_to_palette(*p[i + 2:i + 5])
                        if v == 38:
                            fg = c
                        else:
                            bg = c
                        i += 4
                    elif 30 <= v <= 37:
                        fg = v - 30
                    elif 90 <= v <= 97:
                        fg = v - 90 + 8
                    elif 40 <= v <= 47:
                        bg = v - 40
                    elif 100 <= v <= 107:
                        bg = v - 100 + 8
                    elif v == 39:
                        fg = DEFAULT_FG
                    elif v == 49:
                        bg = DEFAULT_BG
                    i += 1
        elif tok == '\r':
            x = 0
        elif tok == '\n':
            y = min(rows - 1, y + 1)
        elif tok.startswith('\x1b'):
            continue
        else:
            if 0 <= y < rows and 0 <= x < cols:
                grid[y][x] = (tok, fg, bg, bold)
            x += 1
    return grid


def top_bar(draw, title):
    """The launcher's status bar, measured from a real fb0 grab: title, Wi-Fi
    bars, then a clock pill and a battery pill (placeholder time and charge)."""
    S = SCALE
    draw.rectangle((0, 0, 320 * S - 1, TOP * S - 1), fill=(0, 0, 0))
    draw.text((5 * S, 10 * S), title, font=BAR, fill=rgb(0xCCAA00), anchor='lm')
    for i, top in enumerate((11, 8, 5, 2)):
        x = 203 + i * 6
        draw.rounded_rectangle((x * S, top * S, (x + 4) * S - 1, 17 * S - 1), 2 * S, fill=rgb(0x31CE31))
    for x0, x1, colour, text in ((235, 275, 0x9C6531, '10:09'), (279, 315, 0xAC61A4, '80%')):
        draw.rounded_rectangle((x0 * S, 2 * S, x1 * S - 1, 18 * S - 1), 3 * S, fill=rgb(colour))
        draw.text(((x0 + x1) / 2 * S, 10 * S), text, font=BAR, fill=(255, 255, 255), anchor='mm')


def main():
    rec, mark, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    d = json.load(open(rec, encoding='utf-8'))
    end = d['marks'][mark][1]
    data = d['data'].encode('latin1')[:end].decode('utf-8', errors='replace')
    cols, rows = d['cols'], d['rows']
    grid = replay(data, cols, rows)
    img = Image.new('RGB', (320 * SCALE, 170 * SCALE), rgb(PALETTE[DEFAULT_BG]))
    draw = ImageDraw.Draw(img)
    top_bar(draw, 'Peers')
    for y, line in enumerate(grid):
        for x, (ch, fg, bg, bold) in enumerate(line):
            if bold and fg < 8:
                fg += 8
            px, py = x * CW * SCALE, (TOP + y * CH) * SCALE
            draw.rectangle((px, py, px + CW * SCALE - 1, py + CH * SCALE - 1), fill=rgb(PALETTE[bg]))
            if ch != ' ':
                font = MONO if ord(ch) < 0x2000 else SYMBOLS
                draw.text((px, py), ch, font=font, fill=rgb(PALETTE[fg]))
    if '--qr' in sys.argv:
        import app
        # the URL is wrapped over two lines on screen; use the fake daemon's fixed one
        rows_px = app.qr_image('https://login.tailscale.com/a/1a2b3c4d5e6f7',
                               app.LoginView.QR_COLS * CW, 10 * CH)
        qr = Image.frombytes('RGB', (len(rows_px[0]) // 2, len(rows_px)), b''.join(rows_px), 'raw', 'BGR;16')
        qr = qr.resize((qr.width * SCALE, qr.height * SCALE), Image.NEAREST)
        img.paste(qr, (0, TOP * SCALE))
    img.save(out)


if __name__ == '__main__':
    main()
