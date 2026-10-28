"""A QR code encoder, small enough to read, for one job: the two-factor enrolment link.

Enrolling an authenticator app means getting a ``otpauth://`` URI from the screen into
a phone, and the only way anyone expects to do that is to point the camera at a square.
Every Python QR library is a dependency, and a dependency for one image on one page is
the kind of cost this package refuses elsewhere — so this module draws it instead.

The scope is deliberately narrow, which is what keeps it short enough to trust:

* **Byte mode only.** The URI is ASCII, and byte mode encodes any of it.
* **Error correction level M.** Fifteen per cent recovery is what every scanner
  expects of a code shown on a screen; there is no smudge for H to survive.
* **Versions 1 to 20.** An enrolment URI is around a hundred bytes. Version 20 holds
  666 at level M, so a project would need a very long site title to run out — and if
  it does, :func:`encode` raises and the page shows the key on its own, which is the
  fallback it already offers anyone whose camera does not cooperate.

The output is a matrix of booleans and :func:`svg` turns it into markup. Nothing here
reads user input beyond the bytes being encoded, and the SVG contains only numbers,
so it is safe to mark as such in a template.

The construction follows ISO/IEC 18004 and is checked in the test suite against
fingerprints of codes produced by an independent encoder at the same version and mask.
"""

from __future__ import annotations

from itertools import pairwise

__all__ = ["QRTooLong", "encode", "svg"]


class QRTooLong(ValueError):
    """The data does not fit in the largest version this encoder supports."""


# ── tables ──────────────────────────────────────────────────────────────────

#: Level M only: version -> (EC codewords per block, [(block count, data codewords)]).
_BLOCKS: dict[int, tuple[int, list[tuple[int, int]]]] = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
    11: (30, [(1, 50), (4, 51)]),
    12: (22, [(6, 36), (2, 37)]),
    13: (22, [(8, 37), (1, 38)]),
    14: (24, [(4, 40), (5, 41)]),
    15: (24, [(5, 41), (5, 42)]),
    16: (28, [(7, 45), (3, 46)]),
    17: (28, [(10, 46), (1, 47)]),
    18: (26, [(9, 43), (4, 44)]),
    19: (26, [(3, 44), (11, 45)]),
    20: (26, [(3, 41), (13, 42)]),
}

#: The two format-information bits for level M.
_LEVEL_M = 0b00


def _data_capacity(version: int) -> int:
    return sum(count * size for count, size in _BLOCKS[version][1])


# ── Reed-Solomon over GF(256) ───────────────────────────────────────────────


def _gf_mul(x: int, y: int) -> int:
    """Multiply in GF(2^8) modulo the QR polynomial x^8 + x^4 + x^3 + x^2 + 1."""
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coefficient in enumerate(divisor):
            result[i] ^= _gf_mul(coefficient, factor)
    return result


# ── codewords ───────────────────────────────────────────────────────────────


def _pick_version(length: int) -> int:
    for version in _BLOCKS:
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * length <= 8 * _data_capacity(version):
            return version
    raise QRTooLong(f"{length} bytes do not fit in a version-20 QR code at level M.")


def _codewords(data: bytes, version: int) -> list[int]:
    """The data stream, padded, split into blocks, error-corrected and interleaved."""
    count_bits = 8 if version < 10 else 16
    bits: list[int] = []

    def put(value: int, width: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(width)))

    put(0b0100, 4)  # byte mode
    put(len(data), count_bits)
    for byte in data:
        put(byte, 8)

    capacity = 8 * _data_capacity(version)
    put(0, min(4, capacity - len(bits)))  # terminator
    put(0, -len(bits) % 8)
    pad = 0xEC
    while len(bits) < capacity:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11

    stream = [int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)]

    ec_per_block, groups = _BLOCKS[version]
    divisor = _rs_divisor(ec_per_block)
    blocks: list[list[int]] = []
    eccs: list[list[int]] = []
    offset = 0
    for count, size in groups:
        for _ in range(count):
            block = stream[offset : offset + size]
            offset += size
            blocks.append(block)
            eccs.append(_rs_remainder(block, divisor))

    result: list[int] = []
    for i in range(max(len(block) for block in blocks)):
        result.extend(block[i] for block in blocks if i < len(block))
    for i in range(ec_per_block):
        result.extend(ecc[i] for ecc in eccs)
    return result


# ── the matrix ──────────────────────────────────────────────────────────────


def _alignment_positions(version: int) -> list[int]:
    if version == 1:
        return []
    count = version // 7 + 2
    size = version * 4 + 17
    step = (version * 8 + count * 3 + 5) // (count * 4 - 4) * 2
    return [6, *sorted(size - 7 - i * step for i in range(count - 1))]


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


