"""Every line the player reads (SPEC 8). Pure functions from game data to text."""

import re
import sys
from pathlib import Path

from . import GEN_VERSION, VERSION
from .errors import plural
from .lang import literal_text, rule_gloss, rule_text
from .play import median as median_of
from .play import seed_record as seed_record_of
from .play import tier_record as tier_record_of
from .puzzle import OPEN_GAP
from .tiers import OPEN_TIER, PAR, TIERS
from .universe import U, koan_text

WIDTH = 68
FORBIDDEN = frozenset(b"#><|&;$\\`{}[]\t\r")
PATH_OK = re.compile(r"\A[A-Za-z0-9 _.:/-]*\Z")
PLAYING_NEXT = "test KOAN / check RULE / guess RULE / log / rules"
HOLD_NEXT = "test KOAN / hold RULE / guess RULE / log / rules"
BROKEN_NEXT = "hold RULE / test KOAN / check RULE / guess RULE / log / rules"
YESNO = {1: "YES", 0: "NO"}
STATUS = {"playing": "playing", "solved": "solved", "gaveup": "gave up"}
GLUE = "\x00"            # a space that wrap() never breaks at; line() prints it as a space
SHOWN_THEORIES = 6       # THEORY lines on the end card before `and N more`


# ---------------------------------------------------------------- layout primitives (8.2)
def wrap(text, width=WIDTH):
    """Greedy wrap that breaks only at runs of spaces (the run at a break is removed)."""
    parts = re.split(r"( +)", text)
    lines = []
    cur = parts[0]
    for i in range(1, len(parts) - 1, 2):
        gap, word = parts[i], parts[i + 1]
        if len(cur) + len(gap) + len(word) <= width:
            cur += gap + word
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    return lines


def line(keyword, text=""):
    """KEYWORD padded to 10, then text wrapped at 68 with the keyword repeated."""
    return ["%-10s%s" % (keyword, part.replace(GLUE, " ")) for part in wrap(text)]


def glued(text):
    """Text that line() keeps on one line: a koan in prose is never split (8.2)."""
    return text.replace(" ", GLUE)


def raw_line(keyword, text):
    """A path line: never wrapped, exempt from the width cap (8.2)."""
    return ["%-10s%s" % (keyword, text)]


def board_lines(keyword, indices):
    lines = []
    cur = ""
    for q in indices:
        k = koan_text(U[q])
        if not cur:
            cur = k
        elif len(cur) + 3 + len(k) <= WIDTH:
            cur += " / " + k
        else:
            lines.append(cur)
            cur = k
    lines.append(cur)
    return ["%-10s%s" % (keyword, part) for part in lines]


def finish(lines):
    """Join lines into one response: right-stripped, LF-terminated."""
    return "".join(l.rstrip() + "\n" for l in lines)


def kpad(koan, label=None):
    """Koan text left-justified to 12, then the label (RESULT/REPEAT/CONFLICT/log layout)."""
    if label is None:
        return "%-12s" % koan_text(koan)
    return "%-12s %-3s" % (koan_text(koan), YESNO[label])


def predictions(label, predicted, theory):
    """What follows a new koan's label on RESULT and log TEST lines: the typed prediction, then
    the held theory's (written shorter after a typed one, so the line stays within 68)."""
    text = ""
    for who, p in (("predicted", predicted),
                   ("theory" if predicted is not None else "your theory said", theory)):
        if p is not None:
            text += "   %s %s: %s" % (who, YESNO[int(p)],
                                       "right" if int(p) == label else "SURPRISE")
    return text


def theory_state(theory):
    """`(fits the board)` or `broken by 2 1` (the HOLDING line and `hold`)."""
    if theory.broken is None:
        return "(fits the board)"
    return "broken by " + glued(koan_text(U[theory.broken[0]]))


def broken_line(theory):
    """BROKEN, right after the line of the koan that broke the held theory (NEXT then starts
    with hold RULE). Its label is the board's, never the secret."""
    q = theory.broken[0]
    says = (theory.mask >> q) & 1
    return line("BROKEN", 'your theory "%s" says %s on %s but the master says %s.'
                % (theory.text, YESNO[says], glued(koan_text(U[q])), YESNO[1 - says]))


# ---------------------------------------------------------------- game lines (8.3)
def game_fields(game, status=None):
    label = game["label"]
    if label.startswith("daily-"):
        text = "tier %d   daily %s   %s" % (game["tier"], label[len("daily-"):],
                                             STATUS[status or game["status"]])
    elif label.startswith("D-"):
        text = "tier %d   code %s   %s" % (game["tier"], label, STATUS[status or game["status"]])
    else:
        text = "tier %d   seed %s   %s" % (game["tier"], label, STATUS[status or game["status"]])
    if game["assisted"]:
        text += "   assisted"
    return text


