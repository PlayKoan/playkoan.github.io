"""Command-line entry: one verb per process (SPEC 8). main() never raises."""

import difflib
import errno
import io
import os
import re
import sys
import traceback
from pathlib import Path

from . import GEN_VERSION, apprentice, deal, render, state, texts
from .errors import BadSave, Busy, NoGame, Refused, UsageError, echo
from .koans import parse_test
from .universe import INDEX
from .lang import is_rule_word, parse_rule, rule_mask
from .play import (do_check, do_giveup, do_guess, do_hold, do_hold_off, do_test, held,
                   history_entry, replay)
from .puzzle import draw_rule, open_drawable, starters
from .solver import apprentice_hunt, crux, escape, solve
from .tiers import OPEN_TIER, SIZES, get_lists, verify_lists

VERBS = ("help", "rules", "new", "show", "test", "check", "hold", "guess", "log", "stats",
         "giveup", "deal", "doctor", "version")
VERB_ALIASES = {"--help": "help", "-h": "help", "--version": "version", "-v": "version",
                "status": "show", "board": "show", "history": "log"}
REFUSED_VERBS = {}
for _words, _msg in (
        ("solve answer submit", "to state the rule, use guess"),
        ("try query eval evaluate", "to ask about a koan, use test"),
        ("ask", "ask is retired. To ask about a koan, use test"),   # round 8
        ("quit exit stop", "there is nothing to quit, the game saves after every command. "
                           "To end this puzzle and see the rule: giveup"),
        ("reveal resign surrender forfeit", "to end this puzzle and see the rule: giveup"),
        ("undo", "there is no undo, every answer stays on the board"),
        ("hint hints", "there is no hint. check is free and tests cost 1."),
        ("start begin play reset restart", "to start a puzzle use new (new force abandons the "
                                           "current one)")):
    for _w in _words.split():
        REFUSED_VERBS[_w] = _msg

GAME_VERBS = ("show", "test", "check", "hold", "guess", "log", "giveup")
OPTIONAL_WORD = {"help": ("words", "deal"), "show": ("all",), "log": ("all",),
                 "giveup": ("yes",)}
NUMBER_TIERS = {"1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "one": 1, "two": 2, "three": 3,
                "four": 4, "five": 5}
SEED = re.compile(r"\A[1-5]-[0-9]{5}\Z", re.ASCII)
DATE_SHAPE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z", re.ASCII)
MAX_ARG_CHARS = 400
MAX_VS = 6               # rules in one `check A vs B ...`
_SPLIT = re.compile(r"[ \t\n\r\f\v]+")


class Result:
    def __init__(self, code, text):
        self.code = code
        self.text = text


def _game_path():
    return state.game_file()


def _run_here():
    """True when the shell is in the game's own folder (round 13): then the intro and HELP
    print the short command, not the full path, which would show the player's folders."""
    try:
        here = os.path.normcase(os.path.realpath(os.getcwd()))
        return here == os.path.normcase(os.path.realpath(str(_game_path().parent)))
    except (OSError, ValueError):
        return False


# errors of a save folder that cannot be made or written (round 13)
_DIR_ERRNOS = {errno.EACCES, errno.EPERM, errno.EROFS, errno.EEXIST, errno.ENOENT,
               errno.ENOTDIR, errno.EISDIR, errno.EINVAL}


def _save_dir_result(e):
    if e.from_env:
        msg = "cannot write the save folder that GAME_HOME names:"
    else:
        msg = "cannot write a save folder, neither next to the game nor in the temp folder:"
    out = render.line("ERROR", msg)
    out += render.raw_line("SAVE", '"%s"' % render.scrub_path(e.path))
    out += render.line("FIX", "set GAME_HOME to a folder you can write, such as /tmp/koan")
    out += render.line("UNCHANGED", "nothing was recorded, no cost")
    out += render.line("NEXT", "doctor")
    return Result(1, render.finish(out))


def _error(e, next_text, code=2):
    return Result(code, render.error(e.message, getattr(e, "tries", []), next_text))


def _refused(message, next_text, keyword="ERROR"):
    return Result(1, render.error(message, [], next_text, keyword))


