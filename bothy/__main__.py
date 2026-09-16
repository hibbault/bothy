"""`python -m bothy <command>`: the command line, as a module.

The Go build has one binary, so `bothy share` is an executable. Here the package
is what there is, and `python -m bothy` is how it becomes a process -- which is
what the flags, the exit codes and the signals are ported for: a registry, a host
and a client started this way are the same three processes `docker-compose.yml`
starts.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