def score_sum(gs):
    """`9 = 7 tests + 2 asks + 3 x 0 wrong`; the asks only when there are any (tier 5)."""
    paid = plural(gs.tests, "test")
    if gs.asks:
        paid += " + " + plural(gs.asks, "ask")
    return "%d = %s + 3 x %d wrong" % (gs.score, paid, gs.wrong)


def score_text(gs):
    tier = gs.game["tier"]
    base = score_sum(gs)
    if gs.status == "playing":
        return "%s   par %d" % (base, PAR[tier])
    if gs.status == "solved":
        return "final %s   par %d" % (base, PAR[tier])
    return "not scored (gave up)"


def foresight(gs):
    return "%d of %s right" % (gs.right_predictions, plural(gs.predictions, "prediction"))


def recap(gs):
    yes, no = gs.yes_koans(), gs.no_koans()
    out = line("GAME", game_fields(gs.game, gs.status))
    out += line("SCORE", score_text(gs))
    if gs.status == "playing" and (gs.tests >= 30 or gs.wrong >= 7):   # only near a cap
        out += line("LEFT", "%s and %s" % (plural(40 - gs.tests, "test"),
                                           plural(10 - gs.wrong, "wrong guess")))
    if gs.predictions:
        out += line("FORESIGHT", foresight(gs))
    out += line("BOARD", "%s: %d YES, %d NO" % (plural(len(yes) + len(no), "koan"), len(yes),
                                                len(no)))
    out += board_lines("YES", yes)
    out += board_lines("NO", no)
    if gs.theory is not None:
        out += line("HOLDING", "%s   %s" % (gs.theory.text, theory_state(gs.theory)))
    return out


# ---------------------------------------------------------------- responses
def error(message, tries, next_text, keyword="ERROR"):
    out = line(keyword, message)
    for cmd, note in tries[:3]:
        width = max(36, len(cmd) + 2)
        text = ("%-*s%s" % (width, cmd, note)).rstrip() if note else cmd
        if len("TRY       " + text) > 78:
            text = cmd
        if len("TRY       " + text) > 78:
            continue
        out.append("TRY       " + text)
    out += line("UNCHANGED", "nothing was recorded, no cost")
    out += line("NEXT", next_text)
    return finish(out)


def new_game(gs, replay_note, moved_to=None):
    game = gs.game
    tier = game["tier"]
    label = game["label"]
    if label.startswith("daily-"):
        date = label[len("daily-"):]
        head = "daily puzzle for %s (tier 3)." % date
        again = "new daily %s replays this puzzle" % date
    elif label.startswith("D-"):
        head = "dealt puzzle: tier 5 rules, the start chosen by its dealer."
        again = "new code %s replays this puzzle" % label
    else:
        head = "tier %d puzzle%s." % (tier, " (open: the rule is not from a list)"
                                       if tier == OPEN_TIER else "")
        again = "new seed %s replays this puzzle" % label
    out = line("NEW", head + " The board shows 2 YES and 2 NO koans.")
    out += line("AGAIN", again)
    if replay_note:
        out += line("NOTE", "you have played this seed before, so this game is marked as a replay")
    if game["assisted"]:
        out += line("NOTE", "assisted game: scripts are allowed and it is recorded separately")
    out += line("MASTER", "The %s has chosen a rule. Two koans have it, two do not."
                % ("dealer" if label.startswith("D-") else "master"))
    out += recap(gs)
    if moved_to is not None:   # round 13: said once, on the first new
        out += line("SAVE", "the game's own folder cannot be written, so your games are saved "
                    "in the temp folder (GAME_HOME chooses another):")
        out += raw_line("SAVE", '"%s"' % scrub_path(moved_to))
    out += line("NEXT", PLAYING_NEXT)
    return finish(out)


def show(gs, show_all):
    if gs.status == "playing":
        return finish(recap(gs) + line("NEXT", PLAYING_NEXT))
    if not show_all:
        out = line("GAME", game_fields(gs.game, gs.status))
        out += line("SCORE", score_text(gs))
        out += line("HIDDEN", "board and rule are hidden so that a later player is not spoiled")
        out += line("NEXT", "new / show all")
        return finish(out)
    secret = gs.puzzle.secret
    out = recap(gs)
    out += line("RULE", secret.text)
    out += line("MEANS", rule_gloss(secret.rule))
    out += line("NEXT", "new")
    return finish(out)


def test_result(gs, lines):
    new = [l for l in lines if not l.repeat]
    reps = len(lines) - len(new)
    if new:
        head = "%s, cost %d" % (plural(len(new), "new koan"), len(new))
    else:
        head = "no new koans, cost 0"
    if reps:
        head += "   (%s, free)" % plural(reps, "repeat")
    out = line("TESTED", head)
    broken = False
    for l in lines:
        body = kpad(l.koan, l.label)
        if l.repeat:
            text = body + "   already on the board, free"
            if l.predicted is not None:
                text += ", prediction not counted"
            out += line("REPEAT", text)
        else:
            body += predictions(l.label, l.predicted, l.theory)
            out.append("%-10s%s" % ("RESULT", body))
            if l.theory is not None and int(l.theory) != l.label:
                broken = True    # the first koan the theory got wrong; it predicts no later one
                out += broken_line(gs.theory)
    out += recap(gs)
    out += line("NEXT", BROKEN_NEXT if broken else PLAYING_NEXT)
    return finish(out)


