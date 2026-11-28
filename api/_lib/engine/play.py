"""Pure game logic (SPEC 6.2, 6.3, 7, 9.1 level 2). No I/O and no text: results are data.

`replay()` rebuilds the board from the save's moves, checking every move against the puzzle.
The `do_*` functions apply one command to that state and append the moves they cause.
`check`, `hold`, bounced guesses and every refusal look only at the board, never at the secret.
`ask RULE` was retired in round 8; saves from rounds 5-7 with ask moves still replay and score.
"""

import collections

from . import GEN_VERSION
from .errors import BadSave, Refused, UsageError, plural
from .lang import parse_canonical, rule_mask, rule_text
from .puzzle import make_puzzle, open_drawable
from .tiers import OPEN_TIER, PAR
from .universe import INDEX, U, bit, first_diff, koan_str, parse_digits

MAX_TESTS = 40
MAX_WRONG = 10
MAX_THEORIES = 12        # `hold RULE` per puzzle, so that `log` stays within 8192 bytes
MAX_ASKS = 6             # ask moves in a round 5-7 save (ask is retired), for the same reason
CONFLICT_LIMIT = 6

TestLine = collections.namedtuple("TestLine", "koan label repeat predicted theory")
Verdict = collections.namedtuple("Verdict", "kind conflicts counter counter_label")


class Theory:
    """A rule pinned with `hold RULE`. It predicts every new koan until one breaks it."""

    def __init__(self, rule):
        self.rule = rule
        self.text = rule_text(rule)
        self.mask = rule_mask(rule)
        self.broken = None       # (koan index, "test" or "guess", that test's or guess's number)
        self.end = None          # "released" (hold off) or "replaced" (a later hold)


class GameState:
    """A game rebuilt from its save entry: board, counts, predictions and the event log."""

    def __init__(self, game, puzzle):
        self.game = game
        self.puzzle = puzzle
        self.board = {}          # koan index -> label (0/1)
        self.tests = 0
        self.asks = 0
        self.asked = set()       # masks of the ask moves (round 5-7 saves): never twice
        self.wrong = 0
        self.right_predictions = 0
        self.predictions = 0
        self.theories = []       # every theory held, oldest first
        self.theory = None       # the one held now, broken or not
        self.events = []         # for `log`
        self.status = "playing"
        for q in puzzle.yes:
            self.board[q] = 1
        for q in puzzle.no:
            self.board[q] = 0

    @property
    def secret(self):
        return self.puzzle.secret.mask

    @property
    def over(self):
        return self.status != "playing"

    @property
    def score(self):
        return self.tests + self.asks + 3 * self.wrong

    def yes_koans(self):
        return sorted(q for q, a in self.board.items() if a == 1)

    def no_koans(self):
        return sorted(q for q, a in self.board.items() if a == 0)

    def conflicts(self, mask):
        """Board koans where a rule with this mask disagrees with the board, in index order."""
        return [q for q in sorted(self.board) if bit(mask, q) != self.board[q]]

    def theory_says(self, q):
        """The held theory's answer (True/False) on koan q while it is unbroken, else None."""
        t = self.theory
        if t is None or t.broken is not None:
            return None
        return bool(bit(t.mask, q))


def _bad(n):
    return BadSave("move %d does not match this game" % n)


def hold_code(text):
    """How a hold or ask move stores its rule: the canonical text as lowercase ASCII hex. A
    player may hold or ask about the hidden rule itself, and no file may contain its text
    (SPEC 9), so such a rule is never written in clear, whatever the secret is."""
    return text.encode("ascii").hex()


def _stored_rule(code, n):
    """The rule of a hold or ask move's `r`; BadSave unless it decodes to a canonical rule text."""
    try:
        text = bytes.fromhex(code).decode("ascii")
        rule = parse_canonical(text)
    except (UsageError, ValueError):
        raise _bad(n) from None
    if hold_code(rule_text(rule)) != code:
        raise _bad(n)
    return rule


def _foresee(gs, pred, said, a):
    """FORESIGHT for one new koan: an explicit prediction wins over the held theory's."""
    p = pred if pred is not None else said
    if p is not None:
        gs.predictions += 1
        gs.right_predictions += int(p == bool(a))


def _observe(gs, q, a, how, n):
    """A koan just joined the board: it breaks the held theory if the theory says otherwise."""
    t = gs.theory
    if t is not None and t.broken is None and bit(t.mask, q) != a:
        t.broken = (q, how, n)
        gs.events.append(("broken", q))