# ---------------------------------------------------------------- dispatch
def split_quoted(argv):
    """Round 13: an agent often passes the whole move as one argument (`"test 2 4 1"`). No verb
    contains a space, so a first argument with spaces in it is split into words."""
    argv = list(argv)
    if argv and _SPLIT.search(argv[0]) and argv[0].strip():
        argv = [w for w in _SPLIT.split(argv[0]) if w] + argv[1:]
    return argv


def run(argv, environ, saved):
    args = argv[1:]
    if sum(len(a) for a in args) > MAX_ARG_CHARS:
        return _error(UsageError("the command is too long (%d characters, limit %d)"
                                 % (sum(len(a) for a in args), MAX_ARG_CHARS)), "help")
    if not argv:
        return cmd_intro(environ)
    word = argv[0].lower()
    verb = VERB_ALIASES.get(word, word)
    if verb not in VERBS:
        return unknown_verb(argv)
    if verb in ("rules", "stats", "doctor", "version") and args:
        return _error(UsageError("'%s' takes no arguments" % verb), "help")
    if verb in OPTIONAL_WORD:
        if len(args) > 1 or (args and args[0].lower() not in OPTIONAL_WORD[verb]):
            nxt = "help" if verb == "help" else None
            err = UsageError("'%s' takes no argument except %s"
                             % (verb, " or ".join(OPTIONAL_WORD[verb])))
            if nxt:
                return _error(err, nxt)
            return game_verb(verb, None, environ, saved, preset_error=err)
    if verb == "help":
        if args:
            return Result(0, render.named(texts.HELP_DEAL if args[0].lower() == "deal"
                                          else texts.HELP_WORDS, _game_path()))
        return Result(0, render.help_text(_game_path(), texts, _run_here()))
    if verb == "rules":
        return Result(0, render.named(texts.RULES, _game_path()))
    if verb == "version":
        return Result(0, render.version())
    if verb == "new":
        return cmd_new(args, environ, saved)
    if verb == "deal":
        return cmd_deal(args, environ)
    if verb == "stats":
        return cmd_stats(environ)
    if verb == "doctor":
        return cmd_doctor(environ)
    return game_verb(verb, args, environ, saved)


def unknown_verb(argv):
    raw = argv[0]
    word = raw.lower()
    if word in REFUSED_VERBS:
        return _error(UsageError("unknown command '%s': %s" % (word, REFUSED_VERBS[word])), "help")
    if raw and re.match(r"\A[0-9]+\Z", raw, re.ASCII):
        digits = "".join(argv)
        if all(re.match(r"\A[0-9]+\Z", a, re.ASCII) for a in argv) and \
                (all(len(a) == 1 for a in argv) or len(argv) == 1) and \
                1 <= len(digits) <= 6 and all(c in "1234" for c in digits):
            tries = [("test " + " ".join(digits), "")]
        else:
            tries = [("test 2 4 4 1", "example")]
        return _error(UsageError("to ask about a koan, start with test", tries), "help")
    if is_rule_word(raw):
        tries = []
        whole = " ".join(a.lower() for a in argv)
        for cand in (whole, raw.lower()):
            try:
                if len(cand) <= 40 and re.match(r"\A[a-z0-9 _-]+\Z", cand) and parse_rule([cand]):
                    tries = [("check " + cand, "free")]
                    break
            except UsageError:
                continue
        return _error(UsageError("%s is not a command. Rules go after check or guess."
                                 % echo(word), tries), "help")
    msg = "unknown command %s" % echo(raw)
    close = difflib.get_close_matches(word, VERBS, n=1, cutoff=0.6)
    if close:
        msg += ". Did you mean '%s'?" % close[0]
    return _error(UsageError(msg), "help")


# ---------------------------------------------------------------- the game verbs
def _load_game(directory):
    """(save, gs or None, problem) where problem is None, 'unreadable' or 'other'."""
    try:
        save = state.load(directory)
    except BadSave:
        return None, None, "unreadable"
    if save is None or save["game"] is None:
        return save, None, None
    try:
        gs = replay(save["game"])
    except BadSave as e:
        return save, None, "other" if e.other_version else "unreadable"
    return save, gs, None


def _save_problem_result(problem):
    if problem == "other":
        msg = "this puzzle was made by another version of the game. new force starts a new one."
    else:
        msg = ("the save file is unreadable. doctor explains it, new force starts over and keeps "
               "the old file as koan.json.bad.")
    return _error(UsageError(msg), "doctor / new force", code=3)


