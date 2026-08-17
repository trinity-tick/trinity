#!/usr/bin/env python3
"""Entry point for `python -m trinity.api.server` (CLI, --port 8001).

The monolith was a single server.py module whose `if __name__ == "__main__"`
block ran the CLI; as a package, `python -m trinity.api.server` needs this
module (Python refuses to execute a package's __init__ as __main__).
"""

from trinity.api.server import main

if __name__ == "__main__":
    main()
