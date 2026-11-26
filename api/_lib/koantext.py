"""KOAN play by link: the whole game as plain-text pages, one address per move.

An agent that can only open web addresses plays a full game. The address holds the game:
the puzzle label, then one path segment per move, words joined by '-':

    /play                                   the front door: today's three puzzles
    /play/1-12345                           a puzzle's first page
    /play/1-12345/test-11/guess-adjacent-repeat
    /play/1-12345/test-11/more-2            a free sub-page (changes nothing)
    /play/fits/1-12345/test-11              the same game, listing the rules that still fit

The query form works too: /play?p=1-12345&m=test+11,guess+adjacent+repeat&s=more-2

Nothing is stored. Every page replays the game from its address with the real engine
(engine/cli.py, unchanged), keeping the save file in memory for the length of one call.
handle(target, play_url, page_url) is the one entry point; host/server.py and
site-src/api/play.py only move bytes.
"""

import contextlib
import copy
import io
import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

HERE = Path(__file__).resolve().parent
try:                                        # deployed: engine/ sits next to this file
    import engine                           # noqa: F401
except ImportError:                         # in the project: engine/ is one folder up
    sys.path.insert(0, str(HERE.parent))

from engine import cli, state                               # noqa: E402
from engine.lang import parse_rule                          # noqa: E402
from engine.play import replay                              # noqa: E402
from engine.prng import seed_from                           # noqa: E402
from engine.tiers import get_lists                          # noqa: E402
from engine.universe import U, koan_str                     # noqa: E402

MAX_MOVES = 60            # the engine caps a game at 40 tests + 10 wrong guesses + 12 holds
MAX_MOVE_CHARS = 200
MAX_MOVES_CHARS = 3000    # a full 40-test, 10-guess game is about 1,000
MAX_TARGET = 4000         # the whole request target, path and query
GAME_VERBS = ("test", "check", "hold", "guess", "show", "log", "giveup")
LAST_ONLY = ("rules", "help")                 # allowed only as the last move
READ_ONLY = {"check", "show", "log", "rules", "help"}   # skipped unless they are last
SUB_WORDS = ("more", "state", "draft", "pick")
FIXED_TODAY = "2026-11-14"   # the engine's date for seeds and codes: pages never use a clock

# FITS (round 14): pages that list the tier's rules still fitting the board. It is a choice
# carried in the address, right after /play: /play/fits/1-12345/11. FITS_DEFAULT is what an
# address without the word gets; the word for the default is read but never printed.
FITS_DEFAULT = False
FITS_WORDS = {"fits": True, "nofits": False}
FITS_QUERY = {"on": True, "off": False}       # the query form: ?p=1-12345&fits=on
FITS_INLINE = 3              # this few fitting rules are linked on the game page itself
LISTED_TIERS = (1, 2, 3, 4)  # tier 5 and dealt puzzles have no list to count

SEED = re.compile(r"\A[1-5]-[0-9]{5}\Z")
DATE = re.compile(r"\A(?:daily-)?(\d{4}-\d{2}-\d{2})\Z")
WORD_SPLIT = re.compile(r"[\s+_\-]+")
BARE_TEST = re.compile(r"\A[1-4]{1,6}\Z")     # a part of digits alone is a test: /1234

CACHE_PAGE = "public, max-age=3600, s-maxage=86400"
CACHE_DOOR = "public, max-age=60, s-maxage=300"
CACHE_ERROR = "public, max-age=60, s-maxage=60"

_LOCK = threading.Lock()      # the save functions are swapped module-wide: one game at a time
_HOME = []                    # one real, empty folder for GAME_HOME (nothing is written there)


class BadAddress(Exception):
    """An address the server cannot read. `back` is the address to offer instead."""

    def __init__(self, message, back=None):
        Exception.__init__(self, message)
        self.back = back


# ---------------------------------------------------------------- configuration
def load_config(environ=None):
    """(page_url, play_url) from the environment, else site.config.json at the project
    root. A missing value is ''. Neither ends in '/'."""
    environ = os.environ if environ is None else environ
    cfg = {}
    for path in (HERE.parent / "site.config.json", HERE / "site.config.json"):
        if path.is_file():
            try:
                cfg = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cfg = {}
            break
    if not isinstance(cfg, dict):
        cfg = {}

    def pick(env_name, key):
        value = environ.get(env_name) or cfg.get(key) or ""
        value = value.strip() if isinstance(value, str) else ""
        return value.rstrip("/") if re.match(r"\Ahttps?://[^\s]+\Z", value) else ""

    return pick("PAGE_URL", "page_url"), pick("PLAY_URL", "play_url")