def _words(args):
    return [p.lower() for a in args for p in _SPLIT.split(a) if p]


def parse_check(args):
    """`check RULE` -> [rule]; `check RULE vs RULE ...` -> 2 to 6 rules. `vs` is no rule word:
    it is split off here, before the rule parser sees anything."""
    words = [p for a in args for p in _SPLIT.split(a) if p]
    if "vs" not in (w.lower() for w in words):
        return [parse_rule(args)]
    parts = [[]]
    for w in words:
        if w.lower() == "vs":
            parts.append([])
        else:
            parts[-1].append(w)
    if len(parts) > MAX_VS:
        raise UsageError("check compares at most %d rules at once, got %d" % (MAX_VS, len(parts)))
    rules = []
    for i, part in enumerate(parts, 1):
        try:
            rules.append(parse_rule(part))
        except UsageError as e:
            raise UsageError("rule %d of %d: %s" % (i, len(parts), e.message), e.tries) from None
    return rules


def parse_hold(args):
    """`hold` -> None (show the theory), `hold off` -> "off", `hold RULE` -> the rule."""
    words = _words(args)
    if not words:
        return None
    if words == ["off"]:
        return "off"
    return parse_rule(args)


def _no_vs(args):
    if "vs" in _words(args):
        raise UsageError("'vs' is only used to compare rules: check RULE vs RULE")


def game_verb(verb, args, environ, saved, preset_error=None):
    parse_error = preset_error
    parsed = None
    if parse_error is None:
        try:
            if verb == "test":
                parsed = parse_test(args)
            elif verb == "check":
                parsed = parse_check(args)
            elif verb == "guess":
                _no_vs(args)
                parsed = parse_rule(args)
            elif verb == "hold":
                _no_vs(args)
                parsed = parse_hold(args)
            elif verb in ("show", "log"):
                parsed = bool(args)
            elif verb == "giveup":
                parsed = bool(args)
        except UsageError as e:
            parse_error = e
    directory = state.save_dir(environ)
    if (directory / state.SAVE_NAME).exists():
        with state.locked(directory):
            return _game_verb_locked(verb, parsed, parse_error, directory, environ, saved)
    return _game_verb_locked(verb, parsed, parse_error, directory, environ, saved)


def _game_verb_locked(verb, parsed, parse_error, directory, environ, saved):
    save, gs, problem = _load_game(directory)
    playing = gs is not None and gs.status == "playing"
    if parse_error is not None:
        return _error(parse_error, render.PLAYING_NEXT if playing else "new")
    if problem:
        return _save_problem_result(problem)
    if gs is None:
        return _error(NoGame(), "new", code=3)
    next_text = render.PLAYING_NEXT if playing else "new"
    try:
        if verb == "show":
            return Result(0, render.show(gs, parsed))
        if verb == "log":
            return Result(0, render.log(gs, parsed))
        if verb == "test":
            lines = do_test(gs, parsed)
            if any(not l.repeat for l in lines):
                state.write_atomic(directory, save, saved)
            return Result(0, render.test_result(gs, lines))
        if verb == "check":
            conflicts = [do_check(gs, rule) for rule in parsed]
            if len(parsed) > 1:
                return Result(0, render.check_vs_result(gs, parsed, conflicts))
            rule = parsed[0]
            return Result(0, render.check_result(gs, rule, rule_mask(rule), conflicts[0]))
        if verb == "hold":
            return _hold(gs, save, parsed, directory, saved)
        if verb == "guess":
            return _guess(gs, save, parsed, directory, environ, saved)
        return _giveup(gs, save, parsed, directory, environ, saved)
    except Refused as e:
        return _refused(e.message, e.next or next_text, e.keyword)


def _hold(gs, save, parsed, directory, saved):
    if parsed is None:
        return Result(0, render.hold_show(gs, held(gs)))
    if parsed == "off":
        theory = do_hold_off(gs)
        state.write_atomic(directory, save, saved)
        return Result(0, render.hold_off_result(gs, theory))
    conflicts = do_hold(gs, parsed)
    if conflicts:   # refused like a bounced guess: exit 1, nothing written
        return Result(1, render.hold_result(gs, parsed, rule_mask(parsed), conflicts))
    state.write_atomic(directory, save, saved)
    return Result(0, render.hold_result(gs, parsed, rule_mask(parsed), []))


