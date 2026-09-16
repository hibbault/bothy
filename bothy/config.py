"""Reads service settings from the environment, so a service is configured the
same way whether it runs in a container or on your laptop.

Every setting also has an entry in a config file, which is what makes a host's
limits survive a reboot as something its owner can read. The precedence is one
line, in lookup: the environment wins over the file, and a flag wins over both
because flag parsing happens after this and overwrites. See the file section
below.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

from .errors import ConfigError


def text(key: str, def_: str = "") -> str:
    """Return the setting named key, or def when nothing sets it.

    (Go's `Str`. `str` would shadow a builtin, and Go's `def` is a Python
    keyword, so the parameter is `def_` here -- the meaning is unchanged.)
    """
    v = _lookup(key)
    if v != "":
        return v
    return def_


def dur(key: str, def_: float = 0.0) -> float:
    """Parse key as a duration in seconds, falling back to def.

    Go's `Dur` takes and returns a `time.Duration`; the Python side of the port
    counts seconds, which is what every other module here passes around and what
    `format_duration` renders.
    """
    v = _lookup(key)
    if v == "":
        return def_
    try:
        return parse_duration(v)
    except ConfigError:
        return def_


def int_(key: str, def_: int = 0) -> int:
    """Parse key as an integer, falling back to def."""
    v = _lookup(key)
    if v == "":
        return def_
    n = _parse_int(v)
    if n is None:
        return def_
    return n


def int64(key: str, def_: int = 0) -> int:
    """Parse key as a 64-bit integer, falling back to def. Byte counts use
    it: a body limit is the one setting where an int's width is worth not
    thinking about.

    Python integers have no width, so this is the same parse as `int_`; it is
    kept as a separate name because the call sites say which settings hold a
    byte count, and because the range it accepts is still a 64-bit one.
    """
    return int_(key, def_)


def bool_(key: str, def_: bool = False) -> bool:
    """Parse key as a boolean, falling back to def. Unset means def, and so
    does anything unparseable: a typo in a compose file should not silently flip a
    setting to false.

    It accepts what strconv.ParseBool does, plus yes/no and on/off and their first
    letters, because these defaults end up in compose files and shell exports
    where those spellings are what people reach for.

    (Go's `Bool`; `bool_` rather than `bool` only because the latter is a
    builtin.)
    """
    v = _lookup(key).lower()
    if v == "":
        return def_
    if v in ("yes", "y", "on", "1", "t", "true"):
        return True
    if v in ("no", "n", "off", "0", "f", "false"):
        return False
    return def_


def list_(key: str) -> List[str]:
    """Split key on commas, trimming spaces and dropping empty items."""
    out: List[str] = []
    for part in text(key, "").split(","):
        part = part.strip()
        if part != "":
            out.append(part)
    return out


def listen_default(fallback: str, *keys: str) -> str:
    """The address a service listens on when nothing else said.

    `BOTHY_LISTEN` -- or, for a service with its own name for it, the key passed in
    -- wins when it is set, because a person who wrote it meant it.

    Failing that, `PORT` is honoured. That is not a Bothy setting: it is how a
    container platform tells a process which port its traffic will arrive on, and a
    service that ignores it is a service the platform never routes to. The host is
    left empty, so ":8080" means every interface -- which is what a container needs,
    and the opposite of what a laptop wants, which is why the fallback is given per
    command rather than fixed here.
    """
    for key in keys or ("BOTHY_LISTEN",):
        value = text(key, "")
        if value != "":
            return value
    port = os.environ.get("PORT", "").strip()
    if port != "":
        return ":" + port
    return fallback


# The Go standard library's time.ParseDuration has no Python equivalent, so the
# parser lives here. It accepts the same syntax:
#
#   [-+]?([0-9]*(\.[0-9]*)?[a-z]+)+   -- and the single string "0"
#
# that is: an optional sign, then one or more components, each a decimal number
# with a unit. A fraction is written with a period and is allowed on any
# component, so "1.5h", "2m30.5s" and "1h0.5m" all parse. The units are ns, us,
# "µs" (the micro sign) and "μs" (the Greek mu), ms, s, m and h -- there is no
# "d" for days, because a day is not always 86400 seconds. Anything else, "1hr"
# and "10" and "soon" included, is refused rather than read as zero: a typo in a
# timeout must not silently remove it.
_DURATION_UNITS = {
    "ns": 1,
    "us": 1000,
    "ms": 1000 * 1000,
    "s": 1000 * 1000 * 1000,
    "m": 60 * 1000 * 1000 * 1000,
    "h": 3600 * 1000 * 1000 * 1000,
}

_NANOS = 1000 * 1000 * 1000


def parse_duration(raw: str) -> float:
    """Parse a Go duration like "10m", "1h30m" or "500ms" into seconds.

    Raises `ConfigError` with Go's message for anything `time.ParseDuration`
    would refuse. The magnitude is accumulated in nanoseconds, as Go does, so
    the arithmetic is the same integer arithmetic; what comes back is seconds,
    which is exact in a float64 to the nanosecond up to about 104 days -- past
    that the count of nanoseconds is what a float can no longer hold, not the
    syntax.
    """
    orig = raw
    s = raw
    neg = False
    if s[:1] in ("-", "+"):
        neg = s[0] == "-"
        s = s[1:]
    # Special case: if all that is left is "0", this is zero.
    if s == "0":
        return 0.0
    if s == "":
        raise ConfigError('time: invalid duration "%s"' % orig)

    total = 0
    while s != "":
        i = 0
        while i < len(s) and "0" <= s[i] <= "9":
            i += 1
        digits, s = s[:i], s[i:]
        pre = i > 0

        frac = 0
        scale = 1
        post = False
        if s[:1] == ".":
            s = s[1:]
            i = 0
            while i < len(s) and "0" <= s[i] <= "9":
                i += 1
            post = i > 0
            if post:
                frac = int(s[:i])
                scale = 10 ** i
            s = s[i:]
        if not pre and not post:
            # no digits: ".s" or "-.s"
            raise ConfigError('time: invalid duration "%s"' % orig)

        i = 0
        while i < len(s) and s[i] != "." and not ("0" <= s[i] <= "9"):
            i += 1
        if i == 0:
            raise ConfigError('time: invalid duration "%s"' % orig)
        unit, s = s[:i], s[i:]
        if unit in ("\u00b5s", "\u03bcs"):  # U+00B5, U+03BC
            unit = "us"
        if unit not in _DURATION_UNITS:
            raise ConfigError('time: unknown unit "%s" in duration "%s"' % (unit, orig))
        per_unit = _DURATION_UNITS[unit]

        v = int(digits or "0")
        if v > (1 << 63) // per_unit:
            raise ConfigError('time: invalid duration "%s"' % orig)  # overflow
        v *= per_unit
        if frac > 0:
            # float64 is needed to be nanosecond accurate for fractions of hours.
            v += (frac * per_unit) // scale
        total += v
    if total > 1 << 63:
        raise ConfigError('time: invalid duration "%s"' % orig)

    return -total / _NANOS if neg else total / _NANOS


def format_duration(seconds: float) -> str:
    """Render seconds the way Go's `time.Duration.String` does: "1h30m0s",
    "500ms", "1m0s", "20s", "0s".

    Both directions are needed: a host logs a limit ("per-peer budget",
    "time limit per request") and its health output reports one, and a limit
    printed as a float would not be a duration a reader could type back.
    """
    ns = int(round(seconds * _NANOS))
    neg = ns < 0
    if neg:
        ns = -ns
    if ns == 0:
        return "0s"

    if ns < _NANOS:
        # Special case: if the duration is smaller than a second, use the
        # smaller units, like 1.2ms.
        if ns < 1000:
            prec, suffix = 0, "ns"
        elif ns < 1000 * 1000:
            prec, suffix = 3, "\u00b5s"  # U+00B5 'µ' micro sign
        else:
            prec, suffix = 6, "ms"
        frac, ns = _format_fraction(ns, prec)
        out = str(ns) + frac + suffix
    else:
        frac, ns = _format_fraction(ns, 9)
        out = str(ns % 60) + frac + "s"
        ns //= 60
        if ns > 0:
            out = str(ns % 60) + "m" + out
            ns //= 60
            if ns > 0:
                # Stop at hours, because days can be different lengths.
                out = str(ns) + "h" + out

    return "-" + out if neg else out


def _format_fraction(v: int, prec: int) -> Tuple[str, int]:
    """Format the fraction of v/10**prec (e.g. ".12345"), omitting trailing
    zeros.

    It omits the decimal point too when the fraction is 0, and returns the value
    v/10**prec alongside the text.
    """
    digits: List[str] = []
    printing = False
    for _ in range(prec):
        digit = v % 10
        printing = printing or digit != 0
        if printing:
            digits.append(str(digit))
        v //= 10
    if printing:
        digits.append(".")
    return "".join(reversed(digits)), v


_DECIMAL = re.compile(r"[+-]?[0-9]+\Z")
_MIN_INT, _MAX_INT = -(1 << 63), (1 << 63) - 1


def _parse_int(value: str) -> Optional[int]:
    """Parse a decimal integer the way strconv.Atoi does, or None.

    Python's `int()` is not the same function: it reads "1_0" as ten and accepts
    any Unicode digit, so the text is checked first. A number Go's integer types
    cannot hold is refused here as it is there, which is what makes an absurd
    body limit fall back to the default instead of being trusted.
    """
    if not _DECIMAL.match(value):
        return None
    n = int(value)
    if n < _MIN_INT or n > _MAX_INT:
        return None
    return n


# ---------------------------------------------------------------------------
# The config file.
#
# A config file is where a machine's standing policy lives, as opposed to the
# thing you are trying this once.
#
# Flags are for an experiment and environment variables are for a container, but
# neither survives a reboot as something a person can read. A host that shares a
# GPU for months needs its limits written down once, in a file somebody can open,
# comment and keep in version control -- and a public host needs them written
# down most of all, because with no identity the limits are the only protection
# its owner has.
#
# Keys are the environment variable names. A setting therefore has exactly one
# name everywhere it appears -- in the file, in the environment, in `-h`, and in
# the documentation -- rather than a short flag name and a long variable name
# that a reader has to map between.
# ---------------------------------------------------------------------------


class File:
    """One config file, and the settings it set.

    Go has a `path` field and a `Path()` method; here the field is `_path`, so
    that the method of the same name is not shadowed by it.
    """

    def __init__(self, path: str, values: Optional[Dict[str, str]] = None) -> None:
        self._path = path
        self._values: Dict[str, str] = dict(values or {})

    def path(self) -> str:
        """Where this config was read from, whether or not it existed."""
        return self._path

    def len(self) -> int:
        """How many settings the file actually set."""
        return len(self._values)

    def keys(self) -> List[str]:
        """List the settings the file set, sorted, for an operator asking what is
        actually configured."""
        return sorted(self._values)


def default_path() -> str:
    """Where Bothy looks for its config file: the OS's per-user config
    directory, so ~/.config/bothy/config on Linux, Application Support on macOS,
    and %AppData%\\bothy\\config on Windows.
    """
    directory = _user_config_dir()
    if directory == "":
        # Better a surprising path than no config at all: the caller prints it,
        # and `bothy config path` is the answer to "where did that come from?".
        return "bothy.conf"
    return os.path.join(directory, "bothy", "config")


def _user_config_dir() -> str:
    """Go's os.UserConfigDir: the directory this OS keeps a user's configuration
    in, or "" when it will not say.
    """
    if os.name == "nt":
        return os.environ.get("APPDATA", "")
    if sys.platform == "darwin":
        home = os.environ.get("HOME", "")
        if home == "":
            return ""
        return os.path.join(home, "Library", "Application Support")
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg != "":
        return xdg
    home = os.environ.get("HOME", "")
    if home == "":
        return ""
    return os.path.join(home, ".config")


def load(path: str) -> File:
    """Read a config file.

    A file that does not exist is not an error, and yields an empty config: most
    people have none, and a host refusing to start because a path it was told about
    was absent would be worse than one using its defaults.

    A file that exists is held to a higher standard than that. An unparseable line
    is an error naming the line number, and an unknown key is an error naming the
    key -- because the alternative is a typo that silently configures nothing, on a
    file whose whole purpose is to say what the limits are.

    An `OSError` is left to travel, as the Go side returns the ReadFile error: a
    file that was there but could not be read is the operating system's answer,
    not a complaint about the format.
    """
    f = File(path)
    try:
        with open(path, "rb") as fh:
            raw_bytes = fh.read()
    except FileNotFoundError:
        return f
    text_ = raw_bytes.decode("utf-8", "replace")

    for i, raw in enumerate(text_.split("\n")):
        # A Windows editor's BOM, which is only ever at the start of a line.
        line = raw[1:] if raw.startswith("\ufeff") else raw
        line = line.strip()
        if line == "" or line.startswith("#") or line.startswith(";"):
            continue
        key, eq, value = line.partition("=")
        if not eq:
            raise ConfigError(
                "%s line %d: %s is not a setting, want key = value" % (path, i + 1, _quote(line))
            )
        key = key.strip().upper()
        value = _unquote(_strip_comment(value.strip()))
        if key == "":
            raise ConfigError("%s line %d: no setting name before the =" % (path, i + 1))
        if key not in KNOWN_KEYS:
            raise ConfigError(
                "%s line %d: unknown setting %s%s" % (path, i + 1, _quote(key), _suggest(key))
            )
        f._values[key] = value
    return f


def _quote(s: str) -> str:
    """Go's %q, near enough for a config line.

    JSON's string quoting is the same for everything a line of a config file can
    hold; the two differ only over control characters nobody puts in one.
    """
    return json.dumps(s, ensure_ascii=False)


# fallback is the config file consulted when an environment variable is unset.
#
# It is package state because the alternative is threading a file through every
# flag default in the program, and this is resolved once, at startup, before
# anything reads a setting. set_fallback(None) puts it back to no file at all.
_fallback: Optional[File] = None


def set_fallback(f: Optional[File]) -> None:
    """Make f the source of defaults for every setting. Flags still win,
    because flag parsing happens after this and overwrites whatever was there.
    """
    global _fallback
    _fallback = f


def _lookup(key: str) -> str:
    """Resolve one setting: the environment first, then the config file.

    This is the whole of the precedence rule. Flags override both by parsing later,
    so the order a person has to remember is: flag, environment, file, built-in.
    """
    v = os.environ.get(key, "").strip()
    if v != "":
        return v
    if _fallback is not None:
        v = _fallback._values.get(key.upper(), "")
        if v != "":
            return v
    return ""


def _strip_comment(v: str) -> str:
    """Remove a comment that starts mid-line, so that

        BOTHY_MAX_REQUEST_TIME = 10m   # no request may run longer than this

    sets 10m rather than a value with a sentence on the end. A # inside a value
    survives unless whitespace precedes it, because a share key is allowed to
    contain one and a comment is not allowed to eat it.
    """
    for i in range(1, len(v)):
        if v[i] == "#" and v[i - 1] in (" ", "\t"):
            return v[:i].strip()
    return v


def _unquote(v: str) -> str:
    """Strip one layer of matching quotes, so a value with spaces does not
    have to be escaped to survive being read as a whole.
    """
    if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
        return v[1:-1]
    return v


def _suggest(key: str) -> str:
    """Offer the closest known setting to a misspelling, because the error a
    person actually needs is the name they meant.

    The candidates are walked in a fixed order. Go walks a map, so two keys that
    share a prefix with the typo to the same depth make Go's answer depend on
    that run's map seed; sorting is the only difference and it makes the message
    repeatable.
    """
    best, best_score = "", 0
    for known in sorted(KNOWN_KEYS):
        score = _common_prefix(key, known)
        if score > best_score:
            best, best_score = known, score
    if best_score < 4:
        return ""
    return ", did you mean " + best + "?"


def _common_prefix(a: str, b: str) -> int:
    n = 0
    while n < len(a) and n < len(b) and a[n] == b[n]:
        n += 1
    return n


# KNOWN_KEYS is every setting Bothy has ever read, by environment name, which is
# why the retired task runner's (`BOTHY_SWARM_*`) are still in it: a config file is
# read by whichever build is running, and refusing a file over a line nothing reads
# any more would break a host that upgraded rather than one that mistyped.
KNOWN_KEYS = frozenset({
    "BOTHY_ADMIN_KEY",
    "BOTHY_ALLOW_ALL_ROUTES",
    "BOTHY_ALLOW_ROUTES",
    "BOTHY_CHECK_TIMEOUT",
    "BOTHY_CLIENT_LISTEN",
    "BOTHY_DISCOVERY_URL",
    "BOTHY_ENGINE_KIND",
    "BOTHY_ENGINE_URL",
    "BOTHY_EXPECTED_DIGEST",
    "BOTHY_HEARTBEAT",
    "BOTHY_HOST",
    "BOTHY_HOST_LISTEN",
    "BOTHY_LISTEN",
    "BOTHY_LOCAL_API_KEY",
    "BOTHY_MAX_BODY",
    "BOTHY_MAX_CONCURRENT",
    "BOTHY_MAX_REQUESTS_PER_MINUTE",
    "BOTHY_MAX_REQUEST_TIME",
    "BOTHY_MOCK_DELAY",
    "BOTHY_MOCK_MODELS",
    "BOTHY_MOCK_NAME",
    "BOTHY_MODELS",
    "BOTHY_MODELS_DIR",
    "BOTHY_MODEL",
    "BOTHY_NODE_TIMEOUT",
    "BOTHY_OWNER_RESERVE",
    "BOTHY_PAUSED",
    "BOTHY_PEER_MAX_CONCURRENT",
    "BOTHY_PEER_QUOTA",
    "BOTHY_PROBE_TIMEOUT",
    "BOTHY_PUBLIC_ADDRESS",
    "BOTHY_REGISTRY_TOKEN",
    "BOTHY_REGISTRY_TTL",
    "BOTHY_SERVE",
    "BOTHY_SHARE_KEYS",
    "BOTHY_SHARE_KEY",
    "BOTHY_STREAM_USAGE",
    "BOTHY_SWARM_DIR",
    "BOTHY_WEIGHTS_PATH",
})
