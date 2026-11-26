"""The reference solver, the crux test and the trap's escape koan (SPEC 7.3-7.5)."""

from .errors import InternalError
from .puzzle import fits
from .tiers import OPEN_TIER, get_lists
from .universe import LE4, first_diff, ones

POOL = 340   # koans of length 1-4 are indices 0..339


_COLUMNS = {}


def _columns(tier):
    """col[q] has bit h set when entry h of the tier's list says YES on koan q (q < 340).
    The lists are fixed, so the transpose is computed once per tier and process."""
    cols = _COLUMNS.get(tier)
    if cols is None:
        cols = [0] * POOL
        for h, e in enumerate(get_lists()[tier]):
            bit = 1 << h
            for q in ones(e.mask & LE4):
                cols[q] |= bit
        _COLUMNS[tier] = cols
    return cols


def solve(puzzle):
    """Tests a greedy solver that knows the tier's list needs from this start (SPEC 7.3).
    None for the open tier, whose rule space is not a list."""
    if puzzle.tier == OPEN_TIER:
        return None
    masks = [e.mask for e in get_lists()[puzzle.tier]]
    cols = _columns(puzzle.tier)
    t = puzzle.secret.mask
    v = 0
    for h, m in enumerate(masks):
        if fits(m, puzzle.yes, puzzle.no):
            v |= 1 << h
    steps = 0
    while v & (v - 1):
        nv = v.bit_count()
        best, best_q = 0, None
        for q in range(POOL):
            y = (v & cols[q]).bit_count()
            s = min(y, nv - y)
            if s > best:
                best, best_q = s, q
        if best_q is None:
            raise InternalError("solver cannot split the remaining rules")
        if (t >> best_q) & 1:
            v &= cols[best_q]
        else:
            v &= ~cols[best_q]
        steps += 1
    return steps


def apprentice_hunt(puzzle):
    """The apprentice's game from this start: the open tier's solver (round 7); None in tiers
    1 to 4, which have the list solver."""
    if puzzle.tier != OPEN_TIER:
        return None
    from .apprentice import hunt
    return hunt(puzzle.secret.mask, puzzle.yes, puzzle.no)


def crux(puzzle):
    """(koan index, rules it rules out, other rules fitting the start), SPEC 7.4; None in tier 5."""
    if puzzle.tier == OPEN_TIER:
        return None
    t = puzzle.secret.mask
    others = [e.mask for e in get_lists()[puzzle.tier]
              if e.mask != t and fits(e.mask, puzzle.yes, puzzle.no)]
    board = set(puzzle.yes) | set(puzzle.no)
    best, best_q = -1, None
    for q in range(POOL):
        if q in board:
            continue
        tv = (t >> q) & 1
        k = sum(1 for m in others if (m >> q) & 1 != tv)
        if k > best:
            best, best_q = k, q
    return best_q, best, len(others)


def escape(puzzle):
    """The first koan where the trap and the hidden rule differ (SPEC 7.5), or None in tier 1."""
    if puzzle.trap is None:
        return None
    return first_diff(puzzle.trap.mask, puzzle.secret.mask)
