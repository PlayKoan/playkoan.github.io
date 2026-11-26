"""Exceptions the game raises on purpose, and the helpers of their messages. Anything else is
an internal error (exit 4)."""

import re

_ECHO_UNSAFE = re.compile(r"[^A-Za-z0-9_.:=+/-]")


def shorten(text):
    """A token longer than 20 characters as its first 17 and `...` (SPEC 8.7), so that a long
    run of digits never makes a line longer than the 78-character cap (8.2)."""
    return text[:17] + "..." if len(text) > 20 else text


def echo(tok):
    """Quote user input for a message without ever printing an unsafe byte (SPEC 8.7)."""
    return "'" + shorten(_ECHO_UNSAFE.sub("?", tok)) + "'"


def plural(n, word):
    """`1 test`, `2 tests`, `0 tests`, `2 wrong guesses`: the one helper for counts (SPEC 8.2)."""
    if n == 1:
        return "%d %s" % (n, word)
    return "%d %s" % (n, word + "es" if word.endswith("ss") else word + "s")


class KoanError(Exception):
    """Base class of the game's own exceptions."""


class UsageError(KoanError):
    """Bad command or rule text (exit 2). `tries` is a list of (command, note) suggestions."""

    def __init__(self, message, tries=()):
        super().__init__(message)
        self.message = message
        self.tries = list(tries)


class Refused(KoanError):
    """A well-formed command that cannot run now (exit 1). `next` overrides the NEXT line and
    `keyword` the ERROR keyword (`ask` in tiers 1 to 4 answers on an ASK line)."""

    def __init__(self, message, next=None, keyword="ERROR"):
        super().__init__(message)
        self.message = message
        self.next = next
        self.keyword = keyword


class NoGame(KoanError):
    """No puzzle to act on (exit 3)."""

    def __init__(self, message="no puzzle yet"):
        super().__init__(message)
        self.message = message


class BadSave(KoanError):
    """The save file is unreadable or does not match its game (exit 3). Never secret-derived."""

    def __init__(self, reason, other_version=False):
        super().__init__(reason)
        self.reason = reason
        self.other_version = other_version


class Busy(KoanError):
    """The save is locked by another command (exit 5)."""


class InternalError(KoanError):
    """A broken invariant (exit 4). Only the class name is ever shown to the player."""
