"""The 28 condition templates (SPEC 3): predicates, canonical text, glosses, masks.

An atom is a tuple (kind, args):
    ("has", (D,)), ("starts", (D,)), ("ends", (D,)), ("same ends", ()), ... nullary kinds ...,
    ("more", (D, E)), ("as many", (D, E)),
    ("count", (D, CMP, K or None)), ("length", (CMP, K or None)), ("kinds", ...), ("sum", ...).
"""

import collections
import itertools

from .universe import DIGITS, mask_of

QUANTS = ("count", "length", "kinds", "sum")
CMPS = ("atleast", "atmost", "exactly", "even", "odd")
KMAX = {"count": 6, "length": 6, "kinds": 4, "sum": 24}

DIGIT_KINDS = ("has", "starts", "ends")
PAIR_KINDS = ("more", "as many")
NULLARY = (
    "same ends", "palindrome", "nondecreasing", "nonincreasing", "strictly increasing",
    "strictly decreasing", "adjacent repeat", "all different", "jump", "zigzag", "grouped",
    "first is max", "first is min", "last is max", "last is min", "max once", "min once",
    "last above first", "last below first",
)


def grouped(t):
    """Each digit that occurs forms one unbroken block."""
    seen = set()
    prev = None
    for x in t:
        if x != prev:
            if x in seen:
                return False
            seen.add(x)
        prev = x
    return True


def zigzag(t):
    """No equal neighbours, and the direction alternates at every step."""
    if any(a == b for a, b in zip(t, t[1:])):
        return False
    ups = [b > a for a, b in zip(t, t[1:])]
    return all(x != y for x, y in zip(ups, ups[1:]))


_NULLARY_PRED = {
    "same ends": lambda t: t[0] == t[-1],
    "palindrome": lambda t: t == t[::-1],
    "nondecreasing": lambda t: all(a <= b for a, b in zip(t, t[1:])),
    "nonincreasing": lambda t: all(a >= b for a, b in zip(t, t[1:])),
    "strictly increasing": lambda t: all(a < b for a, b in zip(t, t[1:])),
    "strictly decreasing": lambda t: all(a > b for a, b in zip(t, t[1:])),
    "adjacent repeat": lambda t: any(a == b for a, b in zip(t, t[1:])),
    "all different": lambda t: len(set(t)) == len(t),
    "jump": lambda t: any(abs(a - b) >= 2 for a, b in zip(t, t[1:])),
    "zigzag": zigzag,
    "grouped": grouped,
    "first is max": lambda t: t[0] == max(t),
    "first is min": lambda t: t[0] == min(t),
    "last is max": lambda t: t[-1] == max(t),
    "last is min": lambda t: t[-1] == min(t),
    "max once": lambda t: t.count(max(t)) == 1,
    "min once": lambda t: t.count(min(t)) == 1,
    "last above first": lambda t: t[-1] > t[0],
    "last below first": lambda t: t[-1] < t[0],
}


def _quantity(kind, args, t):
    if kind == "count":
        return t.count(args[0])
    if kind == "length":
        return len(t)
    if kind == "kinds":
        return len(set(t))
    return sum(t)


def _compare(cmp, k, v):
    if cmp == "atleast":
        return v >= k
    if cmp == "atmost":
        return v <= k
    if cmp == "exactly":
        return v == k
    if cmp == "even":
        return v % 2 == 0
    return v % 2 == 1


def holds_atom(atom, t):
    """Does koan t (a tuple) satisfy the atom? The definitions of SPEC 3.2, literally."""
    kind, args = atom
    if kind == "has":
        return args[0] in t
    if kind == "starts":
        return t[0] == args[0]
    if kind == "ends":
        return t[-1] == args[0]
    if kind == "more":
        return t.count(args[0]) > t.count(args[1])
    if kind == "as many":
        return t.count(args[0]) == t.count(args[1])
    if kind in QUANTS:
        cmp, k = args[-2], args[-1]
        return _compare(cmp, k, _quantity(kind, args, t))
    return _NULLARY_PRED[kind](t)


def _cmp_text(cmp, k):
    return cmp if k is None else "%s %d" % (cmp, k)


def atom_text(atom):
    """Canonical text (what PARSED prints)."""
    kind, args = atom
    if kind in DIGIT_KINDS:
        return "%s %d" % (kind, args[0])
    if kind == "more":
        return "more %d than %d" % args
    if kind == "as many":
        return "as many %d as %d" % args
    if kind == "count":
        return "count %d %s" % (args[0], _cmp_text(args[1], args[2]))
    if kind in QUANTS:
        return "%s %s" % (kind, _cmp_text(args[0], args[1]))
    return kind


_GLOSS = {
    "nondecreasing": "no digit is smaller than the one before (ties allowed)",
    "nonincreasing": "no digit is larger than the one before (ties allowed)",
    "palindrome": "reads the same backwards",
    "adjacent repeat": "two equal digits side by side",
    "strictly increasing": "each digit is larger than the one before (no ties)",
    "strictly decreasing": "each digit is smaller than the one before (no ties)",
    "same ends": "first digit equals last digit",
    "jump": "two neighbouring digits differ by 2 or more",
    "all different": "no digit appears twice",
    "last above first": "last digit is larger than the first",
    "last below first": "last digit is smaller than the first",
    "first is max": "no digit is larger than the first",
    "first is min": "no digit is smaller than the first",
    "last is max": "no digit is larger than the last",
    "last is min": "no digit is smaller than the last",
    "max once": "the largest digit appears exactly once",
    "min once": "the smallest digit appears exactly once",
    "grouped": "each digit that appears forms one unbroken block",
    "zigzag": "no equal neighbours and the steps alternate up and down",
}
# `not X` reads NOT (gloss of X), except these, which have a positive wording (round 5; the
# extremes in round 6, where NOT (no digit is larger than the first) was a double negative;
# all different and zigzag after the round-6 review, the last two NOT (no ...))
_NOT_GLOSS = {
    "nondecreasing": "some digit is smaller than the one before",
    "nonincreasing": "some digit is larger than the one before",
    "first is max": "some digit is larger than the first",
    "first is min": "some digit is smaller than the first",
    "last is max": "some digit is larger than the last",
    "last is min": "some digit is smaller than the last",
    "all different": "some digit appears twice",
    "zigzag": "two neighbouring digits are equal or two steps in a row both go up or both go "
              "down",
}
_CMP_GLOSS = {"atleast": "is at least %d", "atmost": "is at most %d", "exactly": "is exactly %d",
              "even": "is even", "odd": "is odd"}