def _end_card(gs, save, guess_rule, outcome, directory, environ, saved, ended):
    state.upsert_history(save, history_entry(gs, outcome, ended))
    state.write_atomic(directory, save, saved)
    p = gs.puzzle
    return render.reveal(gs, guess_rule, solve(p), crux(p), escape(p), save["history"],
                         apprentice_hunt(p))


def _guess(gs, save, rule, directory, environ, saved):
    board_size = len(gs.board)
    mask = rule_mask(rule)
    verdict = do_guess(gs, rule)    # raises Refused when the puzzle is over
    if verdict.kind == "solved":
        # today() only on the solve path (SPEC 9.5): a bad GAME_TODAY stops this solve with
        # exit 2 before anything is written, and never a bounced or wrong guess (9.6)
        ended = state.today(environ)
        return Result(0, _end_card(gs, save, rule, "solved", directory, environ, saved, ended))
    drawable = None
    if gs.puzzle.tier == OPEN_TIER and verdict.kind != "bounced":
        drawable = open_drawable(mask)     # after a wrong guess only: no free oracle (round 3)
    elif verdict.kind != "bounced" and all(e.mask != mask for e in get_lists()[gs.puzzle.tier]):
        drawable = ("unlisted", gs.puzzle.tier)    # round 9b: the public list, never the secret
    if verdict.kind == "wrong":
        state.write_atomic(directory, save, saved)
        return Result(0, render.guess_result(gs, rule, mask, verdict, board_size, drawable))
    return Result(1, render.guess_result(gs, rule, mask, verdict, board_size, drawable))


def _giveup(gs, save, confirmed, directory, environ, saved):
    if gs.over:
        raise Refused("this puzzle is already over")
    if not confirmed:
        out = render.line("CONFIRM", "giveup ends this puzzle without a score and shows the rule.")
        out += render.line("CONFIRM", "To do it: giveup yes")
        out += render.line("UNCHANGED", "nothing was recorded, no cost")
        out += render.line("NEXT", "giveup yes / test KOAN / check RULE / guess RULE")
        return Result(1, render.finish(out))
    ended = state.today(environ)
    do_giveup(gs)
    return Result(0, _end_card(gs, save, None, "gaveup", directory, environ, saved, ended))


# ---------------------------------------------------------------- new (8.4.3)
def parse_new_options(args):
    toks = []
    for a in args:
        for part in a.split():
            t = part.lower()
            if t.startswith("--") and len(t) > 2 and t[2].isalpha():
                t = t[2:]
            elif t.startswith("-") and len(t) > 1 and t[1].isalpha():
                t = t[1:]
            if "=" in t:
                name, _, value = t.partition("=")
                toks += [name, value] if value else [name]
            else:
                toks.append(t)
    opts = {}
    tries = [("new tier 2", ""), ("new daily", ""), ("new seed 2-04817", "")]

    def once(name):
        if name in opts:
            raise UsageError("option '%s' was given twice" % name, tries)

    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if t == "tier":
            once("tier")
            if nxt not in NUMBER_TIERS:
                raise UsageError("tier must be 1, 2, 3, 4 or 5, got %s"
                                 % ("nothing" if nxt is None else echo(nxt)), tries)
            opts["tier"] = NUMBER_TIERS[nxt]
            i += 2
        elif t in NUMBER_TIERS:
            once("tier")
            opts["tier"] = NUMBER_TIERS[t]
            i += 1
        elif t == "seed":
            once("seed")
            if nxt is None or not SEED.match(nxt):
                raise UsageError("a seed looks like 2-04817 (the tier, a dash, five digits), "
                                 "got %s" % ("nothing" if nxt is None else echo(nxt)), tries)
            opts["seed"] = nxt
            i += 2
        elif t == "daily":
            once("daily")
            if nxt is not None and nxt[:1].isdigit() and len(nxt) > 1 and nxt[:1].isascii():
                if not state.is_date(nxt):
                    raise UsageError("%s is not a date like 2026-10-26" % echo(nxt), tries)
                opts["daily"] = nxt
                i += 2
            else:
                opts["daily"] = None
                i += 1
        elif t == "code":
            once("code")
            label = deal.normalize(nxt) if nxt else None
            if label is None or deal.decode(label) is None:
                raise UsageError("%s is not a deal code. A code looks like D-7K2Q-M9XA-4RTV-E1HJ "
                                 "(its dealer gets it from deal)"
                                 % ("nothing" if nxt is None else echo(nxt)), tries)
            opts["code"] = label
            i += 2
        elif t in ("assisted", "force"):
            once(t)
            opts[t] = True
            i += 1
        else:
            msg = "unknown option %s for new" % echo(t)
            close = difflib.get_close_matches(t, ["tier", "seed", "daily", "code", "assisted",
                                                  "force"],
                                              n=1, cutoff=0.6)
            if close:
                msg += ". Did you mean '%s'?" % close[0]
            raise UsageError(msg, tries)
    if len([k for k in ("seed", "daily", "code") if k in opts]) > 1:
        raise UsageError("choose one of seed, daily and code", tries)
    if "code" in opts and opts.get("tier", 5) != 5:
        raise UsageError("a dealt puzzle is always tier 5", tries)
    if "daily" in opts and opts.get("tier", 3) != 3:
        raise UsageError("the daily puzzle is always tier 3", tries)
    if "seed" in opts and "tier" in opts and int(opts["seed"][0]) != opts["tier"]:
        raise UsageError("seed %s is a tier %s seed but you asked for tier %d"
                         % (opts["seed"], opts["seed"][0], opts["tier"]), tries)
    return opts


