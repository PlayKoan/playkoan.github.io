"""The rule language of `check` and `guess` (SPEC 4): lexer, parser, printing, evaluation.

A rule is (op, literals) with op in {"single", "and", "or"}; a literal is (neg, atom).
Every failure is a UsageError whose message and TRY lines follow SPEC 4.5 and 4.6 exactly.
"""

import collections
import difflib
import re

from .atoms import (CMPS, DIGIT_KINDS, KMAX, QUANTS, atom_gloss, atom_mask, atom_text,
                    holds_atom, not_gloss)
from .errors import UsageError, echo, shorten
from .universe import FULL

MAX_TOKENS = 40
MAX_LITERALS = 4

Tok = collections.namedtuple("Tok", "text numword")

_SPLIT = re.compile(r"[ \t\n\r\f\v]+")
_TOKEN_OK = re.compile(r"\A[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*\Z", re.ASCII)
_DIGITS = re.compile(r"\A[0-9]+\Z", re.ASCII)
_SYMBOL_TOKENS = frozenset(">= => <= =< = == != > < & && | || ! ~".split())

NUMBER_WORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                "six": "6", "ones": "1", "twos": "2", "threes": "3", "fours": "4",
                "1s": "1", "2s": "2", "3s": "3", "4s": "4"}
FILLERS = frozenset("a an the is are of with digit digits".split())
PHRASES3 = {("first", "equals", "last"): ("same", "ends"),
            ("first", "equal", "last"): ("same", "ends"),
            ("last", "equals", "first"): ("same", "ends"),
            ("last", "equal", "first"): ("same", "ends")}
PHRASES2 = {("at", "least"): ("atleast",), ("at", "most"): ("atmost",),
            ("non", "decreasing"): ("nondecreasing",), ("non", "increasing"): ("nonincreasing",),
            ("adjacent", "repeats"): ("adjacent", "repeat"),
            ("adjacent", "repeated"): ("adjacent", "repeat"),
            ("all", "distinct"): ("all", "different")}
SYNONYMS = {"contains": ("has",), "contain": ("has",), "includes": ("has",), "include": ("has",),
            "start": ("starts",), "begins": ("starts",), "begin": ("starts",), "end": ("ends",),
            "sorted": ("nondecreasing",), "distinct": ("all", "different"),
            "palindromic": ("palindrome",), "largest": ("max",), "maximum": ("max",),
            "biggest": ("max",), "smallest": ("min",), "minimum": ("min",), "len": ("length",),
            "but": ("and",)}

GRAMMAR_WORDS = frozenset((
    "not no without has starts ends first last same palindrome nondecreasing nonincreasing "
    "strictly increasing decreasing adjacent repeat all different jump zigzag grouped max min "
    "once above below more than as many count length kinds sum even odd atleast atmost exactly "
    "and or").split())
_PHRASE_WORDS = {w for k in list(PHRASES3) + list(PHRASES2) for w in k}
KNOWN = sorted(GRAMMAR_WORDS | set(SYNONYMS) | _PHRASE_WORDS)
_KNOWN_SET = frozenset(KNOWN)

_NULLARY_WORDS = {"palindrome": "palindrome", "nondecreasing": "nondecreasing",
                  "nonincreasing": "nonincreasing", "jump": "jump", "zigzag": "zigzag",
                  "grouped": "grouped"}
_CANNOT_START = frozenset(("different", "once", "above", "below"))