_QUANT_GLOSS = {"length": "number of digits", "kinds": "number of different digits",
                "sum": "digit sum"}


def atom_gloss(atom):
    """Plain-English meaning (the MEANS line), SPEC 3.6."""
    kind, args = atom
    if kind == "has":
        return "contains a %d" % args[0]
    if kind == "starts":
        return "first digit is %d" % args[0]
    if kind == "ends":
        return "last digit is %d" % args[0]
    if kind == "more":
        return "more %ds than %ds" % args
    if kind == "as many":
        return "as many %ds as %ds (zero of each counts)" % args
    if kind in QUANTS:
        cmp, k = args[-2], args[-1]
        cmp_gloss = _CMP_GLOSS[cmp] % k if k is not None else _CMP_GLOSS[cmp]
        head = "number of %ds" % args[0] if kind == "count" else _QUANT_GLOSS[kind]
        if kind == "count" and cmp == "even":
            cmp_gloss += " (0 is even)"       # only a count can be 0
        return "%s %s" % (head, cmp_gloss)
    return _GLOSS[kind]


def not_gloss(atom):
    """The positive gloss of `not` + atom, or None when it is written NOT (gloss)."""
    return _NOT_GLOSS.get(atom[0])


_MASKS = {}


def atom_mask(atom):
    """Truth table over all 5460 koans, memoised for the life of the process."""
    m = _MASKS.get(atom)
    if m is None:
        m = mask_of(lambda t: holds_atom(atom, t))
        _MASKS[atom] = m
    return m


def all_conditions():
    """Every distinct condition of the guess language (258; `as many` is symmetric)."""
    out = []
    for kind in DIGIT_KINDS:
        out += [(kind, (d,)) for d in DIGITS]
    out += [(kind, ()) for kind in NULLARY]
    out += [("more", (d, e)) for d, e in itertools.permutations(DIGITS, 2)]
    out += [("as many", (d, e)) for d, e in itertools.combinations(DIGITS, 2)]
    for kind in QUANTS:
        heads = [(d,) for d in DIGITS] if kind == "count" else [()]
        for head in heads:
            for cmp in ("atleast", "atmost", "exactly"):
                out += [(kind, head + (cmp, k)) for k in range(KMAX[kind] + 1)]
            out += [(kind, head + ("even", None)), (kind, head + ("odd", None))]
    return out


# ---------------------------------------------------------------- the 62 drawable atoms (3.5)
Drawable = collections.namedtuple("Drawable", "atom family gen_tier gen_not neg_text")


def _drawables():
    rows = []

    def add(atom, family, tier, gen_not, neg_text=None):
        if gen_not and neg_text is None:
            neg_text = "not " + atom_text(atom)
        rows.append(Drawable(atom, family, tier, gen_not, neg_text if gen_not else None))

    for d in DIGITS:
        add(("has", (d,)), "presence", 1, True)
    for d in DIGITS:
        add(("starts", (d,)), "position", 1, True)
    for d in DIGITS:
        add(("ends", (d,)), "position", 1, True)
    add(("length", ("atleast", 3)), "length", 1, True, "length atmost 2")
    add(("length", ("even", None)), "length", 1, True, "length odd")
    add(("sum", ("even", None)), "parity", 1, True, "sum odd")
    add(("nondecreasing", ()), "order", 1, True)
    add(("nonincreasing", ()), "order", 1, True)
    add(("palindrome", ()), "symmetry", 1, True)
    add(("adjacent repeat", ()), "adjacency", 1, True)
    add(("strictly increasing", ()), "order", 2, True)
    add(("strictly decreasing", ()), "order", 2, True)
    add(("same ends", ()), "symmetry", 2, True)
    add(("jump", ()), "adjacency", 2, True)
    add(("all different", ()), "repetition", 2, True)
    for d in DIGITS:
        add(("count", (d, "atleast", 2)), "count", 2, True, "count %d atmost 1" % d)
    for d in DIGITS:
        add(("count", (d, "exactly", 1)), "count", 2, False)
    add(("kinds", ("exactly", 2)), "count", 2, False)
    add(("kinds", ("exactly", 3)), "count", 2, False)
    add(("last above first", ()), "ends relation", 3, True)
    add(("last below first", ()), "ends relation", 3, True)
    for kind in ("first is max", "first is min", "last is max", "last is min", "max once",
                 "min once"):
        add((kind, ()), "extremes", 3, True)
    for d, e in itertools.permutations(DIGITS, 2):
        add(("more", (d, e)), "comparison", 3, False)
    for d, e in itertools.combinations(DIGITS, 2):
        add(("as many", (d, e)), "comparison", 3, True)
    add(("grouped", ()), "repetition", 3, True)
    add(("zigzag", ()), "adjacency", 3, True)
    return tuple(rows)


DRAWABLE = _drawables()