def cmd_new(args, environ, saved):
    try:
        opts = parse_new_options(args)
        today = state.today(environ)
    except UsageError as e:
        return _error(e, "new")
    directory, moved = state.writable_dir(environ)
    with state.locked(directory):
        save, gs, problem = _load_game(directory)
        force = opts.get("force", False)
        if problem and not force:
            return _save_problem_result(problem)
        if problem == "unreadable":
            os.replace(directory / state.SAVE_NAME, directory / "koan.json.bad")
            save = state.empty_save()
        elif problem == "other":
            save["game"] = None          # dropped, not recorded; the history is kept
        if save is None:
            save = state.empty_save()
        if gs is not None and gs.status == "playing" and gs.game["moves"]:
            if not force:
                return _refused("a puzzle is in progress (%s). Finish it, giveup yes, or new "
                                "force." % render.plural(len(gs.game["moves"]), "move"),
                                "show / giveup yes / new force")
            state.upsert_history(save, history_entry(gs, "abandoned", today))
        if "code" in opts:
            tier = OPEN_TIER
        elif "seed" in opts:
            tier = int(opts["seed"][0])
        elif "tier" in opts:
            tier = opts["tier"]
        elif "daily" in opts:
            tier = 3
        elif save["game"] is not None:
            tier = save["game"]["tier"]
        elif save["history"]:
            tier = save["history"][-1]["tier"]
        else:
            tier = 1
        if "code" in opts:
            label = opts["code"]
        elif "seed" in opts:
            label = opts["seed"]
        elif "daily" in opts:
            label = "daily-" + (opts["daily"] or today)
        else:
            label = _fresh_label(tier, save)
        is_replay = any(h["label"] == label for h in save["history"])
        save["counter"] += 1
        save["game"] = {"assisted": opts.get("assisted", False), "gen": GEN_VERSION,
                        "id": save["counter"], "label": label, "moves": [], "replay": is_replay,
                        "started": today, "status": "playing", "tier": tier}
        new_gs = replay(save["game"])
        state.write_atomic(directory, save, saved)
        return Result(0, render.new_game(new_gs, is_replay, directory if moved else None))


def _fresh_label(tier, save):
    """A random seed for `new`, redrawn (up to 20 times) so the hidden rule differs from the
    previous game's, which is re-derived from its stored label (nothing new is stored)."""
    prev = save["game"] or (save["history"][-1] if save["history"] else None)
    # masks, not texts: the game judges rules by their answers, and in tier 5 one rule can be
    # drawn under two wordings (its halves in either order)
    prev_mask = draw_rule(prev["tier"], prev["label"]).mask if prev else None
    for _ in range(20):
        label = "%d-%05d" % (tier, int.from_bytes(os.urandom(8), "big") % 100000)
        if draw_rule(tier, label).mask != prev_mask:
            break
    return label