# ---------------------------------------------------------------- refused words (4.5)
_TIES_UP = [("strictly increasing", "1 1 2 is NO"), ("nondecreasing", "1 1 2 is YES")]
_TIES_DOWN = [("strictly decreasing", "2 1 1 is NO"), ("nonincreasing", "2 1 1 is YES")]
_REFUSALS = [
    ("increasing ascending rising", "'%s' is ambiguous about ties", _TIES_UP),
    ("decreasing descending falling", "'%s' is ambiguous about ties", _TIES_DOWN),
    ("monotone monotonic", "'%s' is not a condition here", [("nondecreasing or nonincreasing", "")]),
    ("double doubled doubles repeat repeats repeated twice pair pairs consecutive",
     "'%s' is ambiguous",
     [("adjacent repeat", "two equal digits side by side"),
      ("not all different", "some digit appears twice anywhere")]),
    ("both", "'%s' is not used. 'not' applies to one condition only.",
     [("has 1 and has 2", "both"), ("not has 1 or not has 2", "not both")]),
    ("unique", "'%s' is ambiguous",
     [("all different", "no digit appears twice"), ("count 3 exactly 1", "exactly one 3")]),
    ("equals equal", "'%s' is only used in 'first equals last'. Amounts use exactly.",
     [("same ends", "first digit equals last"), ("as many 1 as 2", "equal numbers of 1s and 2s"),
      ("count 3 exactly 2", "")]),
    ("fewer less greater over under exceeds larger bigger higher smaller lower",
     "'%s' is not used. Compare end digits with last above first or last below first, and "
     "counts with more D than E.",
     [("last above first", "last digit larger than first"), ("more 2 than 1", "more 2s than 1s"),
      ("count 3 atleast 2", "")]),
    ("than", "'%s' is only used in 'more D than E'", [("more 1 than 2", "")]),
    ("number amount many", "'%s' is not a word here",
     [("count 3 atleast 2", "number of 3s"), ("length atleast 4", "number of digits"),
      ("kinds exactly 2", "number of different digits")]),
    ("if then implies unless iff", "there is no if-then: 'if X then Y' is written 'not X or Y'",
     [("not starts 1 or ends 4", "")]),
    ("either xor", "'%s' is not used: 'or' already means at least one", [("has 1 or has 2", "")]),
    ("neither nor", "'%s' is not used: 'neither X nor Y' is 'not X and not Y'",
     [("not has 1 and not has 2", "")]),
    ("only except else between", "'%s' is not a word here",
     [("not has 4", ""), ("count 3 atleast 1 and count 3 atmost 2", "")]),
    ("follows followed after before next",
     "there is no condition about which digit comes next to which",
     [("adjacent repeat", ""), ("jump", "")]),
]
REFUSED = {}
for _words, _msg, _tries in _REFUSALS:
    for _w in _words.split():
        REFUSED[_w] = (_msg, _tries)


def _check_tries(pairs):
    return [("check " + rule, note) for rule, note in pairs]


def _refusal(word):
    msg, tries = REFUSED[word]
    return UsageError(msg % word if "%s" in msg else msg, _check_tries(tries))


# ---------------------------------------------------------------- lexing (4.1)
def _char_error(raw):
    if raw in _SYMBOL_TOKENS:
        return UsageError("symbols are not used. Write words such as atleast, atmost, exactly, "
                          "and, or, not.", _check_tries([("count 3 atleast 2", "")]))
    if any(c in raw for c in "()[]{}"):
        return UsageError("there are no brackets. 'not' applies to one condition.",
                          _check_tries([("not has 1 or not has 2", "means not (has 1 and has 2)")]))
    if "," in raw:
        return UsageError("do not use commas. Join conditions with and / or.",
                          _check_tries([("has 1 and has 2", "")]))
    return UsageError("%s has a character other than a-z or 0-9" % echo(raw))


def lex(args):
    """argv elements -> list of Tok (SPEC 4.1 steps 2-10)."""
    raw = [p for a in args for p in _SPLIT.split(a) if p]
    if not raw:
        raise UsageError("the rule is empty")
    for r in raw:
        if not _TOKEN_OK.match(r):
            raise _char_error(r)
    words = [w for r in raw for w in re.split(r"[-_]", r.lower())]
    if len(words) > MAX_TOKENS:
        raise UsageError("rule is too long (%d words, limit %d)" % (len(words), MAX_TOKENS))
    toks = [Tok(NUMBER_WORDS[w], True) if w in NUMBER_WORDS else Tok(w, False) for w in words]
    toks = [t for t in toks if t.text not in FILLERS]
    out = []
    i = 0
    while i < len(toks):
        key3 = tuple(t.text for t in toks[i:i + 3])
        key2 = key3[:2]
        if len(key3) == 3 and key3 in PHRASES3:
            out += [Tok(w, False) for w in PHRASES3[key3]]
            i += 3
        elif len(key2) == 2 and key2 in PHRASES2:
            out += [Tok(w, False) for w in PHRASES2[key2]]
            i += 2
        else:
            out.append(toks[i])
            i += 1
    final = []
    for t in out:
        if t.text in SYNONYMS:
            final += [Tok(w, False) for w in SYNONYMS[t.text]]
        else:
            final.append(t)
    return final


