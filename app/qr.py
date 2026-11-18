"""Minimal QR Code encoder (byte mode, versions 1-10, ECC L/M).

Enough for a Tailscale login URL; no third-party modules, so the package works
on a stock CardputerZero image. Follows the structure of Project Nayuki's
qrcodegen (MIT): build the function patterns, place the interleaved data and
Reed-Solomon codewords, then pick the mask with the lowest penalty.
"""

# Indexed [ecl][version]; ecl 0 = L, 1 = M. Index 0 is unused.
_ECC_PER_BLOCK = ((-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18),
                  (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26))
_NUM_BLOCKS = ((-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4),
               (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5))
_FORMAT_ECL = (1, 0)  # L, M as written in the format bits
MAX_VERSION = 10


def _raw_data_modules(ver):
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver, ecl):
    return _raw_data_modules(ver) // 8 - _ECC_PER_BLOCK[ecl][ver] * _NUM_BLOCKS[ecl][ver]


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _alignment_positions(ver, size):
    if ver == 1:
        return []
    n = ver // 7 + 2
    step = (ver * 8 + n * 3 + 5) // (n * 4 - 4) * 2
    result = [size - 7 - i * step for i in range(n - 1)] + [6]
    return list(reversed(result))


_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


class QR:
    def __init__(self, ver, ecl, codewords):
        self.version, self.ecl = ver, ecl
        self.size = n = ver * 4 + 17
        self.modules = [[False] * n for _ in range(n)]
        self.is_function = [[False] * n for _ in range(n)]
        self._draw_function_patterns()
        self._draw_codewords(self._add_ecc_and_interleave(codewords))
        best, best_penalty = 0, None
        for mask in range(8):
            self._apply_mask(mask)
            self._draw_format_bits(mask)
            penalty = self._penalty()
            if best_penalty is None or penalty < best_penalty:
                best, best_penalty = mask, penalty
            self._apply_mask(mask)  # XOR again to undo
        self.mask = best
        self._apply_mask(best)
        self._draw_format_bits(best)

    # -- function patterns
    def _set_function(self, x, y, dark):
        self.modules[y][x] = dark
        self.is_function[y][x] = True

    def _draw_function_patterns(self):
        n = self.size
        for i in range(n):
            self._set_function(6, i, i % 2 == 0)
            self._set_function(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self._set_function(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _alignment_positions(self.version, n)
        last = len(pos) - 1
        for i, ax in enumerate(pos):
            for j, ay in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set_function(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format_bits(0)  # reserve the area; redrawn with the real mask
        self._draw_version()

    def _draw_format_bits(self, mask):
        data = _FORMAT_ECL[self.ecl] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 != 0
        n = self.size
        for i in range(6):
            self._set_function(8, i, bit(i))
        self._set_function(8, 7, bit(6))
        self._set_function(8, 8, bit(7))
        self._set_function(7, 8, bit(8))
        for i in range(9, 15):
            self._set_function(14 - i, 8, bit(i))
        for i in range(8):
            self._set_function(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set_function(8, n - 15 + i, bit(i))
        self._set_function(8, n - 8, True)  # the always-dark module

    def _draw_version(self):
        if self.version < 7:
            return
        rem = self.version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.version << 12 | rem
        for i in range(18):
            dark = (bits >> i) & 1 != 0
            a, b = self.size - 11 + i % 3, i // 3
            self._set_function(a, b, dark)
            self._set_function(b, a, dark)

    # -- data
    def _add_ecc_and_interleave(self, data):
        ver, ecl = self.version, self.ecl
        num_blocks = _NUM_BLOCKS[ecl][ver]
        ecc_len = _ECC_PER_BLOCK[ecl][ver]
        raw = _raw_data_modules(ver) // 8
        num_short = num_blocks - raw % num_blocks
        short_len = raw // num_blocks
        divisor = _rs_divisor(ecc_len)
        blocks, k = [], 0
        for i in range(num_blocks):
            dat = list(data[k:k + short_len - ecc_len + (0 if i < num_short else 1)])
            k += len(dat)
            ecc = _rs_remainder(dat, divisor)
            if i < num_short:
                dat.append(0)
            blocks.append(dat + ecc)
        result = []
        for i in range(len(blocks[0])):
            for j, blk in enumerate(blocks):
                if i != short_len - ecc_len or j >= num_short:
                    result.append(blk[i])
        return result

    def _draw_codewords(self, data):
        n, i, total = self.size, 0, len(data) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.is_function[y][x] and i < total:
                        self.modules[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 != 0
                        i += 1
            right -= 2

    def _apply_mask(self, mask):
        f = _MASKS[mask]
        for y in range(self.size):
            row, fn = self.modules[y], self.is_function[y]
            for x in range(self.size):
                if not fn[x] and f(x, y):
                    row[x] = not row[x]

    # -- mask selection
    def _penalty(self):
        n, m = self.size, self.modules
        score = 0
        lines = m + [[m[y][x] for y in range(n)] for x in range(n)]
        finder = (True, False, True, True, True, False, True)
        for line in lines:
            run, prev = 0, None
            for v in line:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, v
            if run >= 5:
                score += run - 2
            # 1:1:3:1:1 finder-like pattern with 4 light modules on one side
            padded = [False] * 4 + line + [False] * 4
            for i in range(4, len(padded) - 10):
                if tuple(padded[i:i + 7]) == finder:
                    if not any(padded[i - 4:i]) or not any(padded[i + 7:i + 11]):
                        score += 40
        for y in range(n - 1):
            for x in range(n - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in m)
        total = n * n
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        score += max(0, k) * 10
        return score


def encode(text):
    """Return the smallest QR (as a QR object) holding text in byte mode."""
    data = text.encode('utf-8')
    for ver in range(1, MAX_VERSION + 1):
        count_bits = 8 if ver <= 9 else 16
        need = 4 + count_bits + 8 * len(data)
        if need <= _data_codewords(ver, 0) * 8:
            break
    else:
        raise ValueError('text too long for a QR code up to version %d' % MAX_VERSION)
    ecl = 1 if need <= _data_codewords(ver, 1) * 8 else 0  # use M when it fits for free
    cap = _data_codewords(ver, ecl) * 8
    bits = []

    def put(value, length):
        bits.extend((value >> i) & 1 for i in reversed(range(length)))

    put(0b0100, 4)
    put(len(data), count_bits)
    for b in data:
        put(b, 8)
    put(0, min(4, cap - len(bits)))
    put(0, -len(bits) % 8)
    codewords = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad_byte = 0xEC
    while len(codewords) < cap // 8:
        codewords.append(pad_byte)
        pad_byte ^= 0xEC ^ 0x11
    return QR(ver, ecl, codewords)