def today_utc():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def todays_seeds(date):
    """(easy, medium, hard) labels for a GMT date: the same for every visitor that day."""
    easy = "1-%05d" % (seed_from("KOAN", "host", date, "easy") % 100000)
    medium = "2-%05d" % (seed_from("KOAN", "host", date, "medium") % 100000)
    return easy, medium, "daily-" + date


# ---------------------------------------------------------------- reading an address
def words_of(text):
    return [w for w in WORD_SPLIT.split(text.lower()) if w]


def new_args(label):
    """The engine's `new` command for a label from an address."""
    if SEED.match(label):
        return ["new", "seed", label]
    m = DATE.match(label)
    if m:
        if not state.is_date(m.group(1)):
            raise BadAddress("%s is not a real date" % m.group(1))
        return ["new", "daily", m.group(1)]
    if label.upper().startswith("D-") and len(label) <= 40:
        return ["new", "code", label.upper()]
    raise BadAddress("the puzzle must be a seed like 1-12345, a date like daily-2026-11-14, "
                     "or a deal code like D-WRBD-QM0B-BA50-A67D")


def check_moves(moves, label, play_url):
    """Each move is a list of words. Raise BadAddress on the first bad one."""
    if len(moves) > MAX_MOVES:
        raise BadAddress("at most %d moves in one address" % MAX_MOVES,
                         game_url(play_url, label, moves[:MAX_MOVES]))
    total, fit = 0, 0
    for fit, m in enumerate(moves):
        total += len(" ".join(m)) + 1
        if total > MAX_MOVES_CHARS:
            raise BadAddress("the moves are too long (limit %d characters)" % MAX_MOVES_CHARS,
                             game_url(play_url, label, moves[:fit]))
    for i, m in enumerate(moves):
        back = game_url(play_url, label, moves[:i])
        if len(" ".join(m)) > MAX_MOVE_CHARS:
            raise BadAddress("move %d is too long" % (i + 1), back)
        verb = m[0]
        if verb in GAME_VERBS or (verb in LAST_ONLY and i == len(moves) - 1):
            continue
        raise BadAddress("move %d, %s, is not a move here. Moves start with test, check, "
                         "guess, hold, show, log or giveup" % (i + 1, shown(verb)), back)


def shown(word):
    """A word from an address, safe to print: ASCII letters, digits and '-' only."""
    word = re.sub(r"[^A-Za-z0-9-]", "?", word)
    return "'%s'" % (word[:20] + ("..." if len(word) > 20 else ""))


def parse_target(target):
    """A request target -> ('door'|'how'|'rules'|'game', label, moves, sub, fits).

    moves is a list of word lists; sub is a list of word lists (the free sub-page); fits
    says whether the address asks for the FITS list (FITS_DEFAULT when it does not say)."""
    if len(target) > MAX_TARGET:
        raise BadAddress("the address is too long (limit %d characters)" % MAX_TARGET)
    parts = urlsplit(target)
    query = parse_qs(parts.query, keep_blank_values=True)
    path = unquote(parts.path)
    if "_path" in query:                       # Vercel's rewrite of /play/... to /api/play
        path = "/play/" + query["_path"][0]
    if path.startswith("/api/play"):
        path = "/play" + path[len("/api/play"):]
    if path != "/play" and not path.startswith("/play/"):
        raise BadAddress("this server answers only addresses that start with /play")
    segs = [s for s in re.split(r"[/,]", path[len("/play"):]) if s.strip()]
    fits = FITS_DEFAULT
    if segs and segs[0].strip().lower() in FITS_WORDS:
        fits = FITS_WORDS[segs.pop(0).strip().lower()]
    fits = FITS_QUERY.get(query.get("fits", [""])[0].strip().lower(), fits)
    if query.get("p", [""])[0].strip():
        label = query["p"][0].strip()
        moves = [words_of(m) for m in query.get("m", [""])[0].split(",")]
        subs = re.split(r"[/,]", query.get("s", [""])[0])
        if query.get("d", [""])[0].strip():               # the prototype's draft parameter
            subs = ["draft " + query["d"][0]]
        elif query.get("x", [""])[0].strip():              # the prototype's "more" parameter
            subs = ["more " + query["x"][0]]
        sub = [words_of(s) for s in subs]
    else:
        if not segs:
            return "door", None, [], [], fits
        label = segs[0].strip()
        rest = [words_of(s) for s in segs[1:]]
        cut = next((i for i, w in enumerate(rest) if w and w[0] in SUB_WORDS), len(rest))
        moves, sub = rest[:cut], rest[cut:]
    moves = [["test"] + m if len(m) == 1 and BARE_TEST.match(m[0]) else m for m in moves if m]
    sub = [s for s in sub if s]
    low = label.lower()
    if low in ("how", "rules", "help") and not moves and not sub:
        return ("rules" if low == "rules" else "how"), None, [], [], fits
    if low.startswith("rules-") and not moves and not sub:
        return "rules", seg(words_of(low[len("rules-"):])), [], [], fits
    return "game", label, moves, sub, fits


