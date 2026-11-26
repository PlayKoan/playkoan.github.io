"""Drawing a puzzle from (tier, label): the hidden rule, the trap and the starting koans (SPEC 5.4, 6.1)."""

import collections
import re

from .atoms import DRAWABLE, atom_mask, atom_text
from .errors import InternalError
from .prng import stream
from .tiers import FLOOR, OPEN_TIER, Entry, get_lists, trap_pool
from .universe import FULL, LE4, S_MASK, U, ones, w4

MAX_ATTEMPTS = 1000
MAX_OPEN_ATTEMPTS = 2000

Puzzle = collections.namedtuple("Puzzle", "tier label secret yes no trap attempts n_fit")

ORDINARY_LABEL = re.compile(r"\A([1-5])-[0-9]{5}\Z")
DAILY_LABEL = re.compile(r"\Adaily-\d{4}-\d{2}-\d{2}\Z")


OPEN_GAP = 205      # the one tier-5 threshold, in w4 weights (1024 in all): 20 percent


def _single_masks():
    """(text, mask) of every drawable condition and of its negation: the single conditions,
    which are exactly the masks of the tier-3 list plus the negations it leaves out."""
    out = []
    for d in DRAWABLE:
        m = atom_mask(d.atom)
        out += [(atom_text(d.atom), m), (d.neg_text or "not " + atom_text(d.atom), FULL ^ m)]
    return out


def _nearest(m, singles=None):
    """(d, other, text): the single condition nearest to mask m, d the w4 weight of the koans on
    which they differ; other is False when it has m's answers on all 5460 koans. Ties go to the
    one with the same answers, then to the first text in ASCII order."""
    return min((w4(m ^ s), m != s, text) for text, s in singles or _single_masks())


def open_problem(m, singles=None):
    """Why a two-part rule with mask m cannot be drawn in tier 5, or None if it can. Weights are
    those of w4 (a koan of length 1 weighs 64, of length 4 weighs 1, all of them 1024):

    ("band", w): its YES koans weigh w, less than OPEN_GAP or more than 1024 - OPEN_GAP;
    ("same", text): it has the answers of the single condition text on all 5460 koans;
    ("agrees", text): it agrees with the single condition text on every koan of length 1-4;
    ("near", text, d): it differs from the single condition text on koans weighing d in all,
    0 < d < OPEN_GAP;
    ("start", None): fewer than 2 YES or 2 NO among koans of length 2-4 (never seen in practice).
    Which problem is named never changes whether there is one, so the draw does not depend on it.
    """
    w = w4(m)
    if not OPEN_GAP <= w <= 1024 - OPEN_GAP:
        return ("band", w)
    d, other, text = _nearest(m, singles)
    if d == 0:
        return ("agrees", text) if other else ("same", text)
    if d < OPEN_GAP:
        return ("near", text, d)
    if (m & S_MASK).bit_count() < 2 or ((FULL ^ m) & S_MASK).bit_count() < 2:
        return ("start", None)
    return None


def open_drawable(m):
    """Could tier 5 draw a rule with mask m? ("single", text), ("no", problem), or
    ("and"|"or", a, b, w, d, near): the two halves, the weight w of its YES koans, and the
    single condition near nearest to it, d away (the numbers of the ask reply, round 6)."""
    lits = get_lists()[3]
    for e in lits:
        if e.mask == m:
            return ("single", e.text)
    problem = open_problem(m)
    if problem:
        return ("no", problem)
    for op, cand in (("and", [e for e in lits if e.mask & m == m]),
                     ("or", [e for e in lits if e.mask | m == m])):
        for i, a in enumerate(cand):
            for b in cand[i + 1:]:
                if ((a.mask & b.mask) if op == "and" else (a.mask | b.mask)) == m:
                    d, _, near = _nearest(m)
                    return (op, a.text, b.text, w4(m), d, near)
    return ("no", ("form", None))


