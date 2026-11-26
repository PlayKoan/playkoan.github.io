"""The save file: location, lock, atomic write, structural validation, history (SPEC 9)."""

import contextlib
import datetime
import errno
import json
import os
import re
import tempfile
import time
from pathlib import Path

from . import SCHEMA
from .errors import BadSave, Busy, UsageError
from .puzzle import label_tier

try:
    import msvcrt

    def _lock(fd):
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd):
        os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
except ImportError:   # POSIX
    import fcntl

    def _lock(fd):
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)

LOCK_TIMEOUT = 5.0
_NO_LOCKS = {getattr(errno, n) for n in ("ENOLCK", "ENOSYS", "EOPNOTSUPP", "ENOTSUP")
             if hasattr(errno, n)}
MAX_HISTORY = 2000
SAVE_NAME = "koan.json"
_DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z", re.ASCII)
_KOAN = re.compile(r"\A[1-4]{1,6}\Z", re.ASCII)
_HEX = re.compile(r"\A(?:[0-9a-f]{2}){1,400}\Z", re.ASCII)   # a hold or ask rule, 1-400 chars


GAME_FILE = None   # round 13: a one-file build (koan.py) sets this to its own path
_RECORDS = (SAVE_NAME, "deals.json")


class SaveDirError(Exception):
    """Round 13: no save folder can be written (exit 1, the refused code)."""

    def __init__(self, path, from_env):
        super().__init__(str(path))
        self.path = Path(path)
        self.from_env = from_env


def game_file():
    """The file the player runs: game.py next to the engine, or the one-file koan.py."""
    if GAME_FILE is not None:
        return Path(GAME_FILE)
    return Path(__file__).resolve().parent.parent / "game.py"


def default_dir():
    return game_file().parent / ".save"


def fallback_dir():
    """Round 13: <system temp folder>/koan, used when the game's own folder cannot be written
    (agent sandboxes often mount the code read-only). None if there is no temp folder."""
    try:
        return Path(tempfile.gettempdir()) / "koan"
    except OSError:
        return None


def _has_records(directory):
    return any((directory / name).exists() for name in _RECORDS)


def save_dir(environ):
    """Where the save is read from. GAME_HOME, when set, always wins. Otherwise the folder next
    to the game, unless it holds nothing and the temp fallback holds the records."""
    home = environ.get("GAME_HOME", "")
    if home:
        return Path(home)
    default = default_dir()
    if not _has_records(default):
        fallback = fallback_dir()
        if fallback is not None and _has_records(fallback):
            return fallback
    return default


def can_write(directory):
    """True if the folder exists and can be written, or could be created (nothing is made)."""
    d = Path(directory)
    while not d.exists():
        if d.parent == d:
            return False
        d = d.parent
    return d.is_dir() and os.access(str(d), os.W_OK)


def _make_usable(directory):
    """Create the folder and open its lock file; raises OSError if either cannot be done."""
    directory.mkdir(parents=True, exist_ok=True)
    os.close(os.open(str(directory / "koan.lock"), os.O_RDWR | os.O_CREAT))
    if not os.access(str(directory), os.W_OK):
        raise PermissionError("not writable")


def writable_dir(environ):
    """(folder, moved) for a command that writes. The folder exists and can be written. moved
    is True when the game's own folder could not be used and the temp fallback is used for the
    first time (no save there yet). Raises SaveDirError when no folder works."""
    home = environ.get("GAME_HOME", "")
    directory = save_dir(environ)
    try:
        _make_usable(directory)
        moved = not home and directory != default_dir() and not (directory / SAVE_NAME).exists()
        return directory, moved
    except OSError:
        if home or directory != default_dir():
            raise SaveDirError(directory, bool(home)) from None
    fallback = fallback_dir()
    if fallback is None:
        raise SaveDirError(directory, False)
    try:
        _make_usable(fallback)
    except OSError:
        raise SaveDirError(directory, False) from None
    return fallback, not (fallback / SAVE_NAME).exists()


def is_date(text):
    if not isinstance(text, str) or not _DATE.match(text):
        return False
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def today(environ):
    value = environ.get("GAME_TODAY")
    if value is not None:
        if not is_date(value):
            raise UsageError("GAME_TODAY must be a date like 2026-10-26")
        return datetime.date.fromisoformat(value).isoformat()
    # round 13: the GMT date, so that everyone gets the same daily at the same moment
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


@contextlib.contextmanager
def locked(directory, timeout=None):
    """Hold the OS-level lock on <directory>/koan.lock; the OS releases it if we die."""
    fd = os.open(str(Path(directory) / "koan.lock"), os.O_RDWR | os.O_CREAT)
    limit = LOCK_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + limit
    try:
        while True:
            try:
                os.lseek(fd, 0, 0)
                _lock(fd)
                break
            except OSError as e:
                if e.errno in _NO_LOCKS:    # round 13: a file system without locks: go on
                    break                   # unlocked rather than say "busy" forever
                if time.monotonic() >= deadline:
                    raise Busy() from None
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                _unlock(fd)
            except OSError:
                pass
    finally:
        os.close(fd)