def _hold_refusal(gs, theory):
    """Why `hold` cannot take this theory now (board only), or None. Conflicts come first."""
    if gs.conflicts(theory.mask):
        return "conflicts"
    if len(gs.theories) >= MAX_THEORIES:
        return "you have held %d theories, the most one puzzle allows" % MAX_THEORIES
    if gs.theory is not None and gs.theory.text == theory.text:
        return "you already hold this theory"
    return None


def _take(gs, theory):
    if gs.theory is not None:
        gs.theory.end = "replaced"
    gs.theory = theory
    gs.theories.append(theory)
    gs.events.append(("hold", theory.text))


def _release(gs):
    gs.theory.end = "released"
    gs.theory = None
    gs.events.append(("off",))


def _ask(gs, rule, m):
    """Count one ask move of a round 5-7 save. Its answer is open_drawable of the asked rule's
    mask alone."""
    drawable = open_drawable(m)
    gs.asks += 1
    gs.asked.add(m)
    gs.events.append(("ask", gs.asks, rule_text(rule), drawable[0] != "no"))
    return drawable


def replay(game):
    """Regenerate the puzzle and replay every move; raise BadSave if anything does not match."""
    if game["gen"] != GEN_VERSION:
        raise BadSave("made by gen %d" % game["gen"], other_version=True)
    gs = GameState(game, make_puzzle(game["tier"], game["label"]))
    t = gs.secret
    moves = game["moves"]
    for n, m in enumerate(moves, 1):
        if gs.over:
            raise _bad(n)
        if m["v"] == "test":
            koan = parse_digits(m["k"])
            q = INDEX[koan]
            if q in gs.board or m["a"] != bit(t, q) or gs.tests >= MAX_TESTS:
                raise _bad(n)
            gs.board[q] = m["a"]
            gs.tests += 1
            pred = None if m["p"] is None else bool(m["p"])
            said = gs.theory_says(q)
            _foresee(gs, pred, said, m["a"])
            gs.events.append(("test", gs.tests, q, m["a"], pred, said))
            _observe(gs, q, m["a"], "test", gs.tests)
        elif m["v"] == "hold" and m["r"] is None:
            if gs.theory is None:
                raise _bad(n)
            _release(gs)
        elif m["v"] == "hold":
            theory = Theory(_stored_rule(m["r"], n))
            if _hold_refusal(gs, theory):
                raise _bad(n)
            _take(gs, theory)
        elif m["v"] == "ask":   # never a repeat: a rule asked before is not recorded again
            rule = _stored_rule(m["r"], n)
            mask = rule_mask(rule)
            if gs.puzzle.tier != OPEN_TIER or mask in gs.asked or gs.asks >= MAX_ASKS:
                raise _bad(n)
            _ask(gs, rule, mask)
        elif m["v"] == "wrong":
            try:
                rule = parse_canonical(m["g"])
            except UsageError:
                raise _bad(n) from None
            g = rule_mask(rule)
            if rule_text(rule) != m["g"] or g == t or gs.conflicts(g) or gs.wrong >= MAX_WRONG:
                raise _bad(n)
            c = first_diff(g, t)
            if koan_str(U[c]) != m["c"] or m["a"] != bit(t, c):
                raise _bad(n)
            gs.board[c] = m["a"]
            gs.wrong += 1
            gs.events.append(("wrong", gs.wrong, m["g"], c, m["a"]))
            _observe(gs, c, m["a"], "guess", gs.wrong)
        elif m["v"] == "solve":
            gs.status = "solved"
            gs.events.append(("solve", gs.wrong + 1))
        else:
            gs.status = "gaveup"
            gs.events.append(("giveup",))
    if gs.status != game["status"]:
        if not moves:     # there is no move to name: moves are numbered from 1
            raise BadSave("the status does not match the moves")
        raise _bad(len(moves))
    return gs


def _over(gs):
    return Refused("this puzzle is over (%s). Start another with new."
                   % ("solved" if gs.status == "solved" else "gave up"))


def do_test(gs, items):
    """Apply `test` (items from koans.parse_test). Returns TestLines in typed order."""
    if gs.over:
        raise _over(gs)
    seen = set()
    new = []
    for koan, pred in items:
        q = INDEX[koan]
        if q not in gs.board and q not in seen:
            new.append(q)
        seen.add(q)
    left = MAX_TESTS - gs.tests
    if len(new) > left:
        raise Refused("this needs %s but only %d %s left. Nothing was tested."
                      % (plural(len(new), "test"), left, "is" if left == 1 else "are"))
    lines = []
    t = gs.secret
    for koan, pred in items:
        q = INDEX[koan]
        if q in gs.board:
            lines.append(TestLine(koan, gs.board[q], True, pred, None))
            continue
        a = bit(t, q)
        gs.board[q] = a
        gs.tests += 1
        said = gs.theory_says(q)
        _foresee(gs, pred, said, a)
        gs.game["moves"].append({"v": "test", "k": koan_str(koan), "a": a,
                                 "p": None if pred is None else int(pred)})
        gs.events.append(("test", gs.tests, q, a, pred, said))
        _observe(gs, q, a, "test", gs.tests)
        lines.append(TestLine(koan, a, False, pred, said))
    return lines