class _Matrix:
    def __init__(self, version: int) -> None:
        self.version = version
        self.size = version * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def set(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.fixed[y][x] = True

    def draw_function_patterns(self) -> None:
        size = self.size
        for i in range(size):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        distance = max(abs(dx), abs(dy))
                        self.set(x, y, distance not in (2, 4))
        positions = _alignment_positions(self.version)
        last = len(positions) - 1
        for i, cx in enumerate(positions):
            for j, cy in enumerate(positions):
                # The three corners already hold finder patterns.
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)  # reserves the area; redrawn once the mask is known
        self.draw_version()

    def draw_format(self, mask: int) -> None:
        data = _LEVEL_M << 3 | mask
        remainder = data
        for _ in range(10):
            remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
        bits = (data << 10 | remainder) ^ 0x5412
        size = self.size

        def bit(i: int) -> bool:
            return (bits >> i) & 1 == 1

        for i in range(6):
            self.set(8, i, bit(i))
        self.set(8, 7, bit(6))
        self.set(8, 8, bit(7))
        self.set(7, 8, bit(8))
        for i in range(9, 15):
            self.set(14 - i, 8, bit(i))
        for i in range(8):
            self.set(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set(8, size - 15 + i, bit(i))
        self.set(8, size - 8, True)  # the dark module

    def draw_version(self) -> None:
        if self.version < 7:
            return
        remainder = self.version
        for _ in range(12):
            remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25)
        bits = self.version << 12 | remainder
        for i in range(18):
            dark = (bits >> i) & 1 == 1
            a, b = self.size - 11 + i % 3, i // 3
            self.set(a, b, dark)
            self.set(b, a, dark)

    def draw_codewords(self, codewords: list[int]) -> None:
        size = self.size
        i = 0
        total = len(codewords) * 8
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            upward = (right + 1) & 2 == 0
            for vertical in range(size):
                y = size - 1 - vertical if upward else vertical
                for x in (right, right - 1):
                    if not self.fixed[y][x] and i < total:
                        self.dark[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        test = _MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fixed[y][x] and test(x, y):
                    self.dark[y][x] = not self.dark[y][x]

    def penalty(self) -> int:
        """The four rules of ISO/IEC 18004 §7.8.3, simply counted."""
        size = self.size
        grid = self.dark
        score = 0
        lines = [list(row) for row in grid] + [list(col) for col in zip(*grid, strict=True)]
        finder = ([True, False, True, True, True, False, True], 4)
        for line in lines:
            run = 1
            for a, b in pairwise(line):
                if a == b:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run = 1
            if run >= 5:
                score += run - 2
            pattern, quiet = finder
            for start in range(size - 6):
                if line[start : start + 7] != pattern:
                    continue
                before = line[max(0, start - quiet) : start]
                after = line[start + 7 : start + 7 + quiet]
                if (len(before) == quiet and not any(before)) or (
                    len(after) == quiet and not any(after)
                ):
                    score += 40
        for y in range(size - 1):
            for x in range(size - 1):
                if grid[y][x] == grid[y][x + 1] == grid[y + 1][x] == grid[y + 1][x + 1]:
                    score += 3
        dark = sum(map(sum, grid))
        total = size * size
        score += abs(dark * 20 - total * 10) // total * 10
        return score


def encode(data: bytes | str, *, version: int | None = None, mask: int | None = None):
    """Return the QR code for ``data`` as rows of booleans, ``True`` meaning dark.

    ``version`` and ``mask`` exist for the tests, which pin both to compare against an
    independent encoder. Left alone, the smallest version that fits is used and the
    mask is the one with the lowest penalty, as the standard asks.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    if version is None:
        version = _pick_version(len(data))
    elif version not in _BLOCKS or version < _pick_version(len(data)):
        raise QRTooLong(f"{len(data)} bytes do not fit in a version-{version} QR code.")
    codewords = _codewords(data, version)

    def build(chosen: int) -> _Matrix:
        matrix = _Matrix(version)
        matrix.draw_function_patterns()
        matrix.draw_codewords(codewords)
        matrix.apply_mask(chosen)
        matrix.draw_format(chosen)
        return matrix

    if mask is None:
        best = min((build(m) for m in range(8)), key=lambda matrix: matrix.penalty())
    else:
        best = build(mask)
    return [list(row) for row in best.dark]


def svg(matrix, *, module: int = 4, quiet: int = 4, label: str = "") -> str:
    """The matrix as one ``<svg>``: a white square and one black path.

    Black on white whatever the theme, on purpose. Phone cameras read dark-on-light
    reliably and the inverted form unreliably, and a code that only scans in light
    mode is a support ticket from everyone who prefers dark mode.
    """
    size = len(matrix) + 2 * quiet
    parts = []
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                parts.append(f"M{x + quiet},{y + quiet}h1v1h-1z")
    pixels = size * module
    aria = f' role="img" aria-label="{_attr(label)}"' if label else ' aria-hidden="true"'
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'width="{pixels}" height="{pixels}" shape-rendering="crispEdges"{aria}>'
        f'<rect width="{size}" height="{size}" fill="#fff"/>'
        f'<path d="{"".join(parts)}" fill="#000"/></svg>'
    )


def _attr(value: str) -> str:
    from django.utils.html import escape

    return escape(value)