def conflict_lines(gs, conflicts, mask):
    out = []
    for q in conflicts[:6]:
        out.append("%-10s%s board %s, your rule %s" % ("CONFLICT", kpad(U[q]),
                                                      YESNO[gs.board[q]], YESNO[(mask >> q) & 1]))
    if len(conflicts) > 6:
        out += line("CONFLICT", "and %d more" % (len(conflicts) - 6))
    return out


def parsed_lines(rule):
    return line("PARSED", rule_text(rule)) + line("MEANS", rule_gloss(rule))


def drawable_text(drawable):
    """The TIER line after a wrong guess: why tier 5 never draws that rule, or (round 9b) that a
    tier 1 to 4 list has no rule with its answers; None when the tier could draw it."""
    if drawable[0] == "unlisted":
        return "and tier %d never draws this rule: see the TIER %d list in rules" % (
            drawable[1], drawable[1])
    if drawable[0] != "no":
        return None
    return "and tier 5 never draws this rule: " + drawable_why(drawable[1])


def drawable_why(why):
    """Why tier 5 never draws a rule (open_drawable's problem), for TIER 5 and ASK lines. The
    numbers are the weights of RULES TIER 5 (64, 16, 4, 1 per koan of length 1 to 4), exact."""
    if why[0] == "band":
        why = "its YES koans weigh %d of 1024, but %s" % (
            why[1], glued("tier 5 needs %d to %d" % (OPEN_GAP, 1024 - OPEN_GAP)))
    elif why[0] == "same":       # one of the single conditions that the tier 3 list leaves out
        why = "it is %s, a one-part rule that the tier 3 list leaves out" % glued('"%s"' % why[1])
    elif why[0] == "agrees":     # it differs from that condition only on koans of length 5 or 6
        why = "it agrees with %s on every koan of length 1 to 4" % glued('"%s"' % why[1])
    elif why[0] == "near":       # it differs, on koans weighing 1 to OPEN_GAP - 1 in all
        why = "it differs from %s on koans weighing only %d, but %s" % (
            glued('"%s"' % why[1]), why[2], glued("tier 5 needs %d" % OPEN_GAP))
    elif why[0] == "start":
        why = "it says YES, or NO, on fewer than 2 koans of length 2 to 4"
    else:
        why = "it is not one rule of the tier 3 list or two joined by and or by or"
    return why


def draw_answer(drawable):
    """Could tier 5 draw a rule with these answers, as what, or why not: open_drawable of one
    rule alone, never the hidden rule or the board. The DEAL line uses it (it was the ASK line
    of `ask RULE`, retired in round 8). A joined rule also gets its numbers: its YES weight and
    how far it is from the nearest single condition (round 6). A quoted rule is kept on one line
    (at most 59 characters with its brackets and colon)."""
    kind = drawable[0]
    if kind == "single":
        return "yes: tier 5 could draw it (it is %s of the tier 3 list)" % glued(
            '"%s"' % drawable[1])
    if kind in ("and", "or"):
        _, a, b, w, d, near = drawable
        return ("yes: tier 5 could draw a rule with these answers %s YES weight %d of 1024, at "
                "least %d from every one-part rule %s" % (
                    glued('(as "%s %s %s"):' % (a, kind, b)), w, d,
                    glued('(nearest "%s")' % near)))
    return "no: tier 5 never draws it: " + drawable_why(drawable[1])


def _unordered(lit):
    """Literal text with 'as many D as E' written smaller digit first (it is symmetric)."""
    neg, (kind, args) = lit
    if kind == "as many":
        args = tuple(sorted(args))
    return literal_text((neg, (kind, args)))


def check_result(gs, rule, mask, conflicts):
    out = parsed_lines(rule)
    n = len(gs.board)
    if conflicts:
        out += line("CHECK", "disagrees with %d of %s on the board   (free, nothing recorded)"
                    % (len(conflicts), plural(n, "koan")))
        out += conflict_lines(gs, conflicts, mask)
    else:
        out += line("CHECK", "fits all %s on the board   (free, nothing recorded)"
                    % plural(n, "koan"))
    out += recap(gs)
    out += line("NEXT", PLAYING_NEXT)
    return finish(out)


def _vs_koans(bad, room):
    """`: 2 1 / 4 4` after a rule on its `check A vs B` line: at most 3 of the koans it
    disagrees with, then `and N more`, fewer when 3 do not fit in `room` characters, and none
    (an empty text) when not even 1 fits. Plain check RULE lists them all."""
    for k in range(min(3, len(bad)), 0, -1):
        text = ": " + glued(" / ".join(koan_text(U[q]) for q in bad[:k]))
        if len(bad) > k:
            text += " and %d more" % (len(bad) - k)
        if len(text) <= room:
            return text
    return ""