# ---------------------------------------------------------------- running the engine
@contextlib.contextmanager
def _memory_save(box):
    """Swap the three save-file functions of engine.state for dict-backed ones."""
    real = state.load, state.write_atomic, state.locked

    def load(directory):
        return copy.deepcopy(box[0]) if box[0] is not None else None

    def write_atomic(directory, obj, saved=None):
        box[0] = copy.deepcopy(obj)
        if saved is not None:
            saved[0] = True

    @contextlib.contextmanager
    def locked(directory, timeout=None):
        yield

    state.load, state.write_atomic, state.locked = load, write_atomic, locked
    try:
        yield
    finally:
        state.load, state.write_atomic, state.locked = real


def _home():
    if not _HOME:
        _HOME.append(tempfile.mkdtemp(prefix="koan-host-"))
    return _HOME[0]


def engine_environ(label):
    m = DATE.match(label)
    return {"GAME_HOME": _home(), "GAME_TODAY": m.group(1) if m else FIXED_TODAY}


def run_engine(argvs, environ):
    """Run each argv through cli.main with the save in memory. Returns
    [(exit code, text, game entry after it)]."""
    box = [None]
    outs = []
    with _LOCK, _memory_save(box):
        for argv in argvs:
            buf = io.StringIO()
            code = cli.main(list(argv), stdout=buf, environ=environ)
            game = copy.deepcopy(box[0]["game"]) if box[0] and box[0].get("game") else None
            outs.append((code, buf.getvalue(), game))
    return outs


def play(label, moves, extra=None, assisted=False):
    """Replay a game. Returns a dict:
    text, code   the engine's reply to the last move (or to `new` when there is none)
    game         the save's game entry at the end (None if `new` failed)
    kept         the moves that changed the game, in order (the canonical history)
    extra_text   the reply to `extra` (an argv run after the moves, e.g. ['show']).
    assisted starts the game with the engine's own mark for a game played with help: its
    GAME line says so and a solve has no SHARE line."""
    first = new_args(label) + (["assisted"] if assisted else [])
    argvs, owners = [first], [None]
    for i, m in enumerate(moves):
        if m[0] in READ_ONLY and i != len(moves) - 1:
            continue                       # never recorded: no effect on the game
        argvs.append(m)
        owners.append(i)
    if extra:
        argvs.append(extra)
        owners.append("extra")
    outs = run_engine(argvs, engine_environ(label))
    if outs[0][0] != 0 or outs[0][2] is None:
        return {"text": outs[0][1], "code": outs[0][0], "game": None, "kept": [],
                "extra_text": None, "failed_new": True}
    kept, before = [], outs[0][2]
    last = (outs[0][0], outs[0][1])
    for (code, text, game), owner in zip(outs[1:], owners[1:]):
        if owner == "extra":
            continue
        if game != before:
            kept.append(moves[owner])
        before = game
        last = (code, text)
    game = outs[-2][2] if extra else outs[-1][2]
    return {"text": last[1], "code": last[0], "game": game, "kept": kept,
            "extra_text": outs[-1][1] if extra else None, "failed_new": False}


# ---------------------------------------------------------------- text helpers
def hyphen_move(cmd):
    """An engine command such as 'test 1 2 and 3 3' as one address part, 'test-12-and-33'.
    In a test, single digits next to each other make one koan. None if it is not a move."""
    words = cmd.split()
    if not words or words[0] not in GAME_VERBS:
        return None
    if words[0] == "test":
        joined = []
        for w in words[1:]:
            if len(w) == 1 and w.isdigit() and joined and joined[-1].isdigit():
                joined[-1] += w
            else:
                joined.append(w)
        words = ["test"] + joined
    return seg(words)


UNLISTED = re.compile(r"(TIER (\d)) +and tier \2 never draws this rule: see the TIER \2 list "
                      r"in rules$")


