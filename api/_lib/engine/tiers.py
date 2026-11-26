"""The hidden-rule lists H1-H4, par, and the fairness properties they satisfy (SPEC 5)."""

import hashlib
import itertools

from .atoms import DRAWABLE, all_conditions, atom_mask, atom_text
from .errors import InternalError
from .lang import literal_mask, parse_canonical, rule_mask
from .universe import DIGITS, FULL, LE4, S_MASK, w4

OPEN_TIER = 5       # the hidden rule is drawn from an unpublished space, not from a list
TIERS = (1, 2, 3, 4, 5)
PAR = {1: 3, 2: 3, 3: 4, 4: 5, 5: 9}    # playtests rounds 1-3; tier 5 from rounds 4-5 (round 6)
FLOOR = {1: 3, 2: 4, 3: 4, 4: 4, 5: 0}
LIST_SHA256 = "165d2985800f426a98f250fd45daa60ee3efd9109eb1a127d8a88e007d2ae518"
SIZES = {1: 19, 2: 62, 3: 106, 4: 146}


class Entry:
    """One hidden rule: canonical text, family, whether it is new in its tier, AST, mask."""

    __slots__ = ("text", "family", "new", "rule", "_mask")

    def __init__(self, text, family, new):
        self.text = text
        self.family = family
        self.new = new
        self.rule = parse_canonical(text)
        self._mask = None

    @property
    def mask(self):
        if self._mask is None:
            self._mask = rule_mask(self.rule)
        return self._mask

    def __repr__(self):
        return "Entry(%r)" % self.text


def _shape_rows():
    rows = []
    for d in DIGITS:
        rows.append(("starts %d or ends %d" % (d, d), "start or end"))
        rows.append(("not starts %d and not ends %d" % (d, d), "start or end"))
    for d, e in itertools.combinations(DIGITS, 2):
        rows.append(("has %d and has %d" % (d, e), "both digits"))
        rows.append(("not has %d or not has %d" % (d, e), "both digits"))
    for d, e in itertools.combinations(DIGITS, 2):
        rows.append(("starts %d or starts %d" % (d, e), "start set"))
    for d, e in itertools.combinations(DIGITS, 2):
        rows.append(("ends %d or ends %d" % (d, e), "end set"))
    rows += [("nondecreasing or nonincreasing", "monotone"),
             ("not nondecreasing and not nonincreasing", "monotone"),
             ("first is max or last is max", "peak at an end"),
             ("not first is max and not last is max", "peak at an end"),
             ("first is min or last is min", "trough at an end"),
             ("not first is min and not last is min", "trough at an end"),
             ("not adjacent repeat and not jump", "steps of one"),
             ("adjacent repeat or jump", "steps of one")]
    return rows


def _literal_rows(top):
    """(text, family, gen tier) for tiers 2-3: positives in declared order, then negations."""
    pos = [(atom_text(d.atom), d.family, d.gen_tier) for d in DRAWABLE if d.gen_tier <= top]
    neg = [(d.neg_text, d.family, d.gen_tier) for d in DRAWABLE if d.gen_tier <= top and d.gen_not]
    return pos + neg


def list_texts():
    """{tier: [(text, family, new)]} in the exact order of SPEC 5.2 (no parsing)."""
    out = {1: [(atom_text(d.atom), d.family, True) for d in DRAWABLE if d.gen_tier == 1]}
    for tier in (2, 3):
        out[tier] = [(t, f, g == tier) for t, f, g in _literal_rows(tier)]
    out[4] = [(t, f, True) for t, f in _shape_rows()] + [(t, f, False) for t, f, _ in out[3]]
    return out


def list_digest(texts):
    h = hashlib.sha256()
    for tier in (1, 2, 3, 4):
        for text, family, new in texts[tier]:
            h.update(("%d|%s|%s|%d\n" % (tier, text, family, 1 if new else 0)).encode("ascii"))
    return h.hexdigest()


_LISTS = None


def get_lists():
    """{tier: [Entry]}; built on first use and checked against LIST_SHA256 (SPEC 5.2.5)."""
    global _LISTS
    if _LISTS is None:
        texts = list_texts()
        if list_digest(texts) != LIST_SHA256:
            raise InternalError("hidden-rule lists do not match LIST_SHA256")
        lists = {tier: [Entry(t, f, n) for t, f, n in texts[tier]] for tier in texts}
        for tier, size in SIZES.items():
            if len(lists[tier]) != size:
                raise InternalError("list size")
        _LISTS = lists
    return _LISTS


def trap_pool(tier):
    """Positive literals of drawable atoms from earlier tiers, in declared order (SPEC 6.1)."""
    return [Entry(atom_text(d.atom), d.family, False) for d in DRAWABLE if d.gen_tier < tier]


def verify_lists():
    """Check F1-F5 of SPEC 5.3 for every entry. Returns [] or a list of 'Fx tier' failures."""
    lists = get_lists()
    failures = []
    literal_masks = set()
    for atom in all_conditions():
        m = atom_mask(atom)
        literal_masks.add(m)
        literal_masks.add(FULL ^ m)
    for tier in (1, 2, 3, 4):
        entries = lists[tier]
        bad = set()
        for e in entries:
            if not 103 <= w4(e.mask) <= 921:
                bad.add("F1")
            if (e.mask & S_MASK).bit_count() < 2 or ((FULL ^ e.mask) & S_MASK).bit_count() < 2:
                bad.add("F2")
            for lm in literal_masks:
                if lm != e.mask and (lm ^ e.mask) & LE4 == 0:
                    bad.add("F4")
                    break
            op, lits = e.rule
            if op != "single":
                for lit in lits:
                    if w4(e.mask ^ literal_mask(lit)) * 100 < 10 * 1024:
                        bad.add("F5")
        for a, b in itertools.combinations(entries, 2):
            if a.mask == b.mask or w4(a.mask ^ b.mask) * 100 < 4 * 1024:
                bad.add("F3")
        failures += ["%s %d" % (f, tier) for f in sorted(bad)]
    return failures