def check_vs_result(gs, rules, conflicts):
    """`check A vs B ...`: one CHECK entry per rule, in typed order, no PARSED or MEANS (plain
    check RULE shows those). The verdict comes first, padded so that the verdicts form a column
    and the rules start together; a rule too long for its line wraps in that rule column,
    without koans."""
    verdicts = ["disagrees with %d" % len(bad) if bad else "fits all %d" % len(gs.board)
                for bad in conflicts]
    width = max(len(v) for v in verdicts) + 2
    out = []
    for rule, bad, verdict in zip(rules, conflicts, verdicts):
        text = rule_text(rule)
        parts = wrap(text + _vs_koans(bad, WIDTH - width - len(text)), WIDTH - width)
        for i, part in enumerate(parts):
            out.append("%-10s%-*s%s" % ("CHECK", width, "" if i else verdict,
                                        part.replace(GLUE, " ")))
    out += recap(gs)
    out += line("NEXT", PLAYING_NEXT)
    return finish(out)


def hold_result(gs, rule, mask, conflicts):
    """`hold RULE`: now holding it, or refused like a bounced guess (nothing recorded)."""
    if conflicts:
        out = parsed_lines(rule)
        out += line("HOLD", "refused: your rule disagrees with %s on the board"
                    % plural(len(conflicts), "koan"))
        out += conflict_lines(gs, conflicts, mask)
    else:
        out = line("HOLD", "now holding: " + rule_text(rule))
        out += line("MEANS", rule_gloss(rule))
    out += recap(gs)
    out += line("NEXT", HOLD_NEXT)
    return finish(out)


def hold_show(gs, theory):
    """`hold` alone: the theory held now (its state is on the HOLDING line of the recap)."""
    if theory is None:
        out = line("HOLD", "no theory held")
    else:
        out = line("HOLD", "holding: " + theory.text)
        out += line("MEANS", rule_gloss(theory.rule))
    out += recap(gs)
    out += line("NEXT", HOLD_NEXT)
    return finish(out)


def hold_off_result(gs, theory):
    out = line("HOLD", "released: " + theory.text)
    out += recap(gs)
    out += line("NEXT", HOLD_NEXT)
    return finish(out)


def guess_result(gs, rule, mask, verdict, board_size, drawable=None):
    out = parsed_lines(rule)
    if verdict.kind == "bounced":
        out += line("BOUNCED", "your rule disagrees with %s on the board, so it cannot be the "
                    "hidden rule. Free, nothing recorded." % plural(len(verdict.conflicts), "koan"))
        out += conflict_lines(gs, verdict.conflicts, mask)
    elif verdict.kind == "wrong":
        out += line("WRONG", "fits all %s on the board but is not the hidden rule. Cost 3."
                    % plural(board_size, "koan"))
        out += line("COUNTER", "%s is %s, your rule says %s. It is now on the board."
                    % (koan_text(U[verdict.counter]), YESNO[verdict.counter_label],
                       YESNO[1 - verdict.counter_label]))
    else:   # wrong_free
        out += line("WRONG", "fits all %s on the board but is not the hidden rule. After 10 "
                    "wrong guesses there is no cost and no counterexample. Nothing recorded."
                    % plural(board_size, "koan"))
    t = gs.theory
    broken = verdict.kind == "wrong" and t is not None and t.broken is not None and \
        t.broken[0] == verdict.counter
    if broken:     # right after the COUNTER line, the koan that broke it
        out += broken_line(t)
    why = drawable_text(drawable) if drawable else None
    if why:
        out += line("TIER %d" % gs.puzzle.tier, why)
    out += recap(gs)
    out += line("NEXT", BROKEN_NEXT if broken else PLAYING_NEXT)
    return finish(out)


def _records(history, game):
    plays, best, date, helped = seed_record_of(history, game["label"])
    this = "this deal" if game["label"].startswith("D-") else "this seed"
    if plays <= 1:
        first = "%s: first play" % this
    elif best is None:
        first = "%s: %d plays, none solved" % (this, plays)
    else:
        first = "%s: %d plays, best %d (%s%s)" % (this, plays, best, date,
                                                  ", assisted" if helped else "")
    counted, scores = tier_record_of(history, game["tier"])
    tier = game["tier"]
    if game["label"].startswith("D-"):
        second = "dealt puzzles are not counted in the tier 5 records"
    elif scores:
        second = "tier %d: %d of %d solved, best %d, median %s" % (
            tier, len(scores), counted, min(scores), median_of(scores))
    elif counted:
        second = "tier %d: 0 of %d solved" % (tier, counted)
    else:
        second = "tier %d: no counted games yet" % tier
    return line("RECORD", first) + line("RECORD", second)