def tidy(text, share=None, base=None, rules=None):
    """The engine's reply without the lines that name terminal commands or a save history:
    NEXT, AGAIN and RECORD go; SHARE keeps its score and ends in `share` (a link) instead
    of a `new ...` command. TRY lines become addresses under `base` (dropped without it),
    and the other lines that tell a terminal player what to type say what to open. The
    note that a tier never draws a guessed rule points at `rules`, the tier lists' page."""
    out = []
    for ln in text.rstrip("\n").split("\n"):
        key = ln[:10].strip()
        if key in ("NEXT", "AGAIN", "RECORD"):
            continue
        m = UNLISTED.match(ln)
        if m and rules:
            out += wrap(m.group(1), "Tier %s never hides this rule. The rules it can hide: %s"
                        % (m.group(2), rules), width=78)
            continue
        if key == "SHARE" and share:
            head, sep, _ = ln.rpartition(": new ")
            if sep:
                ln = "%s: %s" % (head, share)
        if key == "TRY":
            move = hyphen_move(re.split(r"\s{2,}", ln[10:].strip())[0])
            if not (move and base):
                continue
            ln = line("TRY", "%s/%s" % (base, move))
        ln = ln.replace("assisted game: scripts are allowed and it is recorded separately",
                        "assisted game: FITS is on, so a solve has no SHARE line")
        ln = ln.replace("Start another with new.", "Open ANOTHER below.")
        ln = ln.replace("To do it: giveup yes", "To do it: open the GIVEUP link below.")
        ln = re.sub(r"python \S+\.py ", "terminal game: ", ln)
        out.append(ln)
    return "\n".join(out)


def line(key, text=""):
    return ("%-10s%s" % (key, text)).rstrip()


def wrap(key, text, width=68):
    """Keyword line(s), the text wrapped at `width` columns, continuation indented."""
    out, cur = [], ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > width - 10:
            out.append(cur)
            cur = w
        else:
            cur = (cur + " " + w) if cur else w
    out.append(cur)
    return [line(key if i == 0 else "", t) for i, t in enumerate(out)]


def seg(words):
    return "-".join(words)


def move_part(m):
    """A move as an address part. A test of one koan is its digits alone ('/1234'), which
    keeps addresses short: every link on a page repeats the whole history."""
    if m and m[0] == "test":
        h = hyphen_move(" ".join(m)) or ""
        if BARE_TEST.match(h[len("test-"):]):
            return h[len("test-"):]
    return seg(m)


def game_url(play_url, label, moves=(), sub=()):
    parts = [play_url, label] + [move_part(m) for m in moves] + [seg(s) for s in sub]
    return "/".join(parts)


def mode_url(root, fits):
    """The front door's address for one FITS choice. Every link on a page is built on it,
    so the choice travels with the game."""
    if fits == FITS_DEFAULT:
        return root
    return "%s/%s" % (root, "fits" if fits else "nofits")


def label_tier(label):
    """The tier a label will start, known before the engine runs (5 for a deal code)."""
    if SEED.match(label):
        return int(label[0])
    return 3 if DATE.match(label) else 5


def fitting(gs):
    """The tier's listed rules that agree with every koan on the board, in list order.
    Each is a `check` that would pass: nothing here is news, only bookkeeping."""
    return [e for e in get_lists()[gs.game["tier"]] if not gs.conflicts(e.mask)]


def switch_line(fits, other):
    """One line offering this same page with the FITS choice the other way round."""
    if fits:
        return line("PLAIN", "the same without the list of rules that still fit: " + other)
    return line("FITS", "also list the rules that still fit: " + other)


def person_link(page_url, play_url, label):
    if page_url:
        if label.startswith("daily-"):
            return "%s#daily=%s" % (page_url, label[len("daily-"):])
        if label.startswith("D-"):
            return "%s#code=%s" % (page_url, label)
        return "%s#seed=%s" % (page_url, label)
    return game_url(play_url, label)


def another_label(label, tier):
    """A different puzzle of the same tier, fixed for this label (pages stay cacheable)."""
    return "%d-%05d" % (tier, seed_from("KOAN", "host", "another", label) % 100000)


HOW = ("A hidden rule says YES or NO to every koan, a row of 1 to 6 digits from 1 to 4. "
       "Find the rule. A test shows one koan's answer: cost 1. A check of a rule is free. "
       "A right guess ends the game; a wrong one costs 3. Low score wins. Rules look like: "
       "has 3, ends 2 or ends 4, has 1 and not ends 3, not palindrome. A rule may use and, "
       "or use or, but not both.")
VISIT = ("Play by opening addresses. Each page lists the next ones; each is this game plus "
         "one move. Nothing is stored. Pages marked free change nothing.")
HONOR = "Scores are on the honor system: any address can be reopened."


def reminder(play_url):
    return [line("KOAN", "Find the hidden rule. Test 1, wrong guess 3, check free; low wins."),
            line("", "One address per move. How to play: %s/how" % play_url)]


