"""Koan input for the `test` verb (SPEC 2.5 and the koan-input table of 8.6)."""

import difflib
import re

from .errors import UsageError, echo, shorten
from .lang import GRAMMAR_WORDS, lex, parse_rule

MAX_BATCH = 8
ANSWERS = {"yes": True, "y": True, "no": False, "n": False}

_SPLIT = re.compile(r"[ \t\n\r\f\v]+")
_DIGITS = re.compile(r"\A[0-9]+\Z", re.ASCII)
_COUNT_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}
_EXAMPLE = [("test 3 3 predict no", "")]


def _is_terminal_word(tok):
    try:
        toks = lex([tok])
    except UsageError:
        return False
    return bool(toks) and toks[0].text in GRAMMAR_WORDS


def _bad_token(tok, all_tokens):
    if "," in tok:
        return UsageError("do not use commas: separate digits with spaces and koans with 'and'",
                          [("test 1 2 and 3 4", "")])
    if "/" in tok or ":" in tok:
        return UsageError("the ' / ' on the board only separates koans for reading. In a command, "
                          "separate koans with 'and'.", [("test 1 2 and 3 4", "")])
    if any(c in tok for c in "()[]{}"):
        return UsageError("brackets are not used: separate digits with spaces and koans with 'and'",
                          [("test 1 2 and 3 4", "")])
    if _is_terminal_word(tok):
        rule = " ".join(all_tokens)
        try:
            ok = len(rule) <= 40 and parse_rule([rule]) is not None
        except UsageError:
            ok = False
        return UsageError("test takes koans (digits 1-4), not rule words like %s" % echo(tok),
                          [("check " + (rule if ok else "has 3"), "free")])
    msg = "%s is not a koan digit or 'and' / 'predict'" % echo(tok)
    close = difflib.get_close_matches(tok, ["and", "predict", "yes", "no"], n=1, cutoff=0.6)
    if close:
        msg += ". Did you mean '%s'?" % close[0]
    return UsageError(msg)


def _finish(digit_toks):
    """Turn one item's digit tokens into a koan tuple, or raise."""
    if len(digit_toks) > 1 and any(len(t) > 1 for t in digit_toks):
        spaced = " ".join(" ".join(t) for t in digit_toks)
        tries = []
        if sum(len(t) for t in digit_toks) <= 6:
            tries.append(("test " + spaced, "one koan"))
        if all(len(t) <= 6 for t in digit_toks) and len(digit_toks) <= MAX_BATCH:
            tries.append(("test " + " and ".join(" ".join(t) for t in digit_toks),
                          "%s koans" % _COUNT_WORDS[len(digit_toks)]))
        typed = " ".join(map(shorten, digit_toks))     # a long token as 17 digits and ...
        raise UsageError("'%s' is unclear: write one koan spaced (2 4 4 1) or as one block "
                         "(2441), and put 'and' between koans" % typed, tries)
    digits = "".join(digit_toks)
    if len(digits) > 6:
        raise UsageError("koan '%s' has %d digits. A koan has 1 to 6 digits."
                         % (" ".join(digits), len(digits)))
    return tuple(int(c) for c in digits)


def parse_test(args):
    """argv elements after `test` -> list of (koan tuple, prediction True/False/None)."""
    toks = [p.lower() for a in args for p in _SPLIT.split(a) if p]
    if not toks:
        raise UsageError("test needs at least one koan",
                         [("test 2 4 4 1", ""), ("test 1 2 and 3 3", "two koans")])
    items = []
    cur, pred = [], None
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok == "and":
            if not cur:
                raise UsageError("a koan is missing before 'and'", [("test 1 2 and 3 3", "")])
            items.append((_finish(cur), pred))
            cur, pred = [], None
        elif tok == "predict":
            if not cur:
                raise UsageError("'predict' must come after a koan", _EXAMPLE)
            if pred is not None:
                raise UsageError("only one prediction per koan", _EXAMPLE)
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if nxt not in ANSWERS:
                raise UsageError("'predict' must be followed by yes or no, got %s"
                                 % ("nothing" if nxt is None else echo(nxt)), _EXAMPLE)
            pred = ANSWERS[nxt]
            i += 1
        elif _DIGITS.match(tok):
            if pred is not None:
                raise UsageError("put 'and' between koans. 'predict' ends a koan.",
                                 [("test 1 2 predict yes and 3 3", "")])
            bad = sorted(c for c in set(tok) if c not in "1234")
            if bad:
                raise UsageError("%s uses the digit %s. Koans use only the digits 1 2 3 4."
                                 % (echo(tok), bad[0]))
            cur.append(tok)
        elif tok in ANSWERS:
            raise UsageError("to record a prediction write 'predict %s' after the koan" % tok,
                             _EXAMPLE)
        else:
            raise _bad_token(tok, toks)
        i += 1
    if not cur:
        if not items:
            raise UsageError("test needs at least one koan",
                             [("test 2 4 4 1", ""), ("test 1 2 and 3 3", "two koans")])
        raise UsageError("a koan is missing at the end", [("test 1 2 and 3 3", "")])
    items.append((_finish(cur), pred))
    if len(items) > MAX_BATCH:
        raise UsageError("at most 8 koans per test command (you gave %d)" % len(items))
    return items