def theory_lines(gs):
    """THEORY lines of the end card: every theory held, oldest first, and how it ended. Only
    here may a line compare a theory with the hidden rule (spoilers, 8.2)."""
    out = []
    for i, t in enumerate(gs.theories[:SHOWN_THEORIES], 1):
        if t.broken is not None:
            q, how, n = t.broken
            fate = "broken by %s (%s %d)" % (glued(koan_text(U[q])), how, n)
        else:
            fate = t.end or "held to the end"
        out += line("THEORY", "%d. %s   %s" % (i, t.text, fate))
    if len(gs.theories) > SHOWN_THEORIES:
        out += line("THEORY", "and %d more" % (len(gs.theories) - SHOWN_THEORIES))
    if gs.theory is not None and gs.theory.mask == gs.puzzle.secret.mask:
        out += line("THEORY", "your last theory was the hidden rule")
    return out


def hunt_lines(hunt):
    """The apprentice's game in one sentence, after the tier-5 end card's SCORE (round 7)."""
    parts = []
    for kind, text, q, a, _, _ in hunt.steps:
        if kind == "test":
            parts.append(glued("test %s %s" % (koan_text(U[q]), YESNO[a])))
        elif q is None:
            parts.append("guess %s: right" % text)
        else:
            parts.append("guess %s: wrong (%s is %s)" % (text, glued(koan_text(U[q])), YESNO[a]))
    # round 7b: its score's sum too (a hunter had to count 6 tests + 2 wrong x 3 by hand)
    return line("HUNT", "the apprentice, %d = %s + 3 x %d wrong: " % (
        hunt.score, plural(hunt.tests, "test"), hunt.wrong) + ", ".join(parts) + ".")


def share_text(gs, solver_n, hunt):
    """The SHARE line of a solve (round 9): spoiler-free, one line (at most 65 characters: a
    score of 76, an apprentice of 40 and a deal code), with the command that starts the same
    puzzle for a friend: `KOAN in 6 (par 9, apprentice 4): new seed 5-44352`."""
    note = "par %d" % PAR[gs.game["tier"]]
    if solver_n is not None:
        note += ", solver %d" % solver_n
    elif hunt is not None:
        note += ", apprentice %d" % hunt.score
    return glued("KOAN in %d (%s): new %s" % (gs.score, note, _seed_field(gs.game["label"])))


def reveal(gs, guess_rule, solver_n, crux, escape_q, history, hunt=None):
    """The reveal card after a solve (guess_rule given) or `giveup yes` (SPEC 8.4.8). In tier 5
    the apprentice's score stands where tiers 1 to 4 show the list solver's (round 7), on a SCORE
    line of its own when it does not fit."""
    game = gs.game
    tier = game["tier"]
    secret = gs.puzzle.secret
    par = PAR[tier]
    out = []
    if solver_n is not None:
        solver = "   solver %d" % solver_n
    elif hunt is not None:
        solver = "   " + glued("apprentice %d" % hunt.score)
    else:
        solver = "   " + glued("no solver in tier 5")
    if guess_rule is not None:
        out += line("SOLVED", "your rule gives the hidden rule's answer on all 5460 koans")
        out += line("PARSED", rule_text(guess_rule))
        out += line("RULE", secret.text + "   (the hidden rule)")
        if rule_text(guess_rule) == secret.text:
            equal = "same wording"
        elif guess_rule[0] == secret.rule[0] and \
                sorted(map(_unordered, guess_rule[1])) == sorted(map(_unordered, secret.rule[1])):
            equal = "same rule, parts in another order"
        else:
            equal = "different wording, same answer on every koan"
        out += line("EQUAL", equal)
        out += line("MEANS", rule_gloss(secret.rule))
        d = gs.score - par
        rel = "at par" if d == 0 else ("%d under par" % -d if d < 0 else "%d over par" % d)
        if game["assisted"]:
            # Round 15: help (scripts, or the list of rules that still fit) makes a comparison
            # with par meaningless, so an assisted solve does not claim one. The GAME line
            # says why: it ends in `assisted`.
            rel = "not compared"
        out += line("SCORE", "%s   par %d, %s%s" % (score_sum(gs), par, rel, solver))
    else:
        out += line("GAVEUP", "the puzzle is over and is not scored")
        out += line("RULE", secret.text + "   (the hidden rule)")
        out += line("MEANS", rule_gloss(secret.rule))
        asks = ", " + plural(gs.asks, "ask") if gs.asks else ""
        out += line("SCORE", "not scored (%s%s, %d wrong)   par %d%s"
                    % (plural(gs.tests, "test"), asks, gs.wrong, par, solver))
    if hunt is not None:
        out += hunt_lines(hunt)
    trap = gs.puzzle.trap
    if trap is not None:
        out += line("TRAP", "the start also fit: " + trap.text)
        out += line("TRAP", "escape: koan \"%s\" (rule %s, trap %s)" % (
            koan_text(U[escape_q]), YESNO[(secret.mask >> escape_q) & 1],
            YESNO[(trap.mask >> escape_q) & 1]))
    if crux is not None:
        q, k, n = crux
        answer = YESNO[(secret.mask >> q) & 1] if q is not None else None
        if n == 0:
            out += line("CRUX", "the start fit no other rule")
        elif k == n:
            out += line("CRUX", "koan \"%s\" (answer %s) would have ruled out all %d other rules "
                        "that fit the start" % (koan_text(U[q]), answer, n))
        else:
            out += line("CRUX", "koan \"%s\" (answer %s) would have ruled out %d of the %d other "
                        "rules that fit the start" % (koan_text(U[q]), answer, k, n))
    if gs.predictions:
        out += line("FORESIGHT", foresight(gs))
    out += theory_lines(gs)
    out += _records(history, game)
    if guess_rule is not None and not game["assisted"]:
        out += line("SHARE", share_text(gs, solver_n, hunt))
    who = "dealer" if game["label"].startswith("D-") else "master"
    out += line("MASTER", "The %s nods." % who if guess_rule is not None else
                "The %s bows and shows the rule." % who)
    out += line("GAME", game_fields(game, gs.status))
    daily = game["label"].startswith("daily-")
    # `new` alone keeps the tier; a solve at or under par also offers the next tier; tier 5
    # points to the dealer's chair (round 9)
    up = (guess_rule is not None and gs.score <= par and tier < OPEN_TIER
          and not game["assisted"])          # round 15: help earns no comparison with par
    if daily:
        nxt = "new / new tier 4" if up else "new"
    elif tier == OPEN_TIER:
        nxt = "new / help deal / new daily"
    else:
        nxt = "new / new tier %d / new daily" % (tier + 1) if up else "new / new daily"
    out += line("NEXT", nxt)
    return finish(out)