# ---------------------------------------------------------------- deal (round 7)
DEAL_TRIES = [("deal has 2 and has 4", "the game picks the start"),
              ("deal has 2 and has 4 board 2 4 and 4 2 and 1 and 3 3", "your own board")]


def parse_deal(args):
    """`deal RULE` -> (rule, None); `deal RULE board KOAN and KOAN and KOAN and KOAN` ->
    (rule, [4 koan indices]). `board` is no rule word (`start` is: it means starts)."""
    words = [p for a in args for p in _SPLIT.split(a) if p]
    lower = [w.lower() for w in words]
    if not words:
        raise UsageError("deal needs a rule, and if you like a board of four koans", DEAL_TRIES)
    _no_vs(args)
    koans = None
    if "board" in lower:
        i = lower.index("board")
        words, koan_words = words[:i], words[i + 1:]
        if not koan_words:
            raise UsageError("board needs four koans: two your rule says YES to, two NO",
                             DEAL_TRIES[1:])
        items = parse_test(koan_words)
        if any(p is not None for _, p in items):
            raise UsageError("a board takes koans only, without yes or no: your rule gives the "
                             "answers", DEAL_TRIES[1:])
        koans = [INDEX[t] for t, _ in items]
        if len(koans) != 4 or len(set(koans)) != 4:
            raise UsageError("a board has four different koans, got %s"
                             % render.plural(len(set(koans)), "different koan"), DEAL_TRIES[1:])
        if not words:
            raise UsageError("deal needs a rule before board", DEAL_TRIES)
    return parse_rule(words), koans


def cmd_deal(args, environ):
    """`deal`: judge the rule, set the start, let the apprentice hunt, record the deal. It never
    changes the puzzle in progress. Round 12: while a tier-5 puzzle is in play (assisted games
    excepted) the chair is closed, because the verdict would be a free drawability check."""
    directory = state.save_dir(environ)
    if (directory / state.SAVE_NAME).exists():
        with state.locked(directory):
            save, gs, _ = _load_game(directory)
        if (gs is not None and gs.status == "playing" and gs.puzzle.tier == OPEN_TIER
                and not save["game"]["assisted"]):
            return Result(1, render.deal_closed())
    try:
        rule, koans = parse_deal(args)
        today = state.today(environ)
    except UsageError as e:
        return _error(e, render.DEAL_NEXT)
    mask = rule_mask(rule)
    drawable = open_drawable(mask)
    sp = apprentice.space()
    if drawable[0] == "no":
        return Result(1, render.deal_unfair(rule, drawable, apprentice.nearby(rule)))
    r = sp.rules[sp.index[mask]]
    if koans is None:     # the game's start, as for a tier-5 puzzle: same rule, same start
        yes, no, trap, _, _ = starters(OPEN_TIER, "deal " + r.text, mask)
    else:
        yes = sorted(q for q in koans if (mask >> q) & 1)
        no = sorted(q for q in koans if not (mask >> q) & 1)
        trap = None
        if len(yes) != 2:
            return Result(1, render.deal_unbalanced(rule, koans, mask))
    hunt = apprentice.hunt(mask, yes, no)
    code = deal.encode(r.kind, r.a, r.b, list(yes) + list(no))
    directory, _ = state.writable_dir(environ)
    with state.locked(directory):
        deals = deal.record_deal(directory, code, hunt.score, today)
    return Result(0, render.deal_result(rule, drawable, yes, no, trap, hunt, len(sp.rules),
                                        deals, code, sp.wordings[mask]))


# ---------------------------------------------------------------- read-only extras
def cmd_intro(environ):
    directory = state.save_dir(environ)
    if not (directory / state.SAVE_NAME).exists():
        return Result(0, render.intro(_game_path(), texts, here=_run_here()))
    with state.locked(directory):
        save, gs, problem = _load_game(directory)
    if gs is not None and gs.status == "playing":
        return Result(0, render.show(gs, False))
    return Result(0, render.intro(_game_path(), texts, gs, problem, _run_here()))


def cmd_stats(environ):
    directory = state.save_dir(environ)
    if not (directory / state.SAVE_NAME).exists():
        return Result(0, render.stats([], deal.load_deals(directory)))
    with state.locked(directory):
        try:
            save = state.load(directory)
        except BadSave:
            return _save_problem_result("unreadable")
        deals = deal.load_deals(directory)
    return Result(0, render.stats(save["history"], deals))