# ---------------------------------------------------------------- pages
def door_page(play_url, page_url, date, switch=None):
    easy, medium, hard = todays_seeds(date)
    out = wrap("KOAN", "A puzzle game for AI agents and people. " + HOW)
    out += wrap("VISIT", VISIT)
    out.append(line("HONOR", HONOR))
    out += [line("TODAY", "%s (GMT). Pick one puzzle:" % date),
            line("EASY", game_url(play_url, easy)),
            line("MEDIUM", game_url(play_url, medium)),
            line("HARD", game_url(play_url, hard)),
            line("RULES", "the full rules, in parts: %s/rules" % play_url)]
    if switch:
        out.append(switch)
    if page_url:
        out.append(line("PEOPLE", "the same game in a browser: " + page_url))
    out.append(line("NEXT", "open one address above"))
    return "\n".join(out) + "\n"


def how_page(play_url, page_url, switch=None):
    out = wrap("HOW", HOW) + wrap("VISIT", VISIT) + [line("HONOR", HONOR)]
    out += wrap("MOVES", "Each move adds one part to a game's address, words joined by -. "
                "1234 (or test-1234) tests the koan 1 2 3 4; test-12-and-34 tests two. "
                "check-has-2 is free. guess-has-1-and-not-ends-3 guesses. giveup-yes ends "
                "the game. Example: /play/1-12345/11/guess-adjacent-repeat")
    out.append(line("RULES", "the full rules, in parts: %s/rules" % play_url))
    out.append(line("START", "today's puzzles: " + play_url))
    if switch:
        out.append(switch)
    if page_url:
        out.append(line("PEOPLE", page_url))
    out.append(line("NEXT", "open one address above"))
    return "\n".join(out) + "\n"


RULE_PARTS = (       # (address word, what it holds, the engine's keywords that start it)
    ("koans", "what a koan is and what the hidden rule does", ("RULES", "KOANS", "MASTER")),
    ("words", "every condition word, with its meaning", ("WORDS",)),
    ("amounts", "counting words, and joining conditions", ("AMOUNTS", "YOURS")),
    ("tiers", "the full list of rules each tier can hide", ("HIDDEN", "TIER")),
    ("draw", "how a rule is drawn, what each rule promises", ("DRAW", "FAMILIES", "FAIR")),
    ("play", "the start, each move, score, daily, end", ("START",)),
)


def rule_parts():
    """The engine's rules text cut into RULE_PARTS: {word: [lines]}. A keyword that no part
    names stays in the part before it, so new engine sections are never lost."""
    outs = run_engine([["rules"]], {"GAME_HOME": _home(), "GAME_TODAY": FIXED_TODAY})
    starts = {k: word for word, _, keys in RULE_PARTS for k in keys}
    parts, cur = {word: [] for word, _, _ in RULE_PARTS}, RULE_PARTS[0][0]
    for ln in tidy(outs[0][1]).split("\n"):
        key = ln[:10].strip()
        key = "TIER" if key.startswith("TIER ") else key
        cur = starts.get(key, cur)
        parts[cur].append(ln)
    return parts


def _rules_part_page(parts, word, play_url):
    out = parts[word] + [line("RULES", "all parts: %s/rules" % play_url),
                         line("START", "today's puzzles: " + play_url),
                         line("NEXT", "open one address above")]
    return "\n".join(out) + "\n"


def rules_page(play_url, part=None):
    """/play/rules: an index of the rules text in parts, each with its size.
    /play/rules-WORD: one part."""
    parts = rule_parts()
    if part is not None:
        if part not in parts:
            raise BadAddress("no rules page named %s" % shown(part), play_url + "/rules")
        return _rules_part_page(parts, part, play_url)
    out = wrap("RULES", "Everything the game promises, in %d free pages. None of them "
               "hints at a puzzle." % len(parts), width=78)
    for word, what, _ in RULE_PARTS:
        size = len(_rules_part_page(parts, word, play_url)) // 4
        out.append(line(word.upper(), "%s, about %d tokens:" % (what, size)))
        out.append(line("", "%s/rules-%s" % (play_url, word)))
    out.append(line("START", "today's puzzles: " + play_url))
    out.append(line("NEXT", "open one address above"))
    return "\n".join(out) + "\n"


def error_page(message, back, play_url):
    out = wrap("ERROR", message, width=78)
    if back:
        out.append(line("BACK", back))
    out.append(line("START", "today's puzzles: " + play_url))
    out.append(line("NEXT", "open BACK or START" if back else "open START"))
    return "\n".join(out) + "\n"


def _shortest_untested(gs, n, lo=0, hi=None):
    hi = len(U) if hi is None else hi
    out = []
    for q in range(lo, hi):
        if q not in gs.board:
            out.append(q)
            if len(out) == n:
                break
    return out


def _test_links(base, qs):
    return ["%s/%s" % (base, koan_str(U[q]).replace(" ", "")) for q in qs]