def log(gs, show_all):
    if gs.status != "playing" and not show_all:
        out = line("GAME", game_fields(gs.game, gs.status))
        out += line("SCORE", score_text(gs))
        out += line("HIDDEN", "board and rule are hidden so that a later player is not spoiled")
        out += line("NEXT", "new / log all")
        return finish(out)
    out = line("LOG", game_fields(gs.game, gs.status))
    for q in gs.puzzle.yes:
        out.append("%-10s%s" % ("START", kpad(U[q], 1)))
    for q in gs.puzzle.no:
        out.append("%-10s%s" % ("START", kpad(U[q], 0)))
    for ev in gs.events:
        if ev[0] == "test":
            _, n, q, a, pred, said = ev
            out.append("%-10s%s" % ("TEST %d" % n, kpad(U[q], a) + predictions(a, pred, said)))
        elif ev[0] == "wrong":
            _, n, text, c, a = ev
            out += line("GUESS %d" % n, "wrong +3: " + text)
            out.append("%-10s%s   from guess %d" % ("COUNTER", kpad(U[c], a), n))
        elif ev[0] == "hold":
            out += line("THEORY", "hold " + ev[1])
        elif ev[0] == "off":
            out += line("THEORY", "hold off")
        elif ev[0] == "broken":
            out += line("THEORY", "broken by " + glued(koan_text(U[ev[1]])))
        elif ev[0] == "ask":
            _, n, text, yes = ev
            out += line("ASK %d" % n, "ask %s: %s" % (text, "yes" if yes else "no"))
        elif ev[0] == "solve":
            out += line("GUESS %d" % ev[1], "solved")
            out += line("END", "solved, final score %d" % gs.score)
        else:
            out += line("END", "gave up")
    out += line("NEXT", PLAYING_NEXT if gs.status == "playing" else "new")
    return finish(out)


DEAL_NEXT = "deal RULE / deal RULE board KOANS / new code CODE / help deal"


def deal_unfair(rule, drawable, nearby=()):
    """`deal` of a rule tier 5 never draws: refused, nothing recorded (round 7); nearby: fair
    rules that share a half with it (round 7b: dealers met 17 to 27 refusals each)."""
    out = parsed_lines(rule)
    out += line("DEAL", "not fair: " + draw_answer(drawable)[len("no: "):])
    if nearby:
        out += line("FAIR", "fair rules that share a half with it: "
                    + ", ".join(glued(t) for t in nearby))
    out += line("UNCHANGED", "nothing was recorded")
    out += line("NEXT", DEAL_NEXT)
    return finish(out)


def deal_unbalanced(rule, koans, mask):
    """`deal ... board` whose four koans are not 2 YES and 2 NO under the rule."""
    out = parsed_lines(rule)
    says = ", ".join("%s is %s" % (glued(koan_text(U[q])), YESNO[(mask >> q) & 1])
                     for q in koans)
    out += line("DEAL", "a board needs 2 koans your rule says YES to and 2 it says NO to. "
                "Your rule says: %s." % says)
    out += line("UNCHANGED", "nothing was recorded")
    out += line("NEXT", DEAL_NEXT)
    return finish(out)