def draw_open_rule(label):
    """Tier 5: one tier-3 condition (1 in 4), two joined by and (1 in 2) or by or (1 in 4)."""
    r = stream(label, "rule")
    lits = get_lists()[3]
    kind = r.below(4)
    if kind == 0:
        return Entry(lits[r.below(len(lits))].text, "open", True)
    op = "and" if kind <= 2 else "or"
    singles = _single_masks()
    for _ in range(MAX_OPEN_ATTEMPTS):
        a = lits[r.below(len(lits))]
        b = lits[r.below(len(lits))]
        if b is a:
            continue
        m = (a.mask & b.mask) if op == "and" else (a.mask | b.mask)
        if open_problem(m, singles) is None:
            return Entry("%s %s %s" % (a.text, op, b.text), "open", True)
    raise InternalError("no open rule found")


def draw_rule(tier, label):
    if label.startswith("D-"):                    # a dealt puzzle (round 7): the code is the rule
        from .deal import decode
        return Entry(decode(label)[0], "open", True)
    if tier == OPEN_TIER:
        return draw_open_rule(label)
    r = stream(label, "rule")
    rows = get_lists()[tier]
    if tier >= 2:
        b = r.below(4)
        rows = [e for e in rows if e.new == (b < 3)]          # 75% new material
    fams = sorted(set(e.family for e in rows))
    fam = fams[r.below(len(fams))]                           # family-uniform
    members = [e for e in rows if e.family == fam]
    return members[r.below(len(members))]


def fits(m, yes, no):
    """Does a rule with mask m agree with the starting koans?"""
    return all((m >> q) & 1 for q in yes) and not any((m >> q) & 1 for q in no)


def eligible_traps(tier, t):
    """Trap candidates for a secret mask t: the narrower ones if any, else those 10% away."""
    feasible = [T for T in trap_pool(tier)
                if T.mask != t
                and (t & T.mask & S_MASK).bit_count() >= 2
                and ((FULL ^ (t | T.mask)) & S_MASK).bit_count() >= 2]
    narrow = [T for T in feasible if (T.mask & LE4) & (FULL ^ t) == 0]
    if narrow:
        return narrow
    return [T for T in feasible if w4(T.mask ^ t) >= 103]


def pick_pair(rng, cand):
    a = cand[rng.below(len(cand))]
    rest = [q for q in cand if q != a]
    b = rest[rng.below(len(rest))]
    return (min(a, b), max(a, b))


def starters(tier, label, t):
    """(yes, no, trap, attempts, n_fit) for secret mask t (SPEC 6.1)."""
    rng = stream(label, "start")
    # tier 5 has no list: n_fit counts the tier-3 single conditions that fit (information only)
    masks = [e.mask for e in get_lists()[3 if tier == OPEN_TIER else tier]]
    traps = None
    if tier >= 2:
        traps = eligible_traps(tier, t)
        if not traps:
            raise InternalError("no eligible trap")
    best = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if traps is None:
            trap = None
            yes = pick_pair(rng, ones(t & S_MASK))
            no = pick_pair(rng, ones((FULL ^ t) & S_MASK))
        else:
            trap = traps[rng.below(len(traps))]
            yes = pick_pair(rng, ones(t & trap.mask & S_MASK))
            no = pick_pair(rng, ones((FULL ^ (t | trap.mask)) & S_MASK))
        n_fit = sum(1 for m in masks if fits(m, yes, no))
        if n_fit >= FLOOR[tier]:
            return yes, no, trap, attempt, n_fit
        if best is None or n_fit > best[4]:
            best = (yes, no, trap, attempt, n_fit)
    return best   # unreachable in practice (SPEC 6.1 measurements)


def label_tier(label):
    """Tier encoded by a label, or None if the label is malformed."""
    m = ORDINARY_LABEL.match(label)
    if m:
        return int(m.group(1))
    if DAILY_LABEL.match(label):
        return 3
    if label.startswith("D-"):
        from .deal import decode
        return OPEN_TIER if decode(label) is not None else None
    return None


def make_puzzle(tier, label):
    if label.startswith("D-"):
        from .deal import make_dealt_puzzle
        return make_dealt_puzzle(label)
    secret = draw_rule(tier, label)
    yes, no, trap, attempts, n_fit = starters(tier, label, secret.mask)
    return Puzzle(tier, label, secret, yes, no, trap, attempts, n_fit)


def koan_of(i):
    return U[i]