TEMPLATE = "/1234 (test) /check-has-2 /guess-ends-1-or-has-3 (words joined by -)"


def _block(key, header, links):
    out = [line(key, header)] if header else []
    for i, u in enumerate(links):
        out.append(line(key if (i == 0 and not header) else "", u))
    return out


def fits_lines(base, gs, fit):
    """The game page's FITS line: the count, and the rules themselves when they are few."""
    tier = gs.game["tier"]
    count = "%d of %d tier %d rules still fit" % (len(fit), len(get_lists()[tier]), tier)
    if not 0 < len(fit) <= FITS_INLINE:
        return [line("FITS", "%s; their list is free: %s/state" % (count, base))]
    out = [line("FITS", count + ("s. This link guesses it:" if len(fit) == 1 else
                                 ". Each link guesses (wrong costs 3):"))]
    out += [line("", "%s/guess-%s" % (base, seg(e.text.split()))) for e in fit]
    out.append(line("RULE", "state another rule, free: %s/state" % base))
    return out


def playing_links(play_url, label, kept, gs, last=None, fit=None):
    base = game_url(play_url, label, kept)
    tests = _shortest_untested(gs, 4, 0, 4) or _shortest_untested(gs, 4)
    n = len(koan_str(U[tests[0]]).replace(" ", "")) if tests else 0
    out = _block("TEST", "%s, cost 1 each:" % ("one-digit koans" if n == 1 else "short koans"),
                 _test_links(base, tests))
    out.append(line("MORE", "longer koans, also cost 1 each; their list is free: %s/more-2"
                    % base))
    if fit is None:
        out.append(line("RULE", "state a rule, free: %s/state" % base))
    else:
        out += fits_lines(base, gs, fit)
    out.append(line("SHOW", "free: %s/show" % base))
    if last == ["giveup"]:
        out.append(line("GIVEUP", "yes, end it and see the rule: %s/giveup-yes" % base))
    else:
        out.append(line("GIVEUP", "asks first: %s/giveup" % base))
    return out


def end_links(play_url, page_url, label, kept, gs):
    out = [line("REPLAY", "this whole game: " + game_url(play_url, label, kept))]
    out.append(line("ANOTHER", "today's puzzles: " + play_url))
    out.append(line("", "a new tier %d puzzle: %s" % (
        gs.game["tier"], game_url(play_url, another_label(label, gs.game["tier"])))))
    return out


def sub_page(play_url, label, kept, gs, sub, board, fit=None, switch=None):
    """A free page: more tests, the rule lists, or the draft builder. `fit` is the list of
    rules that still fit when FITS is on for this puzzle; `switch` is the line that offers
    the state page with FITS the other way round."""
    base = game_url(play_url, label, kept)
    tier = gs.game["tier"]
    head = sub[0][0]
    out = [board, "", line("ADDRESS", base)]
    if head == "state" and len(sub) == 1 and switch:
        if fit is not None:
            return out + fits_page(base, tier, sub[0][1:], fit) + [switch]
        if tier <= 2:
            return out + state_page(base, tier, sub[0][1:]) + [switch]
        if len(sub[0]) == 1:
            return out + draft_page(base, [["draft", "guess"]]) + [switch]
    if head == "more" and len(sub) == 1:
        arg = sub[0][1:]
        if arg in (["2"], []):
            qs = _shortest_untested(gs, 16, 4, 20)
            out += _block("TEST", "two-digit koans not on the board, cost 1 each:",
                          _test_links(base, qs))
            out.append(line("MORE", "three-digit koans, by first digit, free pages:"))
            out += [line("", "%s/more-3-%d" % (base, d)) for d in (1, 2, 3, 4)]
            return out
        if len(arg) == 2 and arg[0] == "3" and arg[1] in ("1", "2", "3", "4"):
            first = 20 + 16 * (int(arg[1]) - 1)
            qs = _shortest_untested(gs, 16, first, first + 16)
            out += _block("TEST", "three-digit koans starting %s, not on the board, cost 1:"
                          % arg[1], _test_links(base, qs))
            out += wrap("LONGER", "koans of 4 to 6 digits: add /1234 (your digits) to "
                        + base, width=200)
            return out
        raise BadAddress("more takes 2, or 3 and a first digit, as in more-3-1",
                         base + "/more-2")
    if head == "state" and tier <= 2 and len(sub) == 1:
        return out + state_page(base, tier, sub[0][1:])
    if head == "state" and len(sub) == 1 and len(sub[0]) == 1:
        sub = [["draft", "guess"]]
        head = "draft"
    if head == "draft" and len(sub) <= 2:
        return out + draft_page(base, sub)
    raise BadAddress("this sub-page does not exist", base)


