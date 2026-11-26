"""The 5460 koans, their canonical order, and truth tables as int bitsets (SPEC 2)."""

import itertools

DIGITS = (1, 2, 3, 4)
U = tuple(t for L in range(1, 7) for t in itertools.product(DIGITS, repeat=L))
N = len(U)                                    # 5460
INDEX = {t: i for i, t in enumerate(U)}
OFF = {1: 0, 2: 4, 3: 20, 4: 84, 5: 340, 6: 1364}
FULL = (1 << N) - 1
SEG = {L: ((1 << 4 ** L) - 1) << OFF[L] for L in range(1, 7)}
LE4 = SEG[1] | SEG[2] | SEG[3] | SEG[4]       # koans of length 1-4 (indices 0..339)
S_MASK = SEG[2] | SEG[3] | SEG[4]             # koans of length 2-4 (indices 4..339)


def index(t):
    """Canonical index of a koan tuple, by formula (INDEX is the lookup table)."""
    return OFF[len(t)] + sum((d - 1) * 4 ** (len(t) - 1 - i) for i, d in enumerate(t))


def mask_of(pred):
    """Truth table of a predicate: bit i is set when pred(U[i]) is true."""
    return int("".join("1" if pred(t) else "0" for t in reversed(U)), 2)


def bit(m, i):
    return (m >> i) & 1


def ones(m):
    """Ascending indices of the set bits of m."""
    out = []
    while m:
        low = m & -m
        out.append(low.bit_length() - 1)
        m ^= low
    return out


def first_diff(a, b):
    """Shortest, then lexicographically first, koan where masks a and b differ; None if equal."""
    x = a ^ b
    if x == 0:
        return None
    return (x & -x).bit_length() - 1


def w4(m):
    """Length-weighted count over lengths 1-4 (each length weighs 256 in total; FULL gives 1024)."""
    return sum((m & SEG[L]).bit_count() << (2 * (4 - L)) for L in (1, 2, 3, 4))


def koan_text(t):
    """Display form: digits separated by single spaces, e.g. '2 4 4 1'."""
    return " ".join(str(d) for d in t)


def koan_str(t):
    """Save-file form: the digits run together, e.g. '2441'."""
    return "".join(str(d) for d in t)


def parse_digits(s):
    """Inverse of koan_str for trusted strings (save file); returns None if s is not a koan."""
    if not 1 <= len(s) <= 6 or any(c not in "1234" for c in s):
        return None
    return tuple(int(c) for c in s)
