"""The errors Bothy raises, and the one thing they have in common.

Go returns sentinel errors and typed error values; Python raises. Each module
still defines the errors it is in a position to explain — a refusal comes from
the meter, a digest mismatch from the client — because an error belongs next to
the code that knows what it means. They all derive from `BothyError` so that a
caller which only wants to say "something Bothy raised" can say it once.

Nothing here is a stand-in for a protocol error. A refusal, a mismatch and an
unreachable fleet are all part of the contract in PROTOCOL.md, and each is turned
into the status code that document specifies — by the service that answers the
caller, not by the code that raised.
"""

from __future__ import annotations


class BothyError(Exception):
    """Base class for every error Bothy raises on purpose.

    A `BothyError` reaching the surface means one of two things: a configuration
    that cannot work (a typo in a limit, a digest that matches nothing), or a
    fleet that could not be used. Both are worth a clear sentence rather than a
    traceback, which is what the CLI prints for them.
    """


class BodyTooLarge(BothyError):
    """A request body was larger than the service is willing to hold.

    Only raised by the helpers in `bothy.httpx` that buffer a body; a service that
    streams never sees it. The host answers this with 413 rather than reading the
    whole thing and then complaining.
    """


class ConfigError(BothyError):
    """A setting cannot be understood.

    Raised at startup, before anything listens, because the alternative is a
    limit that silently does nothing — which on a public host is the one thing
    its owner is relying on.
    """
