"""SplitMix64 and seed derivation (SPEC 5.5). Stable across Python versions; `random` is never used."""

import hashlib

from . import GEN_VERSION
from .errors import InternalError

M64 = (1 << 64) - 1


class SplitMix64:
    def __init__(self, seed):
        self.s = seed & M64

    def next_u64(self):
        self.s = (self.s + 0x9E3779B97F4A7C15) & M64
        z = self.s
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & M64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & M64
        return z ^ (z >> 31)

    def below(self, n):
        """Unbiased integer in [0, n) by rejection sampling; below(1) still consumes a draw."""
        if n <= 0:
            raise InternalError("below(%d)" % n)
        limit = (1 << 64) - ((1 << 64) % n)
        while True:
            x = self.next_u64()
            if x < limit:
                return x % n


def seed_from(*parts):
    return int.from_bytes(hashlib.sha256("|".join(parts).encode("ascii")).digest()[:8], "big")


def stream(label, name):
    return SplitMix64(seed_from("KOAN", "v%d" % GEN_VERSION, label, name))
