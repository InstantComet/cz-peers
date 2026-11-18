"""Self-check for app/qr.py: decode what it drew and compare.

Reads the format bits (both copies, checked against every valid format word),
unmasks, reads the codewords back in zigzag order, de-interleaves them,
checks every Reed-Solomon block's syndromes are zero, and decodes the
byte-mode payload. Also writes a PNG per sample (needs Pillow) to scan by eye.

usage: python tools/qr_check.py [OUT_DIR]
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))
import qr  # noqa: E402

EXP = [0] * 512
x = 1
for i in range(255):
    EXP[i] = x
    x <<= 1
    if x & 0x100:
        x ^= 0x11D
for i in range(255, 512):
    EXP[i] = EXP[i - 255]


def gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return EXP[(LOG[a] + LOG[b]) % 255]


LOG = [0] * 256
for i in range(255):
    LOG[EXP[i]] = i


def format_word(ecl_bits, mask):
    data = ecl_bits << 3 | mask
    # plain polynomial division, written differently from qr.py on purpose
    v = data << 10
    for i in range(14, 9, -1):
        if v >> i & 1:
            v ^= 0b10100110111 << (i - 10)
    return (data << 10 | v) ^ 0b101010000010010


def read_format(m):
    n = len(m)
    a = [(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)] + [(14 - i, 8) for i in range(9, 15)]
    b = [(n - 1 - i, 8) for i in range(8)] + [(8, n - 15 + i) for i in range(8, 15)]
    words = []
    for coords in (a, b):
        words.append(sum(1 << i for i, (cx, cy) in enumerate(coords) if m[cy][cx]))
    assert words[0] == words[1], 'format copies differ'
    for ecl_bits in range(4):
        for mask in range(8):
            if format_word(ecl_bits, mask) == words[0]:
                assert m[n - 8][8], 'dark module missing'
                return ecl_bits, mask
    raise AssertionError('format bits are not a valid format word')


def check(text, out_dir=None):
    code = qr.encode(text)
    m = [row[:] for row in code.modules]
    n = code.size
    ecl_bits, mask = read_format(m)
    ecl = {1: 0, 0: 1}[ecl_bits]
    # function-pattern map from a clean object (only the layout is reused)
    fn = code.is_function
    f = qr._MASKS[mask]
    for y in range(n):
        for x in range(n):
            if not fn[y][x] and f(x, y):
                m[y][x] = not m[y][x]
    bits = []
    right = n - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(n):
            for j in range(2):
                xx = right - j
                y = n - 1 - vert if ((right + 1) & 2) == 0 else vert
                if not fn[y][xx]:
                    bits.append(m[y][xx])
        right -= 2
    raw = qr._raw_data_modules(code.version) // 8
    cws = [int(''.join('1' if b else '0' for b in bits[i * 8:i * 8 + 8]), 2) for i in range(raw)]
    nb = qr._NUM_BLOCKS[ecl][code.version]
    ecc = qr._ECC_PER_BLOCK[ecl][code.version]
    short = raw // nb
    nshort = nb - raw % nb
    lens = [short if i < nshort else short + 1 for i in range(nb)]
    blocks = [[] for _ in range(nb)]
    k = 0
    for i in range(max(lens)):
        for j in range(nb):
            if i < lens[j]:
                blocks[j].append(cws[k])
                k += 1
    data = []
    for blk in blocks:
        for r in range(ecc):  # syndrome S_r = blk(alpha^r) must be 0
            s = 0
            for c in blk:
                s = gf_mul(s, EXP[r]) ^ c
            assert s == 0, 'RS syndrome %d non-zero' % r
        data += blk[:len(blk) - ecc]
    stream = ''.join(format(c, '08b') for c in data)
    assert stream[:4] == '0100', 'not byte mode'
    cb = 8 if code.version <= 9 else 16
    count = int(stream[4:4 + cb], 2)
    payload = bytes(int(stream[4 + cb + 8 * i:12 + cb + 8 * i], 2) for i in range(count))
    assert payload.decode() == text, 'payload mismatch'
    if out_dir:
        from PIL import Image
        scale, quiet = 6, 4
        img = Image.new('L', ((n + 2 * quiet) * scale,) * 2, 255)
        for y in range(n):
            for xx in range(n):
                if code.modules[y][xx]:
                    img.paste(0, ((xx + quiet) * scale, (y + quiet) * scale,
                                  (xx + quiet + 1) * scale, (y + quiet + 1) * scale))
        img.save(os.path.join(out_dir, 'qr_v%d_%s.png' % (code.version, 'LM'[ecl])))
    return code


samples = ['https://login.tailscale.com/a/1a2b3c4d5e6f7', 'HELLO', 'https://example.com/',
           'x' * 100, 'https://login.tailscale.com/a/' + 'f' * 14]
rnd = random.Random(1)
samples += [''.join(rnd.choice('abcdefghijklmnopqrstuvwxyz0123456789/:.') for _ in range(rnd.randint(1, 200)))
            for _ in range(60)]
out = sys.argv[1] if len(sys.argv) > 1 else None
seen = set()
for s in samples:
    c = check(s, out if out and (c_key := (len(s) // 20)) not in seen else None)
    if out:
        seen.add(len(s) // 20)
print('ok: %d samples; login URL -> version %d, ecl %s, mask %d, %d modules' % (
    len(samples), *(lambda c: (c.version, 'LM'[c.ecl], c.mask, c.size))(qr.encode(samples[0]))))