def cmd_doctor(environ):
    directory = state.save_dir(environ)
    path = directory / state.SAVE_NAME
    out = render.line("DOCTOR", render.version_line())
    out += render.raw_line("PATH", '"%s"' % render.scrub_path(_game_path()))
    if path.exists():
        how = "exists, writable" if os.access(path, os.W_OK) else "exists, NOT writable"
    elif state.can_write(directory):
        how = "absent (new creates it)"
    elif environ.get("GAME_HOME"):
        how = "absent, folder NOT writable: set GAME_HOME to a folder you can write"
    else:     # round 13: new will fall back to the temp folder
        fallback = state.fallback_dir()
        if fallback is not None:
            directory, path = fallback, fallback / state.SAVE_NAME
        how = "absent (new creates it here: the game's folder cannot be written)"
    if environ.get("GAME_HOME"):
        how += "   (from GAME_HOME)"
    out += render.raw_line("SAVE", '"%s"   %s' % (render.scrub_path(directory), how))
    playing = False
    if not path.exists():
        st = "no save"
    else:
        with state.locked(directory):
            try:
                save = state.load(directory)
            except BadSave as e:
                save, st = None, "unreadable: %s" % e.reason
            if save is not None:
                if save["game"] is None:
                    st = "no puzzle"
                else:
                    try:
                        gs = replay(save["game"])
                    except BadSave as e:
                        st = "unreadable: %s" % e.reason
                    else:
                        playing = gs.status == "playing"
                        st = "%s, %s" % (render.game_fields(gs.game, gs.status),
                                         render.plural(len(gs.game["moves"]), "move"))
    out += render.line("STATE", st)
    failures = verify_lists()
    if failures:
        out += render.line("LISTS", "FAILED: " + ", ".join(failures))
    else:
        out += render.line("LISTS", "ok: %s rules, all fairness checks pass"
                           % " ".join(str(SIZES[t]) for t in (1, 2, 3, 4)))
    out += render.line("OUTPUT", "ascii, LF")
    out += render.line("NEXT", "show" if playing else "new")
    return Result(0, render.finish(out))


# ---------------------------------------------------------------- process entry
def emit(stream, text):
    try:
        stream.write(text)
        stream.flush()
    except OSError:   # a closed pipe (Windows raises EINVAL, not BrokenPipeError)
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError, io.UnsupportedOperation):
            pass


def main(argv, stdout=None, environ=None):
    environ = os.environ if environ is None else environ
    if stdout is None:
        for s in (sys.stdout, sys.stderr):
            try:
                s.reconfigure(encoding="ascii", errors="backslashreplace", newline="\n")
            except (AttributeError, ValueError):
                pass
        stdout = sys.stdout
    if stdout is None:     # round 13: no stdout at all (`>&-`): play the move, print nothing
        stdout = io.StringIO()
    saved = [False]
    try:
        result = run(split_quoted(argv), environ, saved)
    except KeyboardInterrupt:
        lines = render.line("INTERRUPT", "stopped")
        lines += render.line("INTERRUPT", "your move was saved, see show" if saved[0]
                             else "nothing was saved")
        result = Result(130, render.finish(lines))
    except Busy:
        result = _error(UsageError("the game is busy (another command is running). Run the "
                                   "same command again."), "show", code=5)
    except UsageError as e:   # e.g. GAME_TODAY malformed, found late
        result = _error(e, "help")
    except state.SaveDirError as e:    # round 13
        result = _save_dir_result(e)
    except OSError as e:    # round 13: a save folder that could be made but not written
        if saved[0] or e.errno not in _DIR_ERRNOS:
            if environ.get("GAME_DEBUG") == "1":
                traceback.print_exc(file=sys.stderr)
            result = Result(4, render.internal(type(e).__name__, saved[0]))
        else:
            result = _save_dir_result(state.SaveDirError(state.save_dir(environ),
                                                         bool(environ.get("GAME_HOME"))))
    except Exception as e:   # noqa: BLE001 - anything else is an internal error (exit 4)
        if environ.get("GAME_DEBUG") == "1":
            traceback.print_exc(file=sys.stderr)
        result = Result(4, render.internal(type(e).__name__, saved[0]))
    emit(stdout, result.text)
    return result.code