# ---------------------------------------------------------------- parsing (4.2, 4.6)
def _is_digits(text):
    return text is not None and _DIGITS.match(text) is not None


class _Parser:
    def __init__(self, toks):
        self.toks = toks
        self.words = [t.text for t in toks]
        self.pos = 0
        self.lits = []

    # -- helpers
    def peek(self, k=0):
        i = self.pos + k
        return self.words[i] if i < len(self.words) else None

    def take(self):
        w = self.words[self.pos]
        self.pos += 1
        return w

    def where(self):
        if self.pos == 0:
            return "at the start"
        return "after '%s'" % " ".join(shorten(w) for w in self.words[:self.pos])

    def got(self, w):
        return "nothing" if w is None else echo(w)

    def fail(self, error):
        """Raise the 4.5 refusal if the offending token is a refused word, else `error`."""
        w = self.peek()
        if w in REFUSED:
            raise _refusal(w)
        raise error

    # -- rule
    def parse(self):
        joined = set(self.words)
        if "and" in joined and "or" in joined:
            raise self._mixed()
        self.lits.append(self.literal())
        op = "single"
        while self.peek() is not None:
            w = self.peek()
            if w not in ("and", "or"):
                self.fail(UsageError("expected 'and', 'or' or the end of the rule %s, got %s"
                                     % (self.where(), self.got(w))))
            op = w
            self.take()
            if len(self.lits) == MAX_LITERALS:
                raise UsageError("at most 4 conditions in one rule")
            self.lits.append(self.literal())
        return (op, tuple(self.lits))

    def _mixed(self):
        segs, cur = [], []
        for w in self.words:
            if w in ("and", "or"):
                segs.append(cur)
                cur = []
            else:
                cur.append(w)
        segs.append(cur)
        texts = [" ".join(s) for s in segs]
        tries = []
        for conn, note, fallback in (("and", "all must hold", "X and Y and Z"),
                                     ("or", "at least one holds", "X or Y or Z")):
            cand = (" %s " % conn).join(texts)
            tries.append((_valid_try(cand) or fallback, note))
        return UsageError("this rule mixes 'and' with 'or'. There are no brackets, so that is "
                          "ambiguous. Use one kind of connective.", _check_tries(tries))

    # -- literal
    def literal(self):
        w = self.peek()
        if w is None or w in ("and", "or"):
            raise UsageError("a condition is missing %s" % self.where())
        if w == "not":
            self.take()
            neg, atom = self.literal()
            return (not neg, atom)
        if w == "no":
            self.take()
            nxt = self.peek()
            if _is_digits(nxt):
                return (True, ("has", (self.digit("no"),)))
            neg, atom = self.literal()
            return (not neg, atom)
        if w == "without":
            self.take()
            return (True, ("has", (self.digit("without"),)))
        return self.atom()

    # -- atom (returns a literal, because `has no D` is negative)
    def atom(self):
        w = self.peek()
        if w in REFUSED:
            raise _refusal(w)
        if _is_digits(w):
            raise self._bare_number(w)
        if w == "has":
            self.take()
            if self.peek() == "no":
                self.take()
                return (True, ("has", (self.digit("has no"),)))
            return (False, self.digit_atom("has", "has"))
        if w in ("starts", "ends"):
            self.take()
            return (False, self.digit_atom(w, w))
        if w in ("first", "last"):
            return (False, self.end_atom(w))
        if w == "same":
            self.take()
            if self.peek() != "ends":
                self.fail(UsageError("'same' is only used in 'same ends'",
                                     _check_tries([("same ends", "")])))
            self.take()
            return (False, ("same ends", ()))
        if w in _NULLARY_WORDS:
            self.take()
            return (False, (_NULLARY_WORDS[w], ()))
        if w == "strictly":
            self.take()
            nxt = self.peek()
            if nxt not in ("increasing", "decreasing"):
                self.fail(UsageError("'strictly' must be followed by increasing or decreasing"))
            self.take()
            return (False, ("strictly " + nxt, ()))
        if w in ("adjacent", "all"):
            self.take()
            want = "repeat" if w == "adjacent" else "different"
            if self.peek() != want:
                self.fail(UsageError("'%s' is only used in '%s %s'" % (w, w, want)))
            self.take()
            return (False, (w + " " + want, ()))
        if w in ("max", "min"):
            self.take()
            if self.peek() != "once":
                self.fail(UsageError("'%s' is only used in first is %s, last is %s and %s once"
                                     % (w, w, w, w)))
            self.take()
            return (False, (w + " once", ()))
        if w == "more":
            return (False, self.more_atom())
        if w == "as":
            return (False, self.as_many_atom())
        if w == "count":
            self.take()
            d = self.digit("count")
            return (False, self.quantity("count", (d,), "count %d" % d))
        if w in ("length", "kinds", "sum"):
            self.take()
            return (False, self.quantity(w, (), w))
        if w in ("even", "odd"):
            nxt = self.peek(1)
            if nxt in ("count", "length", "kinds", "sum"):
                self.take()
                self.take()
                if nxt == "count":
                    d = self.digit("count")
                    return (False, ("count", (d, w, None)))
                return (False, (nxt, (w, None)))
            raise self._cmp_misplaced(w)
        if w in ("atleast", "atmost", "exactly"):
            raise self._cmp_misplaced(w)
        if w in _CANNOT_START or w in _KNOWN_SET:
            # C22: a known word that cannot start a condition (`different`, `once`, `least`...)
            raise UsageError("'%s' cannot start a condition %s" % (w, self.where()))
        msg = "unknown word %s" % echo(w)
        close = difflib.get_close_matches(w, KNOWN, n=1, cutoff=0.75)
        if close:
            msg += ". Did you mean '%s'?" % close[0]
        raise UsageError(msg)

    def _cmp_misplaced(self, w):
        example = "length %s 3" % w if w in ("atleast", "atmost", "exactly") else "length %s" % w
        return UsageError("'%s' must come after count D, length, kinds or sum" % w,
                          _check_tries([(example, "")]))

    # -- digits
    def digit(self, word):
        """Consume one digit 1-4 after `word` (has, has no, starts, ends, without, no, count)."""
        w = self.peek()
        if not _is_digits(w):
            self.fail(UsageError("'%s' needs one digit 1-4 %s, got %s"
                                 % (word, self.where(), self.got(w))))
        if len(w) == 1:
            if w not in "1234":
                raise UsageError("koans use only the digits 1 2 3 4, got %s" % echo(w))
            self.take()
            return int(w)
        raise UsageError("expected one digit 1-4 %s, got %s" % (self.where(), echo(w)))

    def digit_atom(self, word, kind):
        """`has D`, `starts D`, `ends D` (kind canonical, word as typed); checks C2."""
        tok = self.toks[self.pos] if self.pos < len(self.toks) else None
        w = self.peek()
        if _is_digits(w) and len(w) == 1:
            nxt = self.peek(1)
            if tok.numword and kind == "has" and _is_digits(nxt) and len(nxt) == 1 \
                    and nxt in "1234":
                raise self._takes_one(word, kind, [w, nxt], numword=True)
            if w in "1234" and _is_digits(nxt) and len(nxt) == 1 and nxt in "1234":
                ds = [w]
                k = 1
                while _is_digits(self.peek(k)) and len(self.peek(k)) == 1 \
                        and self.peek(k) in "1234":
                    ds.append(self.peek(k))
                    k += 1
                raise self._takes_one(word, kind, ds, numword=tok.numword)
        elif _is_digits(w) and 2 <= len(w) <= 4 and all(c in "1234" for c in w):
            raise self._takes_one(word, kind, list(w), numword=False)
        d = self.digit(word)
        return (kind, (d,))

    def _takes_one(self, word, kind, ds, numword):
        msg = "'%s' takes exactly one digit, got '%s'" % (word, " ".join(ds))
        tries = []
        if numword:
            if kind == "has" and len(ds) == 2:
                d, e = ds
                for cand in ("count %s exactly %s" % (e, d), "count %s atleast %s" % (e, d)):
                    if _valid_try(cand):
                        tries.append((cand, ""))
        else:
            joins = ("and", "or") if kind == "has" else ("or",)
            for conn in joins:
                cand = (" %s " % conn).join("%s %s" % (kind, d) for d in ds)
                if _valid_try(cand):
                    tries.append((cand, ""))
        return UsageError(msg, _check_tries(tries))

    def end_atom(self, w):
        """`first ...` / `last ...`."""
        self.take()
        nxt = self.peek()
        if _is_digits(nxt):
            return self.digit_atom(w, "starts" if w == "first" else "ends")
        if nxt in ("max", "min"):
            self.take()
            return ("%s is %s" % (w, nxt), ())
        if nxt in ("above", "below"):
            self.take()
            other = "last" if w == "first" else "first"
            if self.peek() != other:
                self.fail(UsageError("'%s %s' must be followed by %s %s, got %s"
                                     % (w, nxt, other, self.where(), self.got(self.peek()))))
            self.take()
            if w == "last":
                return ("last %s first" % nxt, ())
            # first below last == last above first; first above last == last below first
            return ("last above first" if nxt == "below" else "last below first", ())
        self.fail(UsageError("'%s' must be followed by a digit 1-4, max, min, above or below "
                             "%s, got %s" % (w, self.where(), self.got(nxt))))

    def more_atom(self):
        self.take()

        def need():
            self.fail(UsageError("'more' is used as more D than E %s, got %s"
                                 % (self.where(), self.got(self.peek())),
                                 _check_tries([("more 1 than 2", "")])))
        if not _is_digits(self.peek()):
            need()
        d = self.digit("more")
        if self.peek() == "than":
            self.take()
        if not _is_digits(self.peek()):
            need()
        e = self.digit("more")
        if d == e:
            raise UsageError("'more' needs two different digits")
        return ("more", (d, e))

    def as_many_atom(self):
        self.take()
        err = UsageError("'as' is only used in 'as many D as E'",
                         _check_tries([("as many 1 as 2", "")]))
        if self.peek() != "many":
            self.fail(err)
        self.take()
        if not _is_digits(self.peek()):
            self.fail(err)
        d = self.digit("as many")
        if self.peek() != "as":
            self.fail(err)
        self.take()
        if not _is_digits(self.peek()):
            self.fail(err)
        e = self.digit("as many")
        if d == e:
            raise UsageError("'as many' needs two different digits")
        return ("as many", (d, e))

    def quantity(self, kind, head, q_text):
        w = self.peek()
        if w in ("atleast", "atmost", "exactly"):
            self.take()
            return (kind, head + (w, self.number(kind, q_text)))
        if w in ("even", "odd"):
            self.take()
            return (kind, head + (w, None))
        if w in REFUSED:
            raise _refusal(w)
        n = 2
        if _is_digits(w) and int(w) <= KMAX[kind]:
            n = int(w)
        tries = []
        for cand in ("%s exactly %d" % (q_text, n), "%s atleast %d" % (q_text, n),
                     "%s even" % q_text):
            if _valid_try(cand):
                tries.append((cand, ""))
        raise UsageError("'%s' must be followed by atleast K, atmost K, exactly K, even or odd, "
                         "got %s" % (q_text, self.got(w)), _check_tries(tries))

    def number(self, kind, q_text):
        w = self.peek()
        top = KMAX[kind]
        if not _is_digits(w):
            self.fail(UsageError("expected a number 0-%d %s, got %s"
                                 % (top, self.where(), self.got(w))))
        k = int(w)
        if k > top:
            raise UsageError("after '%s' use a number from 0 to %d, not %s"
                             % (q_text, top, shorten(str(k))))
        self.take()
        return k

    # -- bare numbers (C13, C14)
    def _bare_number(self, w):
        prev = self.words[self.pos - 1] if self.pos > 0 else None
        if prev in ("and", "or") and self.lits and self.lits[-1][1][0] in DIGIT_KINDS:
            neg, (kind, _) = self.lits[-1]
            prefix = ("not " if neg else "") + kind
            out = []
            for i, x in enumerate(self.words):
                if i > 0 and self.words[i - 1] in ("and", "or") and x in ("1", "2", "3", "4"):
                    out.append(prefix)
                out.append(x)
            cand = _valid_try(" ".join(out)) or "has 3"
            return UsageError("unexpected number %s after '%s'. A number must follow a "
                              "condition word." % (echo(w), prev), _check_tries([(cand, "")]))
        out = []
        for i, x in enumerate(self.words):
            if x in ("1", "2", "3", "4") and (i == 0 or self.words[i - 1] in ("and", "or", "not")):
                out.append("has")
            out.append(x)
        cand = _valid_try(" ".join(out)) or "has 3"
        return UsageError("unexpected number %s. A number must follow a condition word."
                          % echo(w), _check_tries([(cand, ""), ("count 3 atleast 2", "")]))


