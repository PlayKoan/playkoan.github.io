"""The dealer's chair (round 7): deal codes, the puzzle a code stands for, and the deal records.

A code is 80 bits in Crockford base32, D-XXXX-XXXX-XXXX-XXXX: a version, the rule (as one tier-3
condition, or two joined by and / or), the four start koans and a check, mixed so that similar
deals look unrelated. It is not a secret, only not readable at a glance.
"""

import json
import os
import re
import zlib
from pathlib import Path

from .puzzle import Puzzle, open_problem
from .tiers import OPEN_TIER, Entry, get_lists
from .universe import N, S_MASK

CODE_VERSION = 1
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"          # Crockford: no I L O U
DEAL_LABEL = re.compile(r"\AD(?:-[0-9A-HJKMNP-TV-Z]{4}){4}\Z")
KINDS = ("single", "and", "or")
BITS = 80
_M = (1 << BITS) - 1
_K = 0x9E3779B97F4A7C15F39D        # odd, so multiplying by it is undone by its inverse mod 2^80
_K_INV = pow(_K, -1, 1 << BITS)
_SHIFT = 37
DEALS_NAME = "deals.json"
MAX_DEALS = 50


def _mix(x):
    for _ in range(3):
        x = (x * _K) & _M
        x ^= x >> _SHIFT
    return x


def _unmix(x):
    for _ in range(3):
        x ^= (x >> _SHIFT) ^ (x >> 2 * _SHIFT)
        x = (x * _K_INV) & _M
    return x


def encode(kind, a, b, koans):
    """The code of a deal: kind in KINDS, a and b indices in the tier-3 list (a == b for a
    single), koans the four start koans (indices in U)."""
    v = CODE_VERSION
    for field, bits in ((KINDS.index(kind), 2), (a, 7), (b, 7)):
        v = (v << bits) | field
    for q in sorted(koans):
        v = (v << 13) | q
    x = _mix((v << 8) | (zlib.crc32(v.to_bytes(9, "big")) & 0xFF))
    chars = "".join(ALPHABET[(x >> (5 * i)) & 31] for i in range(15, -1, -1))
    return "D-" + "-".join(chars[i:i + 4] for i in range(0, 16, 4))


def normalize(text):
    """The canonical label of a code as typed (any case, dashes optional), or None."""
    s = text.strip().upper().replace("-", "")
    s = s.translate(str.maketrans("OIL", "011"))
    if len(s) == 17 and s[0] == "D":
        s = s[1:]
    if len(s) != 16 or any(c not in ALPHABET for c in s):
        return None
    return "D-" + "-".join(s[i:i + 4] for i in range(0, 16, 4))


def decode(label):
    """(rule text, mask, yes, no) of a canonical code label, or None if it is not a deal."""
    if not DEAL_LABEL.match(label):
        return None
    x = 0
    for c in label[2:].replace("-", ""):
        x = (x << 5) | ALPHABET.index(c)
    x = _unmix(x)
    v, check = x >> 8, x & 0xFF
    if zlib.crc32(v.to_bytes(9, "big")) & 0xFF != check:
        return None
    koans = [(v >> (13 * i)) & 0x1FFF for i in range(3, -1, -1)]
    v >>= 52
    b, a, kind, version = v & 127, (v >> 7) & 127, (v >> 14) & 3, v >> 16
    lits = get_lists()[3]
    if version != CODE_VERSION or kind > 2 or a >= len(lits) or b >= len(lits):
        return None
    if any(q >= N for q in koans) or len(set(koans)) != 4 or koans != sorted(koans):
        return None
    if KINDS[kind] == "single":
        if a != b:
            return None
        text, mask = lits[a].text, lits[a].mask
    else:
        if a >= b:
            return None
        op = KINDS[kind]
        mask = (lits[a].mask & lits[b].mask) if op == "and" else (lits[a].mask | lits[b].mask)
        if open_problem(mask) is not None:
            return None
        text = "%s %s %s" % (lits[a].text, op, lits[b].text)
    yes = [q for q in koans if (mask >> q) & 1]
    no = [q for q in koans if not (mask >> q) & 1]
    if len(yes) != 2:
        return None
    return text, mask, tuple(yes), tuple(no)


def make_dealt_puzzle(label):
    """The tier-5 puzzle a code stands for: no trap (the dealer chose the start)."""
    text, mask, yes, no = decode(label)
    n_fit = sum(1 for e in get_lists()[3] if all((e.mask >> q) & 1 for q in yes)
                and not any((e.mask >> q) & 1 for q in no))
    return Puzzle(OPEN_TIER, label, Entry(text, "open", True), yes, no, None, 0, n_fit)


def is_dealt(label):
    return label.startswith("D-")


def start_lengths_ok(koans):
    """The game's own starts use koans of length 2 to 4; a dealer may use any."""
    return all((S_MASK >> q) & 1 for q in koans)


# ---------------------------------------------------------------- the dealer's records
def _deal_ok(d):
    return (isinstance(d, dict) and set(d) == {"code", "score", "dealt"}
            and isinstance(d["code"], str) and decode(d["code"]) is not None
            and type(d["score"]) is int and d["score"] >= 0 and isinstance(d["dealt"], str))


def _deals_ok(obj):
    # total and best (round 7b) count every deal ever, while deals keeps only the last 50
    if not isinstance(obj, dict) or not {"schema", "deals"} <= set(obj) <= \
            {"schema", "deals", "total", "best"} or obj["schema"] != 1:
        return False
    deals = obj["deals"]
    return (isinstance(deals, list) and len(deals) <= MAX_DEALS and all(map(_deal_ok, deals))
            and (type(obj.get("total", 0)) is int and obj.get("total", 0) >= 0)
            and (obj.get("best") is None or _deal_ok(obj["best"])))


def load_deals(directory):
    """{"deals": the last 50 deals (oldest first), "total": deals ever dealt, "best": the first
    deal with the highest score or None}; None if deals.json is unreadable."""
    path = Path(directory) / DEALS_NAME
    obj = {"schema": 1, "deals": []}
    if path.exists():
        try:
            obj = json.loads(path.read_text(encoding="ascii"))
        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
            return None
        if not _deals_ok(obj):
            return None
    deals = obj["deals"]
    best = obj.get("best") or (max(deals, key=lambda d: d["score"]) if deals else None)
    return {"deals": deals, "total": max(obj.get("total", 0), len(deals)), "best": best}


def record_deal(directory, code, score, dealt):
    """Add a deal: a code dealt again replaces its old entry and is not counted twice. The
    records afterwards, with "again" (dealt before) and "prev_best" (the best score before
    this deal, or None), or None when deals.json is unreadable (it is then left as it is)."""
    rec = load_deals(directory)
    if rec is None:
        return None
    again = any(d["code"] == code for d in rec["deals"])
    entry = {"code": code, "score": score, "dealt": dealt}
    deals = ([d for d in rec["deals"] if d["code"] != code] + [entry])[-MAX_DEALS:]
    best = rec["best"]
    prev = best["score"] if best else None
    if best is None or score > best["score"]:
        best = entry
    total = rec["total"] + (0 if again else 1)
    path = Path(directory) / DEALS_NAME
    tmp = Path(directory) / ("%s.%d.tmp" % (DEALS_NAME, os.getpid()))
    with open(tmp, "w", encoding="ascii", newline="\n") as f:
        f.write(json.dumps({"schema": 1, "deals": deals, "total": total, "best": best},
                           indent=1, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return {"deals": deals, "total": total, "best": best, "again": again, "prev_best": prev}
