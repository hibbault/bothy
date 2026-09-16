"""Bothy, in Python.

Share a GPU, borrow a GPU. This package is one implementation of the wire
contract in PROTOCOL.md — the same contract the Go implementation in this
repository speaks, which is the point of the protocol being written down rather
than implied: either can be replaced, and a Python host and a Go client
interoperate because the bytes on the wire are the spec.

The shape mirrors the Go packages one for one, so a reader who knows one can find
their way around the other:

    model      a servable model, and the identity of its weights
    digest     hashing a weights file, cached against size and mtime
    config     settings, from the environment or a config file
    httpx      the HTTP helpers every service shares
    meter      what each peer uses, and the limits that apply to it
    engine     what an inference engine can serve
    registry   the discovery record, its in-memory store, and its client
    discovery  the registry service
    host       the share side: announce, meter, proxy
    client     the connect side: a local endpoint that is somebody else's GPU
    app        both halves in one process, which is how a person runs it
    cli        the command line

Nothing here depends on anything outside the standard library. That is not
minimalism for its own sake: a host has to run on whatever machine already has
the GPU, and "pip install" is a step between a person and sharing it.
"""

from __future__ import annotations

# version is what a released build reports. The Go binary stamps this at build
# time; here it is a constant, because there is no compiler to stamp it.
version = "0.2.1-dev"

__all__ = ["version"]