def deal_closed():
    """`deal` while a tier-5 puzzle is in play (round 12): refused before its arguments are read.
    Its fair / not fair verdict and its count of fitting rules would be a free drawability check
    for that puzzle, the round-3 oracle."""
    out = line("DEAL", "closed while a tier 5 puzzle is in play: a deal would say for free "
               "which rules tier 5 can draw. Solve it or give it up (giveup yes), then deal.")
    out += line("UNCHANGED", "nothing was recorded")
    out += line("NEXT", PLAYING_NEXT)
    return finish(out)


def deal_result(rule, drawable, yes, no, trap, hunt, n_rules, deals, code, wordings=()):
    """The reply to `deal` (round 7): the verdict, the start (the dealer's board, or the game's
    with its trap), the apprentice's hunt step by step, its score, the dealer's record and the
    code to share. Only the dealer sees it: it names the rule throughout. Round 7b: every
    wording of the rule (its chances add up), how many rules fit the board, the share of the
    chances behind each guess, and whether the deal is a new best."""
    out = parsed_lines(rule)
    out += line("DEAL", "fair: " + draw_answer(drawable)[len("yes: "):])
    if len(wordings) > 1:
        out += line("DEAL", "the draw words it %d ways, so their chances add up: %s"
                    % (len(wordings), " / ".join(glued(w) for w in wordings)))
    if trap is None:
        out += line("START", "your board")
    else:
        out += line("START", "the game's start, with the trap %s (deal RULE board KOANS sets "
                    "your own)" % glued('"%s"' % trap.text))
    out += board_lines("YES", yes)
    out += board_lines("NO", no)
    out += line("HUNT", "the apprentice knows the %d rules tier 5 can draw and their chances. "
                "%d of them fit the board." % (n_rules, hunt.fit))
    for kind, text, q, a, left, share in hunt.steps:
        if kind == "test":
            out.append("%-10s%s   %s left" % ("HUNT", "test " + kpad(U[q], a),
                                              plural(left, "rule")))
        elif q is None:
            out += line("HUNT", "guess %s (%d percent of the chances): right" % (text, share))
        else:
            out += line("HUNT", "guess %s (%d percent of the chances): wrong +3, koan %s is "
                        "%s   %s" % (text, share, glued(koan_text(U[q])), YESNO[a],
                                     glued(plural(left, "rule") + " left")))
    out += line("SCORE", "the apprentice took %d = %s + 3 x %d wrong   (tier 5 par %d)"
                % (hunt.score, plural(hunt.tests, "test"), hunt.wrong, PAR[OPEN_TIER]))
    if deals is None:
        out += line("RECORD", "deals.json cannot be read, so this deal was not recorded")
    else:
        prev = deals["prev_best"]
        if prev is None or hunt.score > prev:
            note = " (this one, a new best)" if prev is not None else " (this one)"
        elif hunt.score == prev:
            note = " (this one ties it)" if deals["best"]["code"] != code else " (this one)"
        else:
            note = ""
        out += line("RECORD", "%s dealt, best %d%s" % (plural(deals["total"], "puzzle"),
                                                      deals["best"]["score"], note))
        if deals["again"]:
            out += line("RECORD", "you dealt this one before: same code, same hunt")
    out += line("CODE", code)
    out += line("SHARE", "a friend hunts it with: new code " + code)
    out += line("NEXT", DEAL_NEXT)
    return finish(out)


def deals_line(deals):
    """The dealer's record (round 7): how many deals ever, and the first with the best score."""
    if not deals or not deals["total"]:
        return []
    best = deals["best"]
    return line("DEALS", "%s dealt, best %d (%s, %s)" % (plural(deals["total"], "puzzle"),
                                                         best["score"], best["code"],
                                                         best["dealt"]))