def _valid_try(text):
    """Return text if it parses and its TRY line fits 78 characters (4.6), else None."""
    if len("check " + text) > 68:
        return None
    try:
        parse_rule([text])
    except UsageError:
        return None
    return text


def parse_rule(args):
    """argv elements (after the verb) -> rule AST. Raises UsageError."""
    p = _Parser(lex(args))
    try:
        return p.parse()
    except UsageError:
        raise
    except Exception:   # safety net (C24): the parser never raises anything else
        w = p.peek()
        raise UsageError("cannot read the rule %s, got %s" % (p.where(), p.got(w))) from None


def parse_canonical(text):
    """Parse a canonical text known to be valid (hidden lists, save file)."""
    return parse_rule(text.split(" "))


# ---------------------------------------------------------------- printing and meaning
def literal_text(lit):
    neg, atom = lit
    return ("not " if neg else "") + atom_text(atom)


def rule_text(rule):
    op, lits = rule
    sep = " and " if op == "and" else " or "
    return sep.join(literal_text(l) for l in lits)


def _literal_gloss(lit, alone):
    """A literal's MEANS text: its gloss, `(gloss)` inside a rule of several, `NOT (gloss)`
    for a not, unless atoms has a positive wording for that not."""
    neg, atom = lit
    gloss = not_gloss(atom) if neg else atom_gloss(atom)
    if gloss is None:
        return "NOT (%s)" % atom_gloss(atom)
    return gloss if alone else "(%s)" % gloss


def rule_gloss(rule):
    op, lits = rule
    if op == "single":
        return _literal_gloss(lits[0], True)
    parts = [_literal_gloss(lit, False) for lit in lits]
    return (" AND " if op == "and" else " OR ").join(parts)


def literal_mask(lit):
    neg, atom = lit
    m = atom_mask(atom)
    return FULL ^ m if neg else m


def rule_mask(rule):
    op, lits = rule
    masks = [literal_mask(l) for l in lits]
    m = masks[0]
    for x in masks[1:]:
        m = (m & x) if op == "and" else (m | x)
    return m


def holds(rule, t):
    """Direct evaluation without masks (used only by the differential test)."""
    op, lits = rule
    vals = [holds_atom(atom, t) != neg for neg, atom in lits]
    if op == "and":
        return all(vals)
    if op == "or":
        return any(vals)
    return vals[0]


def is_rule_word(word):
    """Is `word` a rule word (grammar word, phrase or synonym word, or refused word)?"""
    try:
        toks = lex([word])
    except UsageError:
        return False
    return bool(toks) and (toks[0].text in _KNOWN_SET or toks[0].text in REFUSED)