def write_atomic(directory, obj, saved=None):
    """Write the save in one os.replace, so a reader sees the old file or the new one.
    `saved[0]` becomes True as soon as the replace has happened (SPEC 8.9)."""
    directory = Path(directory)
    path = directory / SAVE_NAME
    tmp = directory / ("%s.%d.tmp" % (SAVE_NAME, os.getpid()))
    text = json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=True) + "\n"
    with open(tmp, "w", encoding="ascii", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    last = None
    for i in range(50):
        try:
            os.replace(tmp, path)
            if saved is not None:
                saved[0] = True
            return
        except PermissionError as e:   # Windows: a scanner or reader may hold the file briefly
            last = e
            time.sleep(0.02 * (i + 1) ** 0.5)
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise last


def empty_save():
    return {"schema": SCHEMA, "counter": 0, "game": None, "history": []}


# ---------------------------------------------------------------- structural validation (9.1)
GAME_KEYS = {"assisted", "gen", "id", "label", "moves", "replay", "started", "status", "tier"}
HISTORY_KEYS = {"id", "gen", "tier", "label", "assisted", "replay", "started", "ended", "outcome",
                "tests", "wrong", "score", "par"}
MOVE_KEYS = {"test": {"v", "k", "a", "p"}, "wrong": {"v", "g", "c", "a"}, "solve": {"v"},
             "giveup": {"v"}, "hold": {"v", "r"}, "ask": {"v", "r"}}


def _int(x, lo=None, hi=None):
    return type(x) is int and (lo is None or x >= lo) and (hi is None or x <= hi)


def _label_ok(label, tier):
    return isinstance(label, str) and label_tier(label) == tier and \
        (not label.startswith("daily-") or is_date(label[len("daily-"):]))


def _move_ok(m):
    if not isinstance(m, dict) or m.get("v") not in MOVE_KEYS or set(m) != MOVE_KEYS[m["v"]]:
        return False
    if m["v"] == "test":
        return isinstance(m["k"], str) and _KOAN.match(m["k"]) is not None and \
            _int(m["a"], 0, 1) and (m["p"] is None or _int(m["p"], 0, 1))
    if m["v"] == "wrong":
        return isinstance(m["g"], str) and 0 < len(m["g"]) <= 400 and \
            isinstance(m["c"], str) and _KOAN.match(m["c"]) is not None and _int(m["a"], 0, 1)
    if m["v"] == "hold":   # r: the held rule's canonical text in hex, or None for `hold off`
        return m["r"] is None or (isinstance(m["r"], str) and _HEX.match(m["r"]) is not None)
    if m["v"] == "ask":    # r: the asked rule's canonical text in hex, as for hold
        return isinstance(m["r"], str) and _HEX.match(m["r"]) is not None
    return True


def _game_ok(g, counter):
    if not isinstance(g, dict) or set(g) != GAME_KEYS:
        return False
    return (_int(g["id"], 1, counter) and _int(g["gen"]) and _int(g["tier"], 1, 5)
            and _label_ok(g["label"], g["tier"]) and type(g["assisted"]) is bool
            and type(g["replay"]) is bool and is_date(g["started"])
            and g["status"] in ("playing", "solved", "gaveup")
            and isinstance(g["moves"], list) and all(_move_ok(m) for m in g["moves"]))


def _history_ok(h, counter):
    if not isinstance(h, dict) or set(h) != HISTORY_KEYS:
        return False
    if h["outcome"] not in ("solved", "gaveup", "abandoned"):
        return False
    score_ok = _int(h["score"], 0) if h["outcome"] == "solved" else h["score"] is None
    return (_int(h["id"], 1, counter) and _int(h["gen"]) and _int(h["tier"], 1, 5)
            and _label_ok(h["label"], h["tier"]) and type(h["assisted"]) is bool
            and type(h["replay"]) is bool and is_date(h["started"]) and is_date(h["ended"])
            and _int(h["tests"], 0, 40) and _int(h["wrong"], 0, 10) and score_ok
            and _int(h["par"], 0))


def validate_structure(obj):
    """Raise BadSave('wrong schema') unless obj has exactly the shape of SPEC 9.1."""
    if not isinstance(obj, dict) or set(obj) != {"schema", "counter", "game", "history"}:
        raise BadSave("wrong schema")
    if not _int(obj["schema"]) or obj["schema"] != SCHEMA or not _int(obj["counter"], 0):
        raise BadSave("wrong schema")
    if obj["game"] is not None and not _game_ok(obj["game"], obj["counter"]):
        raise BadSave("wrong schema")
    hist = obj["history"]
    if not isinstance(hist, list) or not all(_history_ok(h, obj["counter"]) for h in hist):
        raise BadSave("wrong schema")
    ids = [h["id"] for h in hist]
    if ids != sorted(set(ids)):
        raise BadSave("wrong schema")


def load(directory):
    """The parsed save, or None when there is no save file. Raises BadSave if unreadable."""
    path = Path(directory) / SAVE_NAME
    if not path.exists():
        return None
    if path.is_dir():    # a folder named koan.json: unreadable, so new force can move it aside
        raise BadSave("not valid JSON")
    try:
        with open(path, encoding="ascii") as f:
            obj = json.load(f)
    except (ValueError, RecursionError):   # RecursionError: nested too deeply to decode
        raise BadSave("not valid JSON") from None
    validate_structure(obj)
    return obj


def upsert_history(save, entry):
    hist = [h for h in save["history"] if h["id"] != entry["id"]] + [entry]
    hist.sort(key=lambda h: h["id"])
    save["history"] = hist[-MAX_HISTORY:]
