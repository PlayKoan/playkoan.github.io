"""The apprentice: a tier-5 hunter that knows every rule tier 5 can draw and its chance (round 7).

It is the open tier's solver (the end card's `apprentice N`) and the dealer's opponent (`deal`).
"""

import collections

from .errors import InternalError
from .puzzle import _single_masks, open_problem
from .tiers import get_lists
from .universe import U, first_diff

POOL = 340          # koans of length 1-4 are indices 0..339
MAX_TESTS = 40
MAX_WRONG = 10

Rule = collections.namedtuple("Rule", "mask weight text kind a b")   # kind: single, and, or
Hunt = collections.namedtuple("Hunt", "steps tests wrong score fit")
# steps: (kind, text, q, answer, left, share): ("test", None, q, a, left, None), a wrong guess
# ("guess", text, counter q, its answer, left, share) and the solve ("guess", text, None, None, 1,
# share); left is the number of rules still in the running after the step, share the percent of
# their chances the guessed rule held. fit: the rules that fit the start (round 7b)


class Space:
    """The rules of tier 5 (distinct masks; a mask drawn under several wordings keeps the first
    and adds up their chances), with the columns and weight groups the hunt counts with."""

    def __init__(self):
        lits = get_lists()[3]
        singles = _single_masks()
        pairs = {"and": [], "or": []}
        for i, a in enumerate(lits):
            for j in range(i + 1, len(lits)):
                b = lits[j]
                for op, m in (("and", a.mask & b.mask), ("or", a.mask | b.mask)):
                    if open_problem(m, singles) is None:
                        pairs[op].append((m, i, j))
        n_s, n_a, n_o = len(lits), len(pairs["and"]), len(pairs["or"])
        # the draw's chances as integers: a single 1/4 over n_s, an `and` 1/2 over n_a, an
        # `or` 1/4 over n_o, all multiplied by 4 * n_s * n_a * n_o
        unit = {"single": n_a * n_o, "and": 2 * n_s * n_o, "or": n_s * n_a}
        found = {}
        rules = []
        self.wordings = {}      # mask -> every wording the draw can give it, the first one first

        def add(m, kind, i, j, text):
            k = found.get(m)
            self.wordings.setdefault(m, []).append(text)
            if k is None:
                found[m] = len(rules)
                rules.append(Rule(m, unit[kind], text, kind, i, j))
            else:
                rules[k] = rules[k]._replace(weight=rules[k].weight + unit[kind])

        for i, e in enumerate(lits):
            add(e.mask, "single", i, i, e.text)
        for op in ("and", "or"):
            for m, i, j in pairs[op]:
                add(m, op, i, j, "%s %s %s" % (lits[i].text, op, lits[j].text))
        self.rules = rules
        self.index = found
        self.counts = (n_s, n_a, n_o)
        self.cols = [0] * POOL
        groups = {}
        for h, r in enumerate(rules):
            bit = 1 << h
            groups[r.weight] = groups.get(r.weight, 0) | bit
            m = r.mask
            for q in range(POOL):
                if (m >> q) & 1:
                    self.cols[q] |= bit
        self.groups = sorted(groups.items(), reverse=True)     # (weight, rules), likeliest first

    def col(self, q):
        """The rules that say YES on koan q."""
        if q < POOL:
            return self.cols[q]
        v = 0
        for h, r in enumerate(self.rules):
            if (r.mask >> q) & 1:
                v |= 1 << h
        return v

    def weight(self, v):
        return sum(w * (v & g).bit_count() for w, g in self.groups)

    def likeliest(self, v):
        """(rule index, weight): the likeliest rule of v, the first one on a tie."""
        for w, g in self.groups:
            x = v & g
            if x:
                return (x & -x).bit_length() - 1, w
        raise InternalError("no rule is left")


_SPACE = []


def space():
    """The rule space, built once per process (about 0.8 s)."""
    if not _SPACE:
        _SPACE.append(Space())
    return _SPACE[0]


def nearby(rule, n=3):
    """Up to n fair rules joined like rule (and, or) that share one of its halves, for a deal
    refused as unfair; () when its halves are not both conditions of the tier 3 list."""
    op, lits = rule
    if op not in ("and", "or") or len(lits) != 2:
        return ()
    from .lang import literal_text
    texts = [e.text for e in get_lists()[3]]
    halves = {texts.index(literal_text(l)) for l in lits if literal_text(l) in texts}
    found = [r.text for r in space().rules if r.kind == op and {r.a, r.b} & halves]
    return tuple(found[:n])


def _split(sp, alive, board):
    """The koan whose answer splits the chances of the rules left most evenly."""
    total = sp.weight(alive)
    best, best_q = 0, None
    for q in range(POOL):
        if q in board:
            continue
        y = sp.weight(alive & sp.cols[q])
        s = min(y, total - y)
        if s > best:
            best, best_q = s, q
    if best_q is None:      # the rules left agree on every koan of length 1-4
        for q in range(POOL, len(U)):
            if q not in board and 0 < sp.weight(alive & sp.col(q)) < total:
                return q
        raise InternalError("the apprentice cannot split the rules left")
    return best_q


def hunt(secret, yes, no):
    """The apprentice's game against the rule with mask secret, from the start yes / no."""
    sp = space()
    if secret not in sp.index:
        raise InternalError("the apprentice does not know this rule")
    alive = (1 << len(sp.rules)) - 1
    board = set(yes) | set(no)
    for q in yes:
        alive &= sp.col(q)
    for q in no:
        alive &= ~sp.col(q)
    steps, tests, wrong = [], 0, 0
    fit = alive.bit_count()
    while True:
        h, w = sp.likeliest(alive)
        total = sp.weight(alive)
        share = 100 * w // total
        guess = alive & (alive - 1) == 0 or (wrong < MAX_WRONG and 2 * w >= total)
        if guess:
            rule = sp.rules[h]
            if rule.mask == secret:
                steps.append(("guess", rule.text, None, None, 1, share))
                return Hunt(steps, tests, wrong, tests + 3 * wrong, fit)
            wrong += 1
            q = first_diff(rule.mask, secret)      # the COUNTER koan a player would get
        else:
            if tests == MAX_TESTS:
                raise InternalError("the apprentice ran out of tests")
            q = _split(sp, alive, board)
            tests += 1
        a = (secret >> q) & 1
        alive &= sp.col(q) if a else ~sp.col(q)
        board.add(q)
        steps.append(("guess" if guess else "test", sp.rules[h].text if guess else None, q, a,
                      alive.bit_count(), share if guess else None))