def do_check(gs, rule):
    """`check`: free, never recorded, board only."""
    if gs.over:
        raise _over(gs)
    return gs.conflicts(rule_mask(rule))


def held(gs):
    """`hold` alone: the theory held now, or None. Read-only."""
    if gs.over:
        raise _over(gs)
    return gs.theory


def do_hold(gs, rule):
    """`hold RULE`: free, recorded, board only. Returns the board koans the rule disagrees
    with; when there are any the hold is refused like a bounced guess and nothing changes."""
    if gs.over:
        raise _over(gs)
    theory = Theory(rule)
    why = _hold_refusal(gs, theory)
    if why == "conflicts":
        return gs.conflicts(theory.mask)
    if why:
        raise Refused(why)
    _take(gs, theory)
    gs.game["moves"].append({"v": "hold", "r": hold_code(theory.text)})
    return []


def do_hold_off(gs):
    """`hold off`: releases the held theory (recorded). Returns it."""
    if gs.over:
        raise _over(gs)
    theory = gs.theory
    if theory is None:
        raise Refused("no theory is held, so there is nothing to release")
    _release(gs)
    gs.game["moves"].append({"v": "hold", "r": None})
    return theory


def do_guess(gs, rule):
    """`guess`: bounced / wrong / wrong_free / solved (SPEC 6.3, C2)."""
    if gs.over:
        raise _over(gs)
    g = rule_mask(rule)
    conflicts = gs.conflicts(g)
    if conflicts:
        return Verdict("bounced", conflicts, None, None)
    t = gs.secret
    if g == t:
        gs.status = "solved"
        gs.game["moves"].append({"v": "solve"})
        gs.game["status"] = "solved"
        gs.events.append(("solve", gs.wrong + 1))
        return Verdict("solved", [], None, None)
    if gs.wrong >= MAX_WRONG:
        return Verdict("wrong_free", [], None, None)
    c = first_diff(g, t)
    a = bit(t, c)
    gs.board[c] = a
    gs.wrong += 1
    gs.game["moves"].append({"v": "wrong", "g": rule_text(rule), "c": koan_str(U[c]), "a": a})
    gs.events.append(("wrong", gs.wrong, rule_text(rule), c, a))
    _observe(gs, c, a, "guess", gs.wrong)
    return Verdict("wrong", [], c, a)


def do_giveup(gs):
    if gs.over:
        raise Refused("this puzzle is already over")
    gs.status = "gaveup"
    gs.game["moves"].append({"v": "giveup"})
    gs.game["status"] = "gaveup"
    gs.events.append(("giveup",))


def history_entry(gs, outcome, ended):
    g = gs.game
    return {"id": g["id"], "gen": g["gen"], "tier": g["tier"], "label": g["label"],
            "assisted": g["assisted"], "replay": g["replay"], "started": g["started"],
            "ended": ended, "outcome": outcome, "tests": gs.tests, "wrong": gs.wrong,
            "score": gs.score if outcome == "solved" else None, "par": PAR[g["tier"]]}


def median(scores):
    """Median as text: '9' for [7, 11], '5.5' for [5, 6]."""
    s = sorted(scores)
    n = len(s)
    m = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    return "%g" % m


def seed_record(history, label):
    """(plays, best score or None, date of best, best is assisted) for RECORD line 1. The best
    is over the solves without help; only when every solve had help is it an assisted one
    (round 15), and then it says so."""
    same = [h for h in history if h["label"] == label]
    solved = [h for h in same if h["outcome"] == "solved"]
    if not solved:
        return len(same), None, None, False
    plain = [h for h in solved if not h["assisted"]]
    pool = plain or solved
    best = min(h["score"] for h in pool)
    date = min(h["ended"] for h in pool if h["score"] == best)
    return len(same), best, date, not plain


def tier_record(history, tier):
    """(counted games, solved scores) over non-assisted, non-replay entries of a tier; dealt
    puzzles (round 7) are not counted either: their starts are chosen by a dealer."""
    counted = [h for h in history if h["tier"] == tier and not h["assisted"] and not h["replay"]
               and not h["label"].startswith("D-")]
    return len(counted), [h["score"] for h in counted if h["outcome"] == "solved"]