def compact_board(board):
    """Only the koans (YES, NO) and a held theory from `show`: a free page needs no score."""
    out, keep = [], False
    for ln in board.split("\n"):
        key = ln[:10].strip()
        if key:
            keep = key in ("YES", "NO", "HOLDING")
        if keep:
            out.append(ln)
    return "\n".join(out) or board


def _families(tier):
    fams = {}
    for e in get_lists()[tier]:
        fams.setdefault(e.family, []).append(e.text)
    return fams


OWN_TEMPLATE = "or add /guess-WORDS to ADDRESS, as in /guess-has-2-or-ends-1"


def state_page(base, tier, family_words):
    fams = _families(tier)
    out = wrap("RULES", "tier %d hides one of these %d. Each link guesses (wrong costs 3); "
               "check in place of guess is free." % (tier, sum(len(v) for v in fams.values())),
               width=78)
    family = " ".join(family_words)
    if family:
        if family not in fams:
            raise BadAddress("no rule group named %s" % shown(seg(family_words)), base + "/state")
        out.append(line("GROUP", family))
        out += [line("", "%s/guess-%s" % (base, seg(t.split()))) for t in fams[family]]
    elif tier == 1:
        for fam, texts in fams.items():
            out.append(line("GROUP", fam))
            out += [line("", "%s/guess-%s" % (base, seg(t.split()))) for t in texts]
    else:
        out.append(line("GROUPS", "free pages, one per group:"))
        out += [line("", "%s/state-%s  (%d)" % (base, seg(f.split()), len(v)))
                for f, v in fams.items()]
    out.append(line("OWN", "a rule of your own, word by word: %s/draft-guess" % base))
    out.append(line("", OWN_TEMPLATE))
    return out


def fits_page(base, tier, family_words, fit):
    """The state page with FITS on: only the listed rules that still fit, as guess links."""
    family = " ".join(family_words)
    if family:
        if family not in _families(tier):
            raise BadAddress("no rule group named %s" % shown(seg(family_words)), base + "/state")
        fit = [e for e in fit if e.family == family]
        head = "%d tier %d rule%s in the group %s still fit%s the board." % (
            len(fit), tier, "" if len(fit) == 1 else "s", family, "s" if len(fit) == 1 else "")
    else:
        head = "%d of %d tier %d rules still fit%s the board." % (
            len(fit), len(get_lists()[tier]), tier, "s" if len(fit) == 1 else "")
    if fit:
        head += " Each link guesses (wrong costs 3); a test that splits them costs 1."
    out = wrap("FITS", head, width=78)
    out += [line("", "%s/guess-%s" % (base, seg(e.text.split()))) for e in fit]
    out.append(line("OWN", "a rule of your own, word by word: %s/draft-guess" % base))
    out.append(line("", OWN_TEMPLATE))
    return out


CONNECTORS = ("and", "or", "not")


def draft_page(base, sub):
    words = sub[0][1:]
    if not words or words[0] not in ("guess", "check"):
        raise BadAddress("a draft starts with guess or check, as in draft-guess-has-2",
                         base + "/draft-guess")
    if len(words) > 30:
        raise BadAddress("the draft is too long", base + "/draft-guess")
    verb, body = words[0], words[1:]
    here = "%s/draft-%s" % (base, seg(words))
    try:
        whole = bool(body) and parse_rule([" ".join(body)]) is not None
    except Exception:              # an unfinished draft: any parse error just means "not yet"
        whole = False
    out = [line("DRAFT", "%s %s" % (verb, " ".join(body)) if body else verb + " (no words yet)")]
    if whole:
        other = "check" if verb == "guess" else "guess"
        out.append(line("SEND", "%s/%s-%s" % (base, verb, seg(body))))
        out.append(line("", "as a %s instead: %s/%s-%s" % (other, base, other, seg(body))))
    elif body:
        out.append(line("SEND", "not yet: the draft is not a whole rule"))
    fams = _families(3)
    if len(sub) == 2:
        if sub[1][0] != "pick" or " ".join(sub[1][1:]) not in fams:
            raise BadAddress("no word group named %s" % shown(seg(sub[1])), here)
        fam = " ".join(sub[1][1:])
        out.append(line("ADD", "one %s condition, free pages:" % fam))
        out += [line("", "%s-%s" % (here, seg(t.split()))) for t in fams[fam]]
        out.append(line("BACK", here))
        out.append(line("OWN", OWN_TEMPLATE))
        return out
    open_end = not body or body[-1] in CONNECTORS
    if open_end:
        # The next word is a condition (or not): offer the groups.
        if not body or body[-1] != "not":
            out.append(line("ADD", "not, free: %s-not" % here))
        out.append(line("PICK", "a group of conditions to add, free pages:"))
        out += [line("", "%s/pick-%s" % (here, seg(f.split()))) for f in fams]
    else:
        # The draft ends in a condition: only a joining word can follow.
        out.append(line("ADD", "a joining word, then a condition, free pages:"))
        out += [line("", "%s-%s" % (here, c)) for c in ("and", "or")]
    out.append(line("OWN", OWN_TEMPLATE))
    return out