def stats(history, deals=None):
    if not history:
        return finish(line("STATS", "no finished games yet") + deals_line(deals)
                      + line("NEXT", "new"))
    counts = {}
    for h in history:
        counts[h["outcome"]] = counts.get(h["outcome"], 0) + 1
    parts = ["%d solved" % counts["solved"]] if counts.get("solved") else []
    if counts.get("gaveup"):
        parts.append("%d gave up" % counts["gaveup"])
    if counts.get("abandoned"):
        parts.append("%d abandoned" % counts["abandoned"])
    out = line("STATS", "%s: %s" % (plural(len(history), "game"), ", ".join(parts)))
    for tier in TIERS:
        counted, scores = tier_record_of(history, tier)
        if not counted:
            continue
        if scores:
            out += line("TIER %d" % tier, "%d of %d solved   best %d   median %s   par %d" % (
                len(scores), counted, min(scores), median_of(scores), PAR[tier]))
        else:
            out += line("TIER %d" % tier, "0 of %d solved   par %d" % (counted, PAR[tier]))
    dailies = [h for h in history if h["label"].startswith("daily-")]
    if dailies:
        last = max(dailies, key=lambda h: h["id"])
        outcome = {"solved": "solved with score %s" % last["score"], "gaveup": "gave up",
                   "abandoned": "abandoned"}[last["outcome"]]
        if last["assisted"] and last["outcome"] == "solved":
            outcome += ", assisted"             # round 15: an assisted score says so
        out += line("DAILY", "%d played, %d solved, last %s %s" % (
            len(dailies), sum(1 for h in dailies if h["outcome"] == "solved"),
            last["label"][len("daily-"):], outcome))
    other = sum(1 for h in history if h["assisted"] or h["replay"])
    if other:
        out += line("OTHER", "%s (assisted or replays) %s not counted in TIER lines"
                    % (plural(other, "game"), "is" if other == 1 else "are"))
    dealt = sum(1 for h in history if h["label"].startswith("D-")
                and not h["assisted"] and not h["replay"])
    if dealt:
        out += line("OTHER", "%s %s not counted in TIER lines"     # wording of round 9b
                    % ("1 puzzle played from a code" if dealt == 1 else
                       "%d puzzles played from codes" % dealt, "is" if dealt == 1 else "are"))
    out += deals_line(deals)
    for h in sorted(history, key=lambda h: -h["id"])[:5]:
        fields = [h["ended"], h["label"], STATUS.get(h["outcome"], h["outcome"])]
        if h["outcome"] == "solved":
            fields += ["score %d" % h["score"], "par %d" % h["par"]]
        if h["assisted"]:
            fields.append("assisted")
        if h["replay"]:
            fields.append("replay")
        out += line("RECENT", "  ".join(fields))
    out += line("NEXT", "new")
    return finish(out)


def safe_path(path):
    """The path with '/' separators, or None if it has a character outside the 10.1 set."""
    p = str(path).replace("\\", "/")
    return p if PATH_OK.match(p) else None


def scrub_path(path):
    return re.sub(r"[^A-Za-z0-9 _.:/-]", "?", str(path).replace("\\", "/"))


def run_name(game_path):
    """`game.py`, or the name of a one-file build such as `koan.py` (round 13)."""
    name = Path(str(game_path)).name
    return name if PATH_OK.match(name) and " " not in name and name else "game.py"


def named(text, game_path):
    """A fixed text with `python game.py ` naming the file actually run (round 13)."""
    name = run_name(game_path)
    if name == "game.py":
        return text
    return text.replace("python game.py ", "python %s " % name)


def help_text(game_path, texts, here=False):
    """`here`: the shell is in the game's folder, so the short form is printed (round 13)."""
    p = None if here else safe_path(game_path)
    text = texts.HELP
    text = named(text, game_path)
    old = ("          never need to remember anything. From any folder, use the full\n"
           "          path: python \"<GAME>\" VERB ...\n")
    if p is None:
        text = text.replace(old, "          never need to remember anything. Run it in the "
                                 "game's folder.\n")
    else:
        text = text.replace("<GAME>", p)
    return text


def intro(game_path, texts, last_game_gs=None, save_problem=None, here=False):
    out = texts.PITCH.rstrip("\n").split("\n")
    p = None if here else safe_path(game_path)
    if p is None:
        out += raw_line("RUN", "python %s VERB    (run it in the game's folder)"
                        % run_name(game_path))
    else:
        out += raw_line("RUN", 'python "%s" VERB    (one command per move)' % p)
    out += line("LEARN", "help (one page, read it first), then rules (every promise)")
    out += line("CLIMB", "new starts at tier 1, or skip ahead: new tier 3. A solve at or under "
                "par offers the next tier. After tier 5, deal your own: help deal")   # round 9b
    if last_game_gs is not None:
        gs = last_game_gs
        fields = "tier %d   %s   " % (gs.game["tier"], _seed_field(gs.game["label"]))
        if gs.status == "solved":
            fields += "solved, score %d" % gs.score
        else:
            fields += "gave up"
        out += line("LAST", fields + "   (board hidden: show all)")
    if save_problem == "unreadable":
        out += line("SAVE", "the save file is unreadable. doctor explains, new force starts over.")
    elif save_problem == "other":
        out += line("SAVE", "this puzzle was made by another version of the game. new force "
                    "starts a new one (your records are kept).")
    out += line("NEXT", "new")
    return finish(out)


def _seed_field(label):
    if label.startswith("daily-"):
        return "daily " + label[len("daily-"):]
    if label.startswith("D-"):
        return "code " + label
    return "seed " + label


def version_line():
    v = sys.version_info
    return "KOAN %s   gen %d   python %d.%d.%d" % (VERSION, GEN_VERSION, v[0], v[1], v[2])


def version():
    return finish(line("VERSION", version_line()) + line("NEXT", "help"))


def internal(name, saved):
    out = line("INTERNAL", "something went wrong inside the game (%s)" % name)
    out += line("INTERNAL", "your move was saved, see show" if saved else "nothing was saved")
    out += line("NEXT", "doctor / show")
    return finish(out)