def game_page(play_url, page_url, label, moves, sub, root=None, fits=FITS_DEFAULT):
    """play_url carries the FITS choice (mode_url); root is the front door without it."""
    root = play_url if root is None else root
    check_moves(moves, label, play_url)
    listed = label_tier(label) in LISTED_TIERS
    r = play(label, moves, extra=["show"] if sub else None, assisted=fits and listed)
    if r["failed_new"]:
        raise BadAddress(tidy(r["text"]).replace("\n", " ").replace("ERROR", "").strip()
                         or "this puzzle cannot be started")
    game, kept = r["game"], r["kept"]
    label = game["label"]
    gs = replay(game)
    fit = fitting(gs) if fits and listed and not gs.over else None
    other = mode_url(root, not fits)
    out = []
    if not moves and not sub:
        out += wrap("HOW", HOW) + wrap("VISIT", VISIT)
    else:
        out += reminder(play_url)
    out.append(line("HONOR", HONOR))
    out.append("")
    here = game_url(play_url, label, kept)
    if sub:
        board = tidy(r["extra_text"])
        if gs.over:
            out += [board, ""] + end_links(play_url, page_url, label, kept, gs)
            out.append(line("NEXT", "this game is over: open one address above"))
            return "\n".join(out) + "\n"
        switch = switch_line(fits, game_url(other, label, kept, sub)) if listed else None
        out += sub_page(play_url, label, kept, gs, sub, compact_board(board), fit, switch)
        out.append(line("NEXT", "open one address above. This page was free and changed "
                        "nothing."))
        return "\n".join(out) + "\n"
    out.append(tidy(r["text"], share=person_link(page_url, play_url, label), base=here,
                    rules=play_url + "/rules-tiers"))
    if re.search(r"^WRONG ", r["text"], re.M):
        out.append(line("COST", "the first 10 wrong guesses cost 3 each; later ones are free."))
    out.append("")
    if gs.over:
        out += end_links(play_url, page_url, label, kept, gs)
        tail = "open one address above"
        if gs.status == "solved" and re.search(r"^SHARE ", r["text"], re.M):
            tail += ", and hand the SHARE line to your person"
        out.append(line("NEXT", tail))
    else:
        out.append(line("ADDRESS", here))
        out += playing_links(play_url, label, kept, gs, moves[-1] if moves else None, fit)
        if not moves and listed:
            out.append(switch_line(fits, game_url(other, label)))
        elif not moves and fits:
            out.append(line("FITS", "tier 5 has no list of rules to fit: this puzzle plays "
                            "the plain way."))
        out.append(line("NEXT", "open one address above, or add your own move to ADDRESS:"))
        out.append(line("NEXT", TEMPLATE))
    return "\n".join(out) + "\n"


def handle(target, play_url, page_url="", date=None):
    """(status, headers, body) for one GET. play_url is the front door's address, such as
    https://example.org/play; page_url is the page for people, or ''."""
    root = play_url = play_url.rstrip("/")
    cache = CACHE_PAGE
    try:
        kind, label, moves, sub, fits = parse_target(target)
        play_url = mode_url(root, fits)       # every link below keeps the FITS choice
        other = mode_url(root, not fits)
        if kind == "door":
            body = door_page(play_url, page_url, date or today_utc(), switch_line(fits, other))
            cache = CACHE_DOOR
        elif kind == "how":
            body = how_page(play_url, page_url, switch_line(fits, other + "/how"))
        elif kind == "rules":
            body = rules_page(play_url, label)
        else:
            body = game_page(play_url, page_url, label, moves, sub, root, fits)
    except BadAddress as e:
        body, cache = error_page(str(e), e.back, play_url), CACHE_ERROR
    except Exception as e:            # never a crash page: a short text and a way back
        body = error_page("the server could not read this address (%s)" % type(e).__name__,
                          None, play_url)
        cache = CACHE_ERROR
    if cache == CACHE_PAGE and re.search(r"(?m)^ERROR ", body):
        cache = CACHE_ERROR           # a bounced move: same reply every time, but keep it short
    headers = {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": cache,
               "X-Robots-Tag": "noindex"}
    return 200, headers, body.encode("utf-8")
